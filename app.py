import os
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import asyncio
import io
import json
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
import uuid
from utils.public_stats import public_stats_bp

import aiosqlite
import requests
from ai_provider import get_provider
from auth_decorators import require_auth
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, get_flashed_messages,render_template_string
from flask import g
from utils.crossword_builder import generate_crossword_grid
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import threading
from collections import defaultdict, deque

from logging_config import setup_logging, register_request_logging, get_logger, log_critical_startup_failure

from utils.admin_api import admin_bp, ensure_admin_tables, get_settings, is_user_blocked

# =====================================================
# 🔹 CONFIG (ամբողջը՝ .env-ից)
# =====================================================

def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_set(name, default=""):
    return {x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()}

def abs_path(p):
    """Relative ուղին հաշվում է app.py-ի թղթապանակից, ոչ թե այն տեղից, որտեղից գործարկվել է python-ը։"""
    return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)

# ── Գաղտնիքներ ──
SECRET_KEY = os.getenv("SECRET_KEY", "")
LOG_VIEW_TOKEN = os.getenv("LOG_VIEW_TOKEN", "")
X_API_KEY = os.getenv("X_API_KEY", "")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# ── Ներքին API-ներ ──
API_URL = os.getenv("API_URL", "http://localhost:5000/api/ask")
API_TIMEOUT = env_int("API_TIMEOUT", 15)

MAINLOGIN_BASE = os.getenv("MAINLOGIN_BASE", "").rstrip("/")
MAINLOGIN_PROFILE_CLAIMS_PATH = os.getenv("MAINLOGIN_PROFILE_CLAIMS_PATH",
                                          "/api/users/{user_id}/profile-claims")
MAINLOGIN_TIMEOUT = env_int("MAINLOGIN_TIMEOUT", 10)
MAINLOGIN_VERIFY_SSL = env_bool("MAINLOGIN_VERIFY_SSL", False)

# ── Հանրային (browser-ի) հասցեներ՝ main / demo ──
MAINLOGIN_PUBLIC_MAIN = (os.getenv("MAINLOGIN_PUBLIC_MAIN") or os.getenv("MAINLOGIN_PUBLIC", "")).rstrip("/")
MAINLOGIN_PUBLIC_DEMO = os.getenv("MAINLOGIN_PUBLIC_DEMO", "").rstrip("/")
GATEWAY_PUBLIC_MAIN = (os.getenv("GATEWAY_PUBLIC_MAIN") or os.getenv("GATEWAY_PUBLIC", "")).rstrip("/")
GATEWAY_PUBLIC_DEMO = os.getenv("GATEWAY_PUBLIC_DEMO", "").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEMO_HOST_MARKER = os.getenv("DEMO_HOST_MARKER", "demo").lower()

# ── AI ──
DEFAULT_AI_MODEL = os.getenv("DEFAULT_AI_MODEL", "claude")
OTHER_AI_MODELS = env_set("OTHER_AI_MODELS", "claude")  # «ԱՅԼ ԱԲ» մոդելներ

# ── App / server ──
URL_PREFIX = os.getenv("URL_PREFIX", "")
# Վերբեռնման սահմանաչափ. պետք է համընկնի gateway/nginx-ի սահմանի հետ (կամ փոքր լինի)
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 25)
DATABASE = abs_path(os.getenv("DATABASE_PATH") or os.path.join("utils", "crossword.db"))
LOG_DIR = abs_path(os.getenv("LOG_DIR") or "logs")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = env_int("PORT", 15400)
FLASK_DEBUG = env_bool("FLASK_DEBUG", False)
FLASK_ENV = os.getenv("FLASK_ENV", "production")
USE_HTTPS = env_bool("USE_HTTPS", False)
SSL_CERT_FILE = abs_path(os.getenv("SSL_CERT_FILE", "certs/cert.pem"))
SSL_KEY_FILE = abs_path(os.getenv("SSL_KEY_FILE", "certs/key.pem"))


def profile_claims_url(user_id):
    """mainlogin-ի profile-claims endpoint-ի լրիվ հասցեն (path-ը՝ .env-ից)։"""
    return MAINLOGIN_BASE + MAINLOGIN_PROFILE_CLAIMS_PATH.format(user_id=user_id)


AM_TZ = timezone(timedelta(hours=4))

# Այս գործարկման (process-ի) մեկնարկի պահը — դրանից հին գրառումները
# viewer-ում default-ով չենք ցույց տալիս (տես view_logs)
APP_START_TIME = datetime.now(AM_TZ)

# =====================================================
# 🔹 APP
# =====================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY or "dev_fallback_key"

app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
# Ադմին պանելը սպասարկվում է AuthServer-ից (Pages/Admin/Crossword/*):
# Այստեղ միայն JSON API-ն է՝ /api/admin/*
app.register_blueprint(admin_bp)

# Public ստատիստիկա dshh.am կայքի համար՝ /api/public/stats (առանց մուտքի)
app.config["CROSSWORD_DB"] = DATABASE
app.register_blueprint(public_stats_bp)

# Import-ի պահին, ոչ թե __main__-ում. gunicorn/waitress-ի տակ __main__-ը
# չի կատարվում, իսկ առանց այս աղյուսակների admin API-ն կփլուզվեր:
ensure_admin_tables()

@app.errorhandler(RequestEntityTooLarge)
def handle_upload_too_large(e):
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    msg = f"Կցված ֆայլերի ընդհանուր չափը գերազանցում է թույլատրելի {limit_mb} ՄԲ-ը։"
    logger.warning(
        "upload_too_large",
        extra={"Operation": "UploadTooLarge", "Endpoint": request.endpoint,
               "ContentLength": request.content_length, "RequestId": g.get("request_id")},
    )
    # fetch-ով եկող հարցումներին (սևագիր, ստուգումներ)՝ JSON
    if request.endpoint in ('save_draft', 'check_crossword') or request.is_json:
        return jsonify({"success": False, "error": msg}), 413
    flash(msg, "error")
    return redirect(request.referrer or url_for('choose_keyword_mode'))

# ── Logging (Information / Warning / Error / Critical policy) ──────────────
setup_logging(app)
register_request_logging(app, current_user_getter=lambda: getattr(g, "current_user", {}).get("sub"))
logger = get_logger("app")



_client_log_hits = defaultdict(deque)
_client_log_lock = threading.Lock()
CLIENT_LOG_RATE_LIMIT = 20      # առավելագույն հարցում
CLIENT_LOG_RATE_WINDOW = 60     # վայրկյան


_LOG_VIEWER_HTML = """
<!doctype html><meta charset="utf-8">
<title>Լոգեր</title>
<style>
 body{font-family:Segoe UI,sans-serif;margin:20px;background:#111;color:#eee}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{border:1px solid #333;padding:6px 8px;text-align:left;vertical-align:top}
 th{background:#1c1c1c;position:sticky;top:0}
 tr.ERROR td, tr.Critical td, tr.CRITICAL td {background:#3a1414}
 tr.WARNING td {background:#3a3214}
 .nav a{margin-right:12px;color:#7ab8ff}
 pre{white-space:pre-wrap;margin:0}
</style>
<div class="nav">
 <a href="?file=app&token={{token}}">app.log</a>
 <a href="?file=error&token={{token}}">error.log</a>
 |
 <a href="?file={{file}}&token={{token}}">բոլորը</a>
 <a href="?file={{file}}&level=Information&token={{token}}">Info</a>
 <a href="?file={{file}}&level=Warning&token={{token}}">Warning</a>
 <a href="?file={{file}}&level=Error&token={{token}}">Error+</a>
  <a href="?file={{file}}&level=Critical&token={{token}}">Critical</a>
 |
 <a href="?file={{file}}&token={{token}}">🔵 Այս գործարկումից</a>
 <a href="?file={{file}}&all=1&token={{token}}">📜 Ամբողջ պատմությունը</a>
 <form method="post" action="logs/clear" style="display:inline"
       onsubmit="return confirm('Ջնջե՞լ {{file}}.log-ի բոլոր գրառումները');">
   <input type="hidden" name="file" value="{{file}}">
   <input type="hidden" name="token" value="{{token}}">
   <button type="submit"
     style="margin-left:16px;background:#5a1414;color:#eee;border:1px solid #833;
            padding:4px 10px;border-radius:4px;cursor:pointer">
     🗑 Մաքրել {{file}}.log
   </button>
 </form>
</div>
<h3>{{file}}.log — {% if show_all %}ամբողջ պատմությունը{% else %}այս գործարկումից սկսած{% endif %} ({{rows|length}} գրառում)</h3>
<table>
 <tr><th>Ժամ</th><th>Level</th><th>Operation</th><th>Message</th><th>Մանրամասն</th></tr>
 {% for r in rows %}
 <tr class="{{r.Severity}}">
   <td>{{r.Timestamp}}</td><td>{{r.Severity}}</td>
   <td>{{r.Operation}}</td><td>{{r.Message}}</td>
   <td><pre>{{r.extra}}</pre></td>
 </tr>
 {% endfor %}
</table>
"""

@app.route('/logs')
async def view_logs():
    # ── token ստուգում (query ?token= կամ X-Log-Token header) ──
    provided = request.args.get('token') or request.headers.get('X-Log-Token', '')
    if not LOG_VIEW_TOKEN or provided != LOG_VIEW_TOKEN:
        return "Access denied", 403

    which = request.args.get('file', 'error')
    level_filter = request.args.get('level')
    show_all = request.args.get('all') == '1'
    fname = 'error.log' if which == 'error' else 'app.log'
    path = os.path.join(LOG_DIR, fname)

    def _read_rows():
        rows = []
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()[-500:]      # վերջին 500 տողը
        except FileNotFoundError:
            return rows
        for line in reversed(lines):             # նորերը վերևում
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                obj = {"Timestamp": "", "Severity": "", "Operation": "", "Message": line}
            # ── Default-ով ցույց ենք տալիս ՄԻԱՅՆ այս գործարկումից հետո գրվածը ──
            # (fix արված, էլ չկրկնվող error-ը restart-ից հետո ինքնաշխատ անհետանում է)
            if not show_all and obj.get("Timestamp"):
                try:
                    ts = datetime.strptime(obj["Timestamp"], "%Y-%m-%dT%H:%M:%S%z")
                    if ts < APP_START_TIME:
                        continue
                except Exception:
                    pass
            # level=Info / Warning / Error / Critical — ցույց է տալիս ՄԻԱՅՆ այդ մակարդակը
            if level_filter:
                _ALIASES = {"information": "info", "informational": "info"}

                def _norm(s):
                    s = (s or "").strip().lower()
                    return _ALIASES.get(s, s)

                want = _norm(level_filter)
                sev = _norm(obj.get("Severity"))
                if want == 'error':  # Error → Error + Critical (հին պահվածքը պահում ենք)
                    if sev not in ('error', 'critical'):
                        continue
                elif sev != want:  # մնացածը՝ ճշգրիտ համընկնում
                    continue
            known = {"Timestamp", "Severity", "Service", "Logger", "Message",
                     "Operation", "Traceback"}
            obj["extra"] = json.dumps(
                {k: v for k, v in obj.items() if k not in known and k != "extra"},
                ensure_ascii=False, indent=1
            )
            rows.append(obj)
        return rows

    rows = await asyncio.to_thread(_read_rows)
    return render_template_string(_LOG_VIEWER_HTML, rows=rows, file=which, token=provided, show_all=show_all)



@app.route('/logs/clear', methods=['POST'])
async def clear_logs():
    provided = request.args.get('token') or request.form.get('token') or request.headers.get('X-Log-Token', '')
    if not LOG_VIEW_TOKEN or provided != LOG_VIEW_TOKEN:
        return "Access denied", 403

    which = request.args.get('file') or request.form.get('file') or 'error'
    fname = 'error.log' if which == 'error' else 'app.log'
    path = os.path.join(LOG_DIR, fname)

    def _truncate():
        try:
            open(path, 'w', encoding='utf-8').close()   # ֆայլը դատարկում ենք (չենք ջնջում)
            return True
        except Exception:
            return False

    ok = await asyncio.to_thread(_truncate)
    logger.info("logs_cleared", extra={"Operation": "ClearLogs", "File": fname, "Success": ok})
    # հետ ենք վերադառնում նույն էջին
    return redirect(url_for('view_logs', file=which, token=provided))



def _client_log_rate_limited(key: str) -> bool:
    now = time.time()
    with _client_log_lock:
        dq = _client_log_hits[key]
        while dq and now - dq[0] > CLIENT_LOG_RATE_WINDOW:
            dq.popleft()
        if len(dq) >= CLIENT_LOG_RATE_LIMIT:
            return True
        dq.append(now)
        return False


async def aio_fetchone(db, sql, params=()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


class PrefixMiddleware:
    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def user_has_role(*names):
    cu = getattr(g, "current_user", None) or {}
    roles = cu.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    roles = {str(r).strip().lower() for r in roles}
    return any(n.lower() in roles for n in names)


def _deny(message, redirect_endpoint="my_crosswords"):
    """403 JSON API-ի համար, flash + redirect՝ սովորական էջերի համար:"""
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(error=message), 403
    flash(message)
    return redirect(url_for(redirect_endpoint))


def blocked_response(mode=None):
    """Ստուգում է՝ արդյոք ընթացիկ օգտատերը կարող է խաչբառ ստեղծել/խմբագրել:
    Հերթականությամբ ստուգվում է՝ անձնական արգելափակում, ստեղծելու գլոբալ
    կողպեք, ապա տվյալ ռեժիմի (``ai`` կամ ``keyword``) կողպեքը:
    Վերադարձնում է պատրաստի պատասխան, կամ None՝ եթե թույլատրված է:
    """
    cu = getattr(g, "current_user", None) or {}
    if is_user_blocked(cu.get("sub")):
        return _deny("Ձեր հաշիվը արգելափակված է ադմինիստրատորի կողմից:")

    settings = get_settings()
    if settings["creation_locked"]:
        return _deny("Խաչբառ ստեղծելը ժամանակավորապես անջատված է:")
    if mode == "ai" and settings["ai_creation_locked"]:
        return _deny("AI-ով խաչբառ ստեղծելը ժամանակավորապես անջատված է:")
    if mode == "keyword" and settings["keyword_creation_locked"]:
        return _deny("Keyword ռեժիմով ստեղծելը ժամանակավորապես անջատված է:")
    return None


def require_api_key_or_user(*roles):
    def decorator(f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            provided = request.headers.get("X-Api-Key", "")
            if INTERNAL_API_KEY and provided == INTERNAL_API_KEY:
                g.via_api_key = True
                return await f(*args, **kwargs)
            g.via_api_key = False
            return await require_auth(*roles)(f)(*args, **kwargs)

        return wrapper

    return decorator


# =====================================================
# 🔹 HELPERS
# =====================================================

def fetch_crossword_by_id_public(conn, id):
    return conn.execute(
        "SELECT * FROM crosswords WHERE id = ?", (id,)
    ).fetchone()


def ask_raw(message: str) -> str:
    if not X_API_KEY:
        raise RuntimeError("❌ API key չկա։ Խնդրում եմ `.env` ֆայլում ավելացրեք X_API_KEY")

    payload = {"message": message}
    headers = {
        "X-API-Key": X_API_KEY,
        "Content-Type": "application/json; charset=utf-8"
    }

    try:
        response = requests.post(
            API_URL, headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=API_TIMEOUT
        )
        response.encoding = "utf-8"
    except requests.RequestException as e:
        logger.error(
            "ask_raw_request_failed",
            extra={"Operation": "AskRaw", "ApiUrl": API_URL, "Exception": type(e).__name__},
            exc_info=True,
        )
        raise RuntimeError(f"❌ API-ի հետ կապի խնդիր: {e}")

    logger.info(
        "ask_raw_response_received",
        extra={"Operation": "AskRaw", "StatusCode": response.status_code, "ResponseLength": len(response.text or "")},
    )
    return response.text


def extract_answer_text(raw: str) -> str:
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "answer" in outer:
            return outer["answer"]
    except Exception:
        pass
    return raw


def clean_json_list(raw_content):
    clean = raw_content.replace('```json', '') \
        .replace('```', '') \
        .replace('`', '') \
        .strip()

    if '[' in clean and ']' in clean:
        start = clean.find('[')
        end = clean.rfind(']') + 1
        clean = clean[start:end]

    return json.loads(clean)


def parse_list_field(value):
    if not value:
        return []
    try:
        result = json.loads(value)
        raw = result if isinstance(result, list) else [result]
    except Exception:
        raw = [value]

    # Ամեն տարրը բաժանում ենք ստորակետով (եթե ներսում ստորակետ կա)
    items = []
    for it in raw:
        for part in str(it).split(','):
            part = part.strip()
            if part:
                items.append(part)
    return items


def normalize_to_json_list(value):
    if not value:
        return json.dumps([], ensure_ascii=False)

    # Հավաքում ենք raw տարրերը
    if isinstance(value, list):
        raw_items = value
    else:
        try:
            parsed = json.loads(value)
            raw_items = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            raw_items = [value]

    # Ամեն տարր բաժանում ենք ստորակետով ու մաքրում
    items = []
    for it in raw_items:
        for part in str(it).split(','):
            part = part.strip()
            if part:
                items.append(part)

    return json.dumps(items, ensure_ascii=False)


def normalize(text):
    return text.strip().upper()

def flash_skipped_words(crossword_data):
    """Ծանուցում է այն հարցերի մասին, որոնց պատասխանները չհատվեցին և դուրս մնացին։
    Տվյալը ուղարկում ենք JSON-ով՝ 'skipped' կատեգորիայով, որ էջը ցույց տա
    պատուհանով և կարմիրով նշի համապատասխան տողերը (ինչպես ստեղծման էջում)։"""
    skipped = (crossword_data or {}).get('skipped') or []
    if not skipped:
        return False
    payload = [
        {"q": s.get('question', '') or '', "a": (s.get('word', '') or '').upper()}
        for s in skipped
    ]
    flash(json.dumps(payload, ensure_ascii=False), "skipped")
    return True

# Մասնագիտական դաշտերի key → հայերեն պիտակ
CREATOR_FIELD_LABELS = {
    "photo": "Լուսանկար",
    "school": "ԴՊՐՈՑ",
    "subjects": "Առարկաներ",
    "digitlevel": "Տարակարգ",
    "voluntaryCertification": "Կամավոր ատեստավորում",
    "professionalAchievements": "Մասնագիտական նվաճումներ",
}


def fetch_profile_claims_sync(user_id):
    """Վերցնում է օգտվողի profile-claims-ը mainlogin-ից (firstName, lastName, email...)։"""
    try:
        resp = requests.get(profile_claims_url(user_id),
                            timeout=MAINLOGIN_TIMEOUT, verify=MAINLOGIN_VERIFY_SSL)
        data = resp.json()
        if isinstance(data, dict):
            return data.get("claims", data)
        return {}
    except Exception as e:
        # External service դեգրադացված է, բայց fallback (դատարկ dict) թույլ է տալիս
        # request-ին շարունակել → Warning, ոչ թե Error (PDF, գլուխ 3)
        logger.warning(
            "profile_claims_fetch_failed",
            extra={"Operation": "FetchProfileClaims", "UserId": user_id, "Exception": type(e).__name__},
        )
        return {}


def build_user_view(claims, profile=None):
    """Կազմում է մաքուր user dict՝ ճիշտ անունով, էլ-հասցեով, սկզբնատառերով և նկարով։"""
    claims = claims or {}
    profile = profile or {}

    def pick(src, *keys):
        for k in keys:
            v = src.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    first_name = pick(profile, "firstName", "FirstName") or pick(claims, "given_name", "firstName", "first_name")
    last_name = pick(profile, "lastName", "LastName") or pick(claims, "family_name", "lastName", "last_name")
    email = pick(profile, "email", "mail") or pick(claims, "email", "mail")

    name = (first_name + " " + last_name).strip()
    if not name:
        candidate = pick(claims, "name", "fullName", "display_name", "unique_name")
        name = "" if "@" in candidate else candidate
    if not name and email:
        name = email.split("@")[0]

    if first_name or last_name:
        initials = ((first_name[:1] if first_name else "") + (last_name[:1] if last_name else "")).upper()
    elif name:
        initials = "".join(p[:1] for p in name.split()[:2]).upper()
    else:
        initials = "ԳՀ"

    # ── profile image՝ բոլոր հնարավոր key-երը + լրիվ URL ───────────────
    img = pick(profile,
               "profileImagePath", "ProfileImagePath",
               "profileImage", "ProfileImage",
               "imagePath", "ImagePath",
               "avatar", "Avatar", "photo", "Photo", "picture", "Picture") \
          or pick(claims,
                  "picture", "profileImagePath", "ProfileImagePath", "avatar")

    if img:
        if img.startswith(("http://", "https://")):
            pass  # արդեն լրիվ URL է
        elif img.startswith("//"):
            img = "https:" + img  # protocol-relative
        elif img.startswith("/"):
            img = MAINLOGIN_BASE + img  # /uploads/x.jpg → լրիվ
        else:
            img = MAINLOGIN_BASE + "/" + img  # uploads/x.jpg → լրիվ

    out = dict(claims)
    out.update({
        "name": name,
        "email": email,
        "initials": initials or "ԳՀ",
        "firstName": first_name,
        "lastName": last_name,
        "profileImagePath": img,
    })
    return out


def current_user_view():
    """Վերադարձնում է մաքուր user dict ճիշտ անունով. քեշավորում է profile-ը session-ում։"""
    claims = getattr(g, "current_user", None)
    if not claims:
        return {}
    uid = str(claims.get("sub", ""))

    cache = session.get("profile_cache") or {}
    entry = cache.get(uid)

    # v2 = ֆիքսից հետո; հին քեշը (առանց _v) անտեսում ենք
    if not entry or entry.get("_v") != 2:
        profile = fetch_profile_claims_sync(uid)
        profile["_v"] = 2
        cache[uid] = profile
        session["profile_cache"] = cache
        session.modified = True
    else:
        profile = entry

    return build_user_view(claims, profile)


def is_demo_request():
    """True, եթե էջը բացվել է demo հասցեով (ըստ DEMO_HOST_MARKER-ի, օր.՝ «demo»)։"""
    try:
        host = (request.host or "").lower()
    except RuntimeError:          # request-ից դուրս (startup և այլն)
        return False
    return bool(DEMO_HOST_MARKER) and DEMO_HOST_MARKER in host


def public_urls():
    if is_demo_request():
        return MAINLOGIN_PUBLIC_DEMO, GATEWAY_PUBLIC_DEMO
    return MAINLOGIN_PUBLIC_MAIN, GATEWAY_PUBLIC_MAIN

@app.context_processor
def inject_user():
    mainlogin, gateway = public_urls()
    try:
        user = current_user_view()
    except Exception as e:
        logger.warning(
            "inject_user_failed",
            extra={"Operation": "InjectUserContext", "Exception": type(e).__name__},
        )
        user = {}
    return {
        "user": user,
        "MAINLOGIN_PUBLIC": mainlogin,
        "GATEWAY_PUBLIC": gateway,
    }
def select_questions_for_keyword(keyword, questions):
    selected = []
    for letter in keyword:
        matches = [
            q for q in questions
            if letter in q["a"] and q not in selected
        ]
        if not matches:
            return None
        matches.sort(key=lambda x: len(x["a"]), reverse=True)
        selected.append(matches[0])
    return selected


# =====================================================
# 🔹 DB SETUP HELPERS
# =====================================================
#
# Այս ֆունկցիաները կանչվում են ՄԻԱՅՆ app-ի startup-ի ժամանակ (տես __main__ բլոկը)։
# Եթե որևէ մեկը ձախողվում է, app-ը կարող է վատ վիճակում մեկնարկել (բացակայող
# սյունակ/աղյուսակ) → PDF-ի քաղաքականությամբ սա Critical է, ոչ թե Error, քանի որ
# խնդիրը համակարգային է և ազդում է ամբողջ service-ի աշխատանքի վրա։

def check_and_create_crossword_stats_table():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crossword_stats (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id          INTEGER UNIQUE NOT NULL,
                view_count            INTEGER DEFAULT 0,
                like_count            INTEGER DEFAULT 0,
                dislike_count         INTEGER DEFAULT 0,
                download_count        INTEGER DEFAULT 0,
                read_duration_seconds INTEGER DEFAULT 0,
                comment_count         INTEGER DEFAULT 0,
                print_count           INTEGER DEFAULT 0,
                share_count           INTEGER DEFAULT 0,
                FOREIGN KEY (crossword_id) REFERENCES crosswords (id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        logger.info("startup_table_ready", extra={"Operation": "StartupDbInit", "Table": "crossword_stats"})
    except Exception as e:
        log_critical_startup_failure(
            logger, "startup_table_init_failed", exception=e,
            Operation="StartupDbInit", Table="crossword_stats",
        )
    finally:
        conn.close()


# ── ՆՈՐ: share_count column (հին բազաների համար) ──────────────────────────────
def check_and_add_share_count_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crossword_stats ADD COLUMN share_count INTEGER DEFAULT 0;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crossword_stats", "Column": "share_count"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crossword_stats", Column="share_count",
            )
    finally:
        conn.close()


def check_and_create_comments_table():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER NOT NULL,
                name         TEXT,
                comment_text TEXT NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (crossword_id) REFERENCES crosswords (id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        logger.info("startup_table_ready", extra={"Operation": "StartupDbInit", "Table": "comments"})
    except Exception as e:
        log_critical_startup_failure(
            logger, "startup_table_init_failed", exception=e,
            Operation="StartupDbInit", Table="comments",
        )
    finally:
        conn.close()


# ── ՆՈՐ: vote_feedback table (հավանել/չհավանելու պատճառներ) ──────────────────
def check_and_create_vote_feedback_table():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vote_feedback (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER NOT NULL,
                vote_type    TEXT NOT NULL,      -- 'like' / 'dislike'
                reasons      TEXT,               -- JSON ցանկ (like-ի դեպքում)
                other_text   TEXT,               -- "Այլ" տեքստ (like)
                message      TEXT,               -- դժգոհության հաղորդագրություն (dislike)
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (crossword_id) REFERENCES crosswords (id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        logger.info("startup_table_ready", extra={"Operation": "StartupDbInit", "Table": "vote_feedback"})
    except Exception as e:
        log_critical_startup_failure(
            logger, "startup_table_init_failed", exception=e,
            Operation="StartupDbInit", Table="vote_feedback",
        )
    finally:
        conn.close()


def check_and_add_voter_id_column():
    """vote_feedback-ին ավելացնում է voter_id և եզակի ինդեքս՝ մեկ մարդ = մեկ քվե։"""
    conn = sqlite3.connect(DATABASE)
    try:
        try:
            conn.execute("ALTER TABLE vote_feedback ADD COLUMN voter_id TEXT;")
            logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "vote_feedback", "Column": "voter_id"})
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        # Հին գրառումները (voter_id = NULL) ինդեքսից դուրս են մնում
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_vote_feedback_once
            ON vote_feedback (crossword_id, vote_type, voter_id)
            WHERE voter_id IS NOT NULL
        """)
        conn.commit()
    except Exception as e:
        log_critical_startup_failure(
            logger, "startup_column_add_failed", exception=e,
            Operation="StartupDbInit", Table="vote_feedback", Column="voter_id",
        )
    finally:
        conn.close()


def check_and_add_created_at_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "created_at"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="created_at",
            )
    finally:
        conn.close()


def check_and_add_total_words_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE results ADD COLUMN total_words INTEGER;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "results", "Column": "total_words"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="results", Column="total_words",
            )
    finally:
        conn.close()


def check_and_create_participants_table():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER NOT NULL,
                name         TEXT NOT NULL,
                surname      TEXT NOT NULL,
                gender       TEXT NOT NULL,
                school       TEXT,
                grade        TEXT,
                start_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL,
                crossword_id   INTEGER NOT NULL,
                score          INTEGER,
                duration       INTEGER,
                is_completed   INTEGER DEFAULT 0,
                end_time       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (participant_id) REFERENCES participants (id),
                FOREIGN KEY (crossword_id)   REFERENCES crosswords (id)
            )
        """)
        conn.commit()
        logger.info("startup_table_ready", extra={"Operation": "StartupDbInit", "Table": "participants,results"})
    except Exception as e:
        log_critical_startup_failure(
            logger, "startup_table_init_failed", exception=e,
            Operation="StartupDbInit", Table="participants,results",
        )
    finally:
        conn.close()


def check_and_add_hidden_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN hidden INTEGER DEFAULT 0;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "hidden"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="hidden",
            )
    finally:
        conn.close()


def check_and_add_creator_name_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN creator_name TEXT;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "creator_name"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="creator_name",
            )
    finally:
        conn.close()


def check_and_add_is_ai_generated_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN is_ai_generated INTEGER DEFAULT 0;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "is_ai_generated"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="is_ai_generated",
            )
    finally:
        conn.close()


def check_and_add_keyword_material_columns():
    conn = sqlite3.connect(DATABASE)
    for col in ["keyword_material_type TEXT", "keyword_material_value TEXT"]:
        try:
            conn.execute(f"ALTER TABLE crosswords ADD COLUMN {col}")
            conn.commit()
            logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": col})
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                log_critical_startup_failure(
                    logger, "startup_column_add_failed", exception=e,
                    Operation="StartupDbInit", Table="crosswords", Column=col,
                )
    conn.close()


# ── ՆՈՐ: show_creator_data column ────────────────────────────────────────────
def check_and_add_show_creator_data_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN show_creator_data TEXT DEFAULT 'no';")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "show_creator_data"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="show_creator_data",
            )
    finally:
        conn.close()

def check_and_add_is_draft_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN is_draft INTEGER DEFAULT 0;")
        conn.commit()
        logger.info("startup_column_added", extra={"Operation": "StartupDbInit", "Table": "crosswords", "Column": "is_draft"})
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            log_critical_startup_failure(
                logger, "startup_column_add_failed", exception=e,
                Operation="StartupDbInit", Table="crosswords", Column="is_draft",
            )
    finally:
        conn.close()


@app.route('/api/creator-claims/<user_id>')
async def creator_claims(user_id):
    try:
        url = profile_claims_url(user_id)
        resp = await asyncio.to_thread(
            lambda: requests.get(url, timeout=MAINLOGIN_TIMEOUT, verify=MAINLOGIN_VERIFY_SSL)
        )
        try:
            return jsonify(resp.json()), resp.status_code
        except Exception:
            return jsonify({"error": "Invalid response", "raw": resp.text[:300]}), resp.status_code
    except Exception as e:
        logger.error(
            "creator_claims_fetch_failed",
            extra={"Operation": "CreatorClaims", "UserId": user_id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"error": str(e)}), 500


# ── Հեղինակի տվյալները՝ ՄԻԱՅՆ այն դաշտերը, որոնք հեղինակը թույլ է տվել ──
CREATOR_ALWAYS_KEYS = {"firstName", "lastName", "FirstName", "LastName", "description"}
CREATOR_FIELD_ALIASES = {
    # show_creator_data-ի key → mainlogin-ի claims-ում հնարավոր անունները
    "photo": {"photo", "Photo", "profileImagePath", "ProfileImagePath",
              "picture", "Picture", "avatar", "Avatar"},
    "professionalAchievements": {"professionalAchievements", "achievements"},
}


@app.route('/api/crosswords/<int:id>/creator')
async def crossword_creator(id):
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)
    if not cw:
        return jsonify({"claims": {}}), 404
    cw = dict(cw)

    raw = cw.get('show_creator_data') or 'no'
    if raw == 'no':
        return jsonify({"claims": {}})

    if raw == 'yes':
        allowed_keys = None  # հին խաչբառեր՝ բոլորը
    else:
        try:
            fields = json.loads(raw)
        except Exception:
            return jsonify({"claims": {}})
        allowed_keys = set(CREATOR_ALWAYS_KEYS)
        for f in fields:
            allowed_keys.add(f)
            allowed_keys |= CREATOR_FIELD_ALIASES.get(f, set())

    claims = await asyncio.to_thread(fetch_profile_claims_sync, cw.get('user_id'))
    if allowed_keys is not None:
        claims = {k: v for k, v in claims.items() if k in allowed_keys}

    return jsonify({"claims": claims})

@app.route('/debug_scd/<int:id>')
async def debug_scd(id):
    if not FLASK_DEBUG:
        return "Not found", 404
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)
    if not cw:
        return "not found", 404
    cw = dict(cw)
    return jsonify({"show_creator_data": cw.get("show_creator_data")})


# =====================================================
# 🔹 API — CROSSWORDS LIST
# =====================================================

@app.route('/api/users/<user_id>/crosswords', methods=['GET'])
@require_api_key_or_user()
async def api_crosswords(user_id):
    if not g.get("via_api_key", False) and str(g.current_user["sub"]) != str(user_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    public_base = PUBLIC_BASE_URL or (request.host_url.rstrip("/") + "/crossword")

    # published (default) | draft | all
    status = request.args.get('status', 'all')
    draft_filter = ""
    if status == 'published':
        draft_filter = "AND COALESCE(c.is_draft, 0) = 0"
    elif status == 'draft':
        draft_filter = "AND COALESCE(c.is_draft, 0) = 1"
    # all → առանց զտիչի

    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            rows = await db.execute_fetchall(f"""
                SELECT
                    c.id, c.title, c.keyword, c.is_ai_generated, c.is_draft, c.data,
                    c.subject, c.grade, c.created_at,
                    COALESCE(cs.view_count,     0) AS view_count,
                    COALESCE(cs.like_count,     0) AS like_count,
                    COALESCE(cs.download_count, 0) AS download_count
                FROM crosswords c
                LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                WHERE c.user_id = ? {draft_filter}
                ORDER BY c.id DESC
            """, (user_id,))

            result = []
            for row in rows:
                cw = dict(row)

                # ← ԱԲ-ի տեսակը data JSON-ից
                try:
                    ai_kind = (json.loads(cw.get("data") or "{}")).get("ai_kind")
                except Exception:
                    ai_kind = None

                if not cw["is_ai_generated"]:
                    creation_label = "Ինքնուրույն"
                elif ai_kind == "other_ai":
                    creation_label = "ԱՅԼ ԱԲ"
                else:
                    creation_label = "ԴՇՀ ԱԲ"  # default (նաև հին AI խաչբառերի համար)

                is_draft = bool(cw.get("is_draft"))
                result.append({
                    "id": str(cw["id"]),
                    "title": cw["title"],
                    "keyword": cw["keyword"],
                    "isAiGenerated": bool(cw["is_ai_generated"]),
                    "creationLabel": creation_label,
                    "aiKind": ai_kind,
                    "isDraft": is_draft,
                    # ── ՆՈՐ ──────────────────────────────────────────
                    "status": "draft" if is_draft else "published",
                    "statusLabel": "Սևագիր" if is_draft else "Ավարտված",
                    # սևագրի դեպքում՝ ուղիղ edit-ը, ավարտվածի՝ սովորական view-ն
                    "url": f"{public_base}/edit/{cw['id']}" if is_draft
                    else f"{public_base}/{cw['id']}",
                    # ──────────────────────────────────────────────────
                    "subjects": parse_list_field(cw.get("subject")),
                    "grades": parse_list_field(cw.get("grade")),
                    "createdAt": cw.get("created_at"),
                    "viewCount": cw.get("view_count", 0),
                    "likeCount": cw.get("like_count", 0),
                    "downloadCount": cw.get("download_count", 0),
                    "downloadUrl": f"{public_base}/download/{cw['id']}",
                    "externalUrl": f"{public_base}/{cw['id']}",
                    "editUrl": f"{public_base}/edit/{cw['id']}",
                })

            return jsonify(result)

    except Exception as e:
        logger.error(
            "api_crosswords_list_failed",
            extra={"Operation": "ListUserCrosswords", "UserId": user_id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# =====================================================
# 🔹 API — CROSSWORD STATS
# =====================================================

@app.route('/api/crosswords/<int:id>/stats', methods=['GET'])
@require_api_key_or_user()
async def api_crossword_stats(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            row = await aio_fetchone(db, """
                SELECT view_count, like_count, dislike_count,
                       download_count, read_duration_seconds,
                       comment_count, print_count, share_count
                FROM crossword_stats
                WHERE crossword_id = ?
            """, (id,))

            if not row:
                return jsonify({
                    "viewCount": 0,
                    "likeCount": 0,
                    "dislikeCount": 0,
                    "downloadCount": 0,
                    "readDurationSeconds": 0,
                    "commentCount": 0,
                    "printCount": 0,
                    "shareCount": 0
                })

            s = dict(row)
            return jsonify({
                "viewCount": s.get("view_count", 0),
                "likeCount": s.get("like_count", 0),
                "dislikeCount": s.get("dislike_count", 0),
                "downloadCount": s.get("download_count", 0),
                "readDurationSeconds": s.get("read_duration_seconds", 0),
                "commentCount": s.get("comment_count", 0),
                "printCount": s.get("print_count", 0),
                "shareCount": s.get("share_count", 0)
            })

    except Exception as e:
        logger.error(
            "crossword_stats_fetch_failed",
            extra={"Operation": "GetCrosswordStats", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500



@app.route('/api/crosswords/<int:id>/print', methods=['POST'])
async def api_crossword_print(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO crossword_stats (crossword_id, print_count)
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET print_count = print_count + 1
            """, (id,))
            await db.commit()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(
            "crossword_print_count_failed",
            extra={"Operation": "CrosswordPrintCount", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/crosswords/<int:id>/download', methods=['POST'])
async def api_crossword_download_count(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO crossword_stats (crossword_id, download_count)
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET download_count = download_count + 1
            """, (id,))
            await db.commit()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(
            "crossword_download_count_failed",
            extra={"Operation": "CrosswordDownloadCount", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# ── ՆՈՐ: կիսվելը հաշվելու endpoint ────────────────────────────────────────────
@app.route('/api/crosswords/<int:id>/share', methods=['POST'])
async def api_crossword_share(id):
    if get_settings()["sharing_locked"]:
        return jsonify({"success": False,
                        "error": "Կիսվելը ժամանակավորապես անջատված է:"}), 403

    try:
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO crossword_stats (crossword_id, share_count)
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET share_count = share_count + 1
            """, (id,))
            await db.commit()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(
            "crossword_share_count_failed",
            extra={"Operation": "CrosswordShareCount", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/crosswords/<int:id>/vote', methods=['POST'])
async def api_crossword_vote(id):
    try:
        data = request.get_json(silent=True) or {}
        vote_type = data.get('type')
        action = data.get('action')

        if vote_type not in ('like', 'dislike') or action not in ('add', 'remove'):
            return jsonify({"success": False, "error": "Invalid params"}), 400

        col = 'like_count' if vote_type == 'like' else 'dislike_count'
        delta = 1 if action == 'add' else -1

        async with aiosqlite.connect(DATABASE) as db:
            await db.execute(f"""
                INSERT INTO crossword_stats (crossword_id, {col})
                VALUES (?, ?)
                ON CONFLICT(crossword_id) DO UPDATE
                SET {col} = MAX(0, {col} + ?)
            """, (id, max(0, delta), delta))
            await db.commit()

        return jsonify({"success": True})

    except Exception as e:
        logger.error(
            "crossword_vote_failed",
            extra={"Operation": "CrosswordVote", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


VOTER_COOKIE = "cw_voter"
VOTER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2   # 2 տարի


def _get_or_create_voter_id():
    """Վերադարձնում է (voter_id, is_new)։ ID-ն պահվում է cookie-ում։"""
    vid = (request.cookies.get(VOTER_COOKIE) or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", vid):
        return vid, False
    return uuid.uuid4().hex, True


def _attach_voter_cookie(resp, voter_id, is_new):
    if is_new:
        resp.set_cookie(
            VOTER_COOKIE, voter_id,
            max_age=VOTER_COOKIE_MAX_AGE,
            httponly=True, samesite="Lax",
            secure=request.is_secure,
        )
    return resp

# ── ՆՈՐ: հավանել/չհավանելու կարծիք պահելու endpoint ──────────────────────────
@app.route('/api/crosswords/<int:id>/feedback', methods=['POST'])
async def api_crossword_feedback(id):
    data = request.get_json(silent=True) or {}
    vote_type = data.get('type')
    if vote_type not in ('like', 'dislike'):
        return jsonify({"success": False, "error": "Invalid type"}), 400

    reasons = data.get('reasons') or []
    reasons_json = json.dumps(reasons, ensure_ascii=False) if isinstance(reasons, list) else '[]'
    other = (data.get('other') or '').strip()[:1000]
    message = (data.get('message') or '').strip()[:2000]

    col = 'like_count' if vote_type == 'like' else 'dislike_count'
    voter_id, is_new = _get_or_create_voter_id()

    def already_voted_response():
        resp = jsonify({"success": False, "alreadyVoted": True,
                        "error": "Արդեն քվեարկել եք այս խաչբառի համար"})
        return _attach_voter_cookie(resp, voter_id, is_new), 409

    try:
        async with aiosqlite.connect(DATABASE) as db:
            exists = await aio_fetchone(db, """
                SELECT 1 FROM vote_feedback
                WHERE crossword_id = ? AND vote_type = ? AND voter_id = ?
            """, (id, vote_type, voter_id))
            if exists:
                logger.info("vote_already_exists",
                            extra={"Operation": "SaveCrosswordFeedback", "CrosswordId": id, "VoteType": vote_type})
                return already_voted_response()

            try:
                # ՄԵԿ քվե = ՄԵԿ տող (կետերը՝ reasons JSON-ում)
                await db.execute("""
                    INSERT INTO vote_feedback (crossword_id, vote_type, reasons, other_text, message, voter_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (id, vote_type, reasons_json, other, message, voter_id))
            except sqlite3.IntegrityError:
                # Երկու միաժամանակյա հարցում. եզակի ինդեքսը երկրորդը կանգնեցրեց
                return already_voted_response()

            await db.execute(f"""
                INSERT INTO crossword_stats (crossword_id, {col})
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET {col} = {col} + 1
            """, (id,))
            await db.commit()

        resp = jsonify({"success": True})
        return _attach_voter_cookie(resp, voter_id, is_new)

    except Exception as e:
        logger.error(
            "crossword_feedback_save_failed",
            extra={"Operation": "SaveCrosswordFeedback", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# ── ՆՈՐ: հեղինակի համար՝ կարծիքները կարդալու endpoint ─────────────────────────
@app.route('/api/crosswords/<int:id>/feedback', methods=['GET'])
@require_api_key_or_user()
async def api_get_feedback(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            rows = await db.execute_fetchall("""
                SELECT id, vote_type, reasons, other_text, message, created_at
                FROM vote_feedback
                WHERE crossword_id = ?
                ORDER BY id DESC
            """, (id,))

            result = []
            for r in rows:
                try:
                    reasons = json.loads(r["reasons"]) if r["reasons"] else []
                except Exception:
                    reasons = []
                result.append({
                    "id": r["id"],
                    "type": r["vote_type"],
                    "reasons": reasons,
                    "other": r["other_text"] or "",
                    "message": r["message"] or "",
                    "createdAt": r["created_at"],
                })
            return jsonify(result)
    except Exception as e:
        logger.error(
            "crossword_feedback_read_failed",
            extra={"Operation": "GetCrosswordFeedback", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500

# ── ՆՈՐ: հանրային endpoint՝ հավանել/չհավանելու քանակները ցույց տալու համար ──
@app.route('/api/crosswords/<int:id>/feedback-counts', methods=['GET'])
async def api_crossword_feedback_counts(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            row = await aio_fetchone(db, """
                SELECT COALESCE(like_count, 0)    AS likes,
                       COALESCE(dislike_count, 0) AS dislikes
                FROM crossword_stats
                WHERE crossword_id = ?
            """, (id,))
            if not row:
                return jsonify({"likes": 0, "dislikes": 0})
            return jsonify({"likes": row["likes"], "dislikes": row["dislikes"]})
    except Exception as e:
        logger.warning(
            "crossword_feedback_counts_failed",
            extra={"Operation": "GetCrosswordFeedbackCounts", "CrosswordId": id, "RequestId": g.get("request_id"), "Exception": type(e).__name__},
        )
        return jsonify({"likes": 0, "dislikes": 0, "error": str(e)}), 200


# =====================================================
# 🔹 API — COMMENTS
# =====================================================

@app.route('/api/crosswords/<int:id>/comments', methods=['GET'])
async def api_get_comments(id):
    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row
            rows = await db.execute_fetchall("""
                SELECT id, name, comment_text, created_at
                FROM comments
                WHERE crossword_id = ?
                ORDER BY id DESC
            """, (id,))
            return jsonify([
                {
                    "id": r["id"],
                    "name": r["name"] or "Անանուն",
                    "comment": r["comment_text"],
                    "createdAt": r["created_at"],
                }
                for r in rows
            ])
    except Exception as e:
        logger.error(
            "comments_fetch_failed",
            extra={"Operation": "GetComments", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/crosswords/<int:id>/comments', methods=['POST'])
async def api_add_comment(id):
    if get_settings()["comments_locked"]:
        return jsonify({"success": False,
                        "error": "Մեկնաբանելը ժամանակավորապես անջատված է:"}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'Անանուն').strip()[:60]
    text = (data.get('comment') or data.get('text') or '').strip()[:1000]

    if not text:
        return jsonify({"success": False, "error": "Մեկնաբանությունը դատարկ է"}), 400

    created_at = datetime.now(AM_TZ).strftime('%Y-%m-%d %H:%M:%S')  # ← ՆՈՐ

    try:
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                "INSERT INTO comments (crossword_id, name, comment_text, created_at) VALUES (?, ?, ?, ?)",
                (id, name, text, created_at)  # ← created_at-ը բացահայտ
            )
            comment_id = cursor.lastrowid

            await db.execute("""
                INSERT INTO crossword_stats (crossword_id, comment_count)
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET comment_count = comment_count + 1
            """, (id,))
            await db.commit()

        return jsonify({"success": True, "id": comment_id}), 200
    except Exception as e:
        logger.error(
            "comment_add_failed",
            extra={"Operation": "AddComment", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# =====================================================
# 🔹 API — DELETE CROSSWORD
# =====================================================

@app.route('/api/crosswords/<int:id>', methods=['DELETE'])
@require_api_key_or_user()
async def api_delete_crossword(id):
    via_key = g.get("via_api_key", False)
    user_id = None if via_key else g.current_user["sub"]

    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            if via_key:
                row = await aio_fetchone(db, "SELECT id FROM crosswords WHERE id = ?", (id,))
            else:
                row = await aio_fetchone(db,
                                         "SELECT id FROM crosswords WHERE id = ? AND user_id = ?",
                                         (id, user_id)
                                         )

            if not row:
                return jsonify({"success": False, "error": "Not found or access denied"}), 404

            await db.execute("DELETE FROM crossword_stats WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM comments     WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM vote_feedback WHERE crossword_id = ?", (id,))  # ← ՆՈՐ
            await db.execute("DELETE FROM results      WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM participants WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM questions    WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM crosswords   WHERE id = ?", (id,))
            await db.commit()

            logger.info(
                "crossword_deleted",
                extra={"Operation": "DeleteCrossword", "CrosswordId": id, "UserId": user_id, "ViaApiKey": via_key, "RequestId": g.get("request_id")},
            )
            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(
            "crossword_delete_failed",
            extra={"Operation": "DeleteCrossword", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# =====================================================
# 🔹 API — USER STATS BY PERIOD
# =====================================================

@app.route('/api/users/<user_id>/stats/<period>', methods=['GET'])
@require_api_key_or_user()
async def api_user_stats(user_id, period):
    if not g.get("via_api_key", False) and str(g.current_user["sub"]) != str(user_id):
        return jsonify({"success": False, "error": "Access denied"}), 403

    period_days = {
        "7d": 7,
        "month": 30,
        "3m": 90,
        "year": 365,
        "all": None
    }

    if period not in period_days:
        return jsonify({"success": False, "error": "Invalid period. Use: 7d, month, 3m, year, all"}), 400

    days = period_days[period]

    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            if days:
                total_row = await aio_fetchone(db, """
                    SELECT COUNT(*) AS cnt FROM crosswords
                    WHERE user_id = ?
                    AND created_at >= datetime('now', ? || ' days')
                """, (user_id, f"-{days}"))
            else:
                total_row = await aio_fetchone(db,
                                               "SELECT COUNT(*) AS cnt FROM crosswords WHERE user_id = ?",
                                               (user_id,)
                                               )
            total_materials = total_row["cnt"] if total_row else 0

            if days:
                cur_rows = await db.execute_fetchall("""
                    SELECT
                        COALESCE(SUM(cs.view_count),  0) AS total_views,
                        COALESCE(SUM(cs.like_count),  0) AS total_likes,
                        COALESCE(SUM(cs.print_count), 0) AS total_prints,
                        COALESCE(SUM(cs.share_count), 0) AS total_shares
                    FROM crosswords c
                    LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                    WHERE c.user_id = ?
                    AND c.created_at >= datetime('now', ? || ' days')
                """, (user_id, f"-{days}"))
            else:
                cur_rows = await db.execute_fetchall("""
                    SELECT
                        COALESCE(SUM(cs.view_count),  0) AS total_views,
                        COALESCE(SUM(cs.like_count),  0) AS total_likes,
                        COALESCE(SUM(cs.print_count), 0) AS total_prints,
                        COALESCE(SUM(cs.share_count), 0) AS total_shares
                    FROM crosswords c
                    LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                    WHERE c.user_id = ?
                """, (user_id,))

            current = dict(cur_rows[0]) if cur_rows else {
                "total_views": 0, "total_likes": 0, "total_prints": 0, "total_shares": 0
            }

            if days:
                prev_rows = await db.execute_fetchall("""
                    SELECT
                        COALESCE(SUM(cs.view_count),  0) AS total_views,
                        COALESCE(SUM(cs.like_count),  0) AS total_likes,
                        COALESCE(SUM(cs.print_count), 0) AS total_prints,
                        COALESCE(SUM(cs.share_count), 0) AS total_shares
                    FROM crosswords c
                    LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                    WHERE c.user_id = ?
                    AND c.created_at >= datetime('now', ? || ' days')
                    AND c.created_at <  datetime('now', ? || ' days')
                """, (user_id, f"-{days * 2}", f"-{days}"))

                prev = dict(prev_rows[0]) if prev_rows else {
                    "total_views": 0, "total_likes": 0, "total_prints": 0, "total_shares": 0
                }
            else:
                prev = {"total_views": 0, "total_likes": 0, "total_prints": 0, "total_shares": 0}

            monthly_views = [0] * 12
            monthly_likes = [0] * 12

            monthly_rows = await db.execute_fetchall("""
                SELECT
                    CAST(strftime('%m', c.created_at) AS INTEGER) - 1 AS month_idx,
                    COALESCE(SUM(cs.view_count), 0) AS views,
                    COALESCE(SUM(cs.like_count), 0) AS likes
                FROM crosswords c
                LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                WHERE c.user_id = ?
                AND strftime('%Y', c.created_at) = strftime('%Y', 'now')
                GROUP BY month_idx
            """, (user_id,))

            for row in monthly_rows:
                idx = row["month_idx"]
                if 0 <= idx <= 11:
                    monthly_views[idx] = row["views"]
                    monthly_likes[idx] = row["likes"]

            top_rows = await db.execute_fetchall("""
                SELECT c.id, c.title, COALESCE(cs.view_count, 0) AS view_count
                FROM crosswords c
                LEFT JOIN crossword_stats cs ON cs.crossword_id = c.id
                WHERE c.user_id = ?
                ORDER BY view_count DESC
                LIMIT 5
            """, (user_id,))

            top_materials = [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "viewCount": r["view_count"]
                }
                for r in top_rows
            ]

            return jsonify({
                "totalMaterials": total_materials,
                "totalViews": current["total_views"],
                "totalLikes": current["total_likes"],
                "totalPrints": current["total_prints"],
                "totalShares": current["total_shares"],
                "previousPeriodViews": prev["total_views"],
                "previousPeriodLikes": prev["total_likes"],
                "previousPeriodPrints": prev["total_prints"],
                "previousPeriodShares": prev["total_shares"],
                "monthlyViews": monthly_views,
                "monthlyLikes": monthly_likes,
                "topMaterials": top_materials,
                "regionStats": []
            })

    except Exception as e:
        logger.error(
            "user_stats_failed",
            extra={"Operation": "GetUserStats", "UserId": user_id, "Period": period, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500


# =====================================================
# 🔹 AI ROUTES
# =====================================================

@app.route('/generate_ai_crossword', methods=['POST'])
@require_auth()
async def generate_ai_crossword():
    denied = blocked_response(mode="ai")
    if denied:
        return denied

    ai_model = request.form.get("ai_model", DEFAULT_AI_MODEL)
    topic = request.form.get('topic', 'Ընդհանուր')
    creator_name = request.form.get('creator_name', 'AI Assistant')
    keyword_mode = request.form.get("keyword_mode")
    description = request.form.get("description", "").strip()
    question_count = int(request.form.get("question_count", 10))
    question_count = max(2, min(40, question_count))
    manual_keyword = normalize(request.form.get("keyword", "").strip())
    creator_fields = request.form.getlist('creator_fields')
    if not user_has_role("teacher"):
        creator_fields = []
    show_creator_data = json.dumps(creator_fields, ensure_ascii=False) if creator_fields else 'no'

    try:
        provider = get_provider(ai_model)
        if keyword_mode != "with":
            # Ավելի շատ ենք խնդրում՝ կրկնվող պատասխանները հեռացնելուց հետո count-ը լրանա
            raw_questions = await provider.generate_questions(topic, question_count + 8, description)

            seen = set()
            formatted_questions = []
            for q in raw_questions:
                ans = normalize(q["a"])
                if not ans or ans in seen:
                    continue
                seen.add(ans)
                formatted_questions.append(q)
                if len(formatted_questions) >= question_count:
                    break

            keyword = ""
            crossword_data = generate_crossword_grid(formatted_questions, keyword)
            if not crossword_data:
                raise ValueError("Grid generation failed")

        else:
            formatted_questions = await provider.generate_questions(topic, question_count + 8, description)
            if len(formatted_questions) < max(5, question_count // 2):
                raise ValueError("Բավարար բառեր չկան")

            if manual_keyword:
                formatted_questions = [
                    q for q in formatted_questions
                    if normalize(q["a"]) != manual_keyword
                ]

            if manual_keyword:
                selected_questions = select_questions_for_keyword(manual_keyword, formatted_questions)
                if not selected_questions:
                    raise ValueError("Բանալի բառին համապատասխան հարցեր չգտնվեցին")
                grid = generate_crossword_grid(selected_questions, manual_keyword)
                if not grid:
                    raise ValueError("Չհաջողվեց կառուցել տրված բանալի բառով")
                crossword_data = grid
                keyword = manual_keyword
                formatted_questions = selected_questions

            else:
                normalized_topic = normalize(topic)
                candidates = list(set([
                    normalize(q["a"])
                    for q in formatted_questions
                    if 4 <= len(q["a"]) <= 7 and normalize(q["a"]) != normalized_topic
                ]))
                if not candidates:
                    raise ValueError("Հարմար բանալի բառ չգտնվեց")

                success = False
                crossword_data = None
                keyword = ""

                for candidate in sorted(candidates, key=len, reverse=True):
                    filtered = [q for q in formatted_questions if normalize(q["a"]) != candidate]
                    selected = select_questions_for_keyword(candidate, filtered)
                    if not selected:
                        continue
                    grid = generate_crossword_grid(selected, candidate)
                    if grid:
                        crossword_data = grid
                        keyword = candidate
                        formatted_questions = selected
                        success = True
                        break

                if not success:
                    raise ValueError("Չհաջողվեց կառուցել բանալի բառով խաչբառ")

        # ← ai_kind-ը գրում ենք ԲՈԼՈՐ ճյուղերի համար (if/else-ից դուրս, try-ի ներսում)
        # OTHER_AI_MODELS-ը՝ .env-ից (օր.՝ OTHER_AI_MODELS=claude,gpt)
        crossword_data['ai_model'] = ai_model
        crossword_data['ai_kind'] = 'other_ai' if (ai_model or '').lower() in OTHER_AI_MODELS else 'dshh_ai'
        logger.info(
            "ai_crossword_data_prepared",
            extra={
                "Operation": "GenerateAiCrossword",
                "UserId": g.current_user["sub"],
                "AiModel": ai_model,
                "AiKind": crossword_data['ai_kind'],
                "Topic": topic,
                "QuestionCount": len(formatted_questions),
            },
        )

        crossword_id = await asyncio.to_thread(
            db_operation_wrapper,
            insert_new_crossword_db,
            g.current_user["sub"],
            topic,
            keyword,
            "",
            crossword_data,
            f"Թեմա՝ {topic}",
            creator_name,
            ["AI Generated"],
            ["Բոլոր"],
            1,  # is_ai
            show_creator_data,
            1  # is_draft → սևագիր, մինչև ուսուցիչը չպահպանի խմբագրման էջում
        )

        logger.info(
            "ai_crossword_created",
            extra={
                "Operation": "GenerateAiCrossword",
                "UserId": g.current_user["sub"],
                "CrosswordId": crossword_id,
                "AiModel": ai_model,
                "RequestId": g.get("request_id"),
            },
        )

        return redirect(url_for('edit_crossword', id=crossword_id, ai=1))

    except Exception as e:
        # Կոնկրետ request-ի ձախողում, app-ը շարունակում է աշխատել → Error, ոչ թե Critical
        logger.error(
            "ai_crossword_generation_failed",
            extra={
                "Operation": "GenerateAiCrossword",
                "UserId": g.current_user.get("sub") if getattr(g, "current_user", None) else None,
                "AiModel": ai_model,
                "Topic": topic,
                "RequestId": g.get("request_id"),
            },
            exc_info=True,
        )
        if "credit balance" in str(e).lower() or "authentication" in str(e).lower():
            flash("ԱԲ ծառայությունը ժամանակավորապես հասանելի չէ։ Փորձեք ավելի ուշ։", "danger")
        else:
            flash("Խաչբառի ստեղծման ժամանակ առաջացավ սխալ։", "danger")
        return redirect(url_for('choose_keyword_mode'))


@app.route('/api/ai-questions-for-edit', methods=['POST'])
@require_auth()
async def ai_questions_for_edit():
    data = request.get_json(silent=True) or {}
    topic = (data.get('topic') or '').strip()
    subject = data.get('subject', '')
    grade = data.get('grade', '')
    try:
        count = int(data.get('count', 3) or 3)
    except (TypeError, ValueError):
        count = 3
    count = max(1, min(40, count))
    existing = data.get('existing') or []

    if not topic:
        return jsonify({"error": "Թեմա չի նշված"}), 400

    full_topic = topic
    if subject:
        full_topic += f" ({subject})"
    if grade:
        full_topic += f" {grade}-րդ դասարան"

    def clean_answer(s):
        # Նույն կանոնը, ինչ frontend-ում՝ միայն հայերեն/լատիներեն մեծատառեր
        return re.sub(r'[^Ա-ՖA-ZА-ЯЁ]', '', (s or '').strip().upper())

    seen = set()
    for x in existing:
        if isinstance(x, dict):
            a = clean_answer(x.get('answer') or x.get('a'))
            if a:
                seen.add(a)

    result = []
    try:
        provider = get_provider(DEFAULT_AI_MODEL)
        # Մինչև 3 փորձ՝ մինչև հավաքվի պահանջված քանակը
        for _ in range(3):
            need = count - len(result)
            if need <= 0:
                break
            pairs = await provider.generate_questions(full_topic, need + 3)
            for p in pairs or []:
                q = (p.get('q') or p.get('question') or '').strip()
                a = clean_answer(p.get('a') or p.get('answer'))
                if not q or len(a) < 2 or a in seen:
                    continue
                seen.add(a)
                result.append({"question": q, "answer": a})
                if len(result) >= count:
                    break

        return jsonify({"pairs": result[:count], "requested": count})

    except Exception as e:
        logger.error(
            "ai_questions_for_edit_failed",
            extra={"Operation": "AiQuestionsForEdit", "Topic": topic, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        # Եթե մինչ սխալը ինչ-որ բան հավաքվել է, վերադարձնում ենք
        if result:
            return jsonify({"pairs": result[:count], "requested": count})
        return jsonify({"error": str(e)}), 500




@app.route('/api/ai-edit-crossword', methods=['POST'])
@require_auth()
async def ai_edit_crossword():
    data = request.get_json(silent=True) or {}
    qa_pairs = data.get("qa_pairs", [])

    if not qa_pairs:
        return jsonify({"error": "Հարցեր չկան"}), 400

    prompt = f"""
Դու կատարում ես ՄԻԱՅՆ ուղղագրական ստուգում։

ԽԻՍՏ ԿԱՆՈՆՆԵՐ՝
- ՉՓՈԽԵՍ հարցի իմաստը
- ՉՓՈԽԵՍ ձևակերպումը
- ՉԿՐՃԱՏԵՍ կամ ՉԵՐԿԱՐԱՑՆԵՍ
- Ուղղիր ՄԻԱՅՆ տառասխալները
- Եթե սխալ չկա → թող նույնը

Պատասխանները թող ՄԵԾԱՏԱՌ

Տվյալներ՝
{qa_pairs}

Վերադարձրու ՄԻԱՅՆ JSON այս ֆորմատով՝
[
  {{"question": "...", "answer": "...", "changes": "ինչ տառասխալ է ուղղվել կամ 'չկա'"}}
]
"""
    try:
        provider = get_provider(DEFAULT_AI_MODEL)
        raw = await provider.ask_async(prompt)  # ← ուղիղ async կանչ, ոչ call_for_json

        # parse անում ենք raw պատասխանը
        try:
            parsed = clean_json_list(raw)
        except Exception:
            match = re.search(r"\[.*\]", raw, re.S)
            parsed = json.loads(match.group()) if match else []

        if isinstance(parsed, dict):
            parsed = parsed.get("pairs") or parsed.get("questions") or []

        if not isinstance(parsed, list) or not parsed:
            # AI-ն վավեր պատասխան չի վերադարձրել. request-ի ձախողում, ոչ raw payload-ը լոգում ենք
            # (կանոն 17. request/response body ամբողջությամբ չգրանցել), այլ միայն երկարությունը
            logger.error(
                "ai_edit_crossword_invalid_response",
                extra={
                    "Operation": "AiEditCrossword",
                    "RequestId": g.get("request_id"),
                    "RawResponseLength": len(raw or ""),
                },
            )
            return jsonify({"error": "AI-ն վավեր ցանկ չվերադարձրեց"}), 502

        normalized = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            q = item.get("question") or item.get("q") or ""
            a = item.get("answer") or item.get("a") or ""
            if q and a:
                normalized.append({
                    "question": str(q).strip(),
                    "answer": str(a).strip().upper(),
                    "changes": item.get("changes", "")
                })

        if not normalized:
            return jsonify({"error": "Վավեր հարց-պատասխան չստացվեց"}), 502

        return jsonify({"pairs": normalized})

    except Exception as e:
        logger.error(
            "ai_edit_crossword_failed",
            extra={"Operation": "AiEditCrossword", "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"error": str(e)}), 500


@app.route('/ai_setup')
@require_auth()
async def ai_setup():
    denied = blocked_response(mode="ai")
    if denied:
        return denied

    return render_template('ai_setup.html', is_teacher=user_has_role("teacher"))


# =====================================================
# 🔹 UPLOAD / DOWNLOAD
# =====================================================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


async def save_file_async(file, save_path):
    return await asyncio.to_thread(file.save, save_path)


async def delete_file_async(file_path):
    return await asyncio.to_thread(os.remove, file_path)


@app.route('/upload-bg', methods=['POST'])
@require_auth()
async def upload_bg():
    if 'bg_file' not in request.files:
        return jsonify({'success': False, 'error': 'Ֆայլ չի ընտրվել'})

    file = request.files['bg_file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Ֆայլի անունը բացակայում է'})

    if file and allowed_file(file.filename):
        filename = f"bg_{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        try:
            await save_file_async(file, save_path)
        except Exception as e:
            logger.error(
                "bg_upload_failed",
                extra={"Operation": "UploadBackground", "Filename": filename, "RequestId": g.get("request_id")},
                exc_info=True,
            )
            return jsonify({'success': False, 'error': 'Ֆայլը պահպանել չհաջողվեց'})
        return jsonify({'success': True, 'filename': filename, 'url': f'/static/uploads/{filename}'})
    else:
        return jsonify({'success': False, 'error': 'Ֆայլի ֆորմատը թույլատրված չէ'})


@app.route('/delete-bg', methods=['POST'])
@require_auth()
async def delete_bg():
    data = request.get_json(silent=True) or {}
    filename = secure_filename(data.get('filename') or '')
    if not filename:
        return jsonify({'success': False, 'error': 'Ֆայլի անունը բացակայում է'})

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(file_path):
        try:
            await delete_file_async(file_path)
        except Exception as e:
            logger.error(
                "bg_delete_failed",
                extra={"Operation": "DeleteBackground", "Filename": filename, "RequestId": g.get("request_id")},
                exc_info=True,
            )
            return jsonify({'success': False, 'error': 'Ֆայլը ջնջել չհաջողվեց'})
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Ֆայլը չի գտնվել'})


@app.route('/download/<int:crossword_id>')
@require_auth()
async def download_crossword(crossword_id):
    user_id = g.current_user["sub"]
    fields = ['data', 'title', 'keyword', 'description', 'grade', 'subject']
    db_data = {}

    for field in fields:
        val = await asyncio.to_thread(
            db_operation_wrapper, get_single_field_db, crossword_id, user_id, field
        )
        db_data[field] = val

    if not db_data['data']:
        return "Խաչբառը չի գտնվել։", 404

    try:
        data_json = json.loads(db_data['data']) if isinstance(db_data['data'], str) else db_data['data']
    except Exception as e:
        logger.error(
            "crossword_download_data_parse_failed",
            extra={"Operation": "DownloadCrossword", "CrosswordId": crossword_id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return "Տվյալների սխալ ձևաչափ։", 500

    title = db_data['title'] or "Անանուն Խաչբառ"
    content = f"ՎԵՐՆԱԳԻՐ: {title}\n"

    subjects_list = parse_list_field(db_data['subject'])
    grades_list = parse_list_field(db_data['grade'])

    content += f"ԱՌԱՐԿԱ: {', '.join(subjects_list) if subjects_list else 'Նշված չէ'}\n"
    content += f"ԴԱՍԱՐԱՆ: {', '.join(grades_list) if grades_list else 'Նշված չէ'}\n"
    content += f"ԲԱՆԱԼԻ ԲԱՌ: {db_data['keyword'] or 'Նշված չէ'}\n"

    if db_data['description']:
        content += f"ՆԿԱՐԱԳՐՈՒԹՅՈՒՆ: {db_data['description']}\n"

    content += "\n" + "=" * 50 + "\n\n"
    content += "ՀԱՐՑԵՐ ԵՎ ՊԱՏԱՍԽԱՆՆԵՐ:\n\n"

    placed_items = data_json.get('placed', [])
    if placed_items:
        for item in placed_items:
            num = item.get('number', '?')
            q_text = item.get('question', '---')
            answer = item.get('word', '---')
            direction = "Հորիզոնական" if item.get('dir') == 'h' else "Ուղղահայաց"
            content += f"{num}. [{direction}] ՀԱՐՑ: {q_text}\n"
            content += f"   ՊԱՏԱՍԽԱՆ: {answer}\n"
            content += "-" * 35 + "\n"
    else:
        content += "Հարցեր չեն գտնվել։"

    output = io.BytesIO()
    output.write(content.encode('utf-8'))
    output.seek(0)

    safe_filename = "".join([c for c in title if c.isalnum() or c in (' ', '_')]).strip()

    logger.info(
        "crossword_downloaded",
        extra={"Operation": "DownloadCrossword", "CrosswordId": crossword_id, "UserId": user_id, "RequestId": g.get("request_id")},
    )

    return send_file(output, mimetype="text/plain", as_attachment=True,
                     download_name=f"{safe_filename}.txt")


# =====================================================
# 🔹 DB OPERATION WRAPPERS
# =====================================================

def db_operation_wrapper(callback, *args, **kwargs):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        result = callback(conn, *args, **kwargs)
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        logger.error(
            "db_operation_failed",
            extra={"Operation": getattr(callback, "__name__", "unknown_db_operation")},
            exc_info=True,
        )
        raise e
    finally:
        conn.close()


def insert_new_crossword_db(conn, user_id, title, keyword, bg_path, crossword_data,
                            description, creator_name, subject, grade, is_ai=0,
                            show_creator_data='no', is_draft=0):
    subject_json = normalize_to_json_list(subject)
    grade_json = normalize_to_json_list(grade)
    # Սևագրի դեպքում դատարկ նկարագրությունը պահում ենք դատարկ (առանց ինքնաշխատ լրացման)
    desc_value = description if is_draft else (description or "Նկարագրություն չկա")


    conn.execute("""
        INSERT INTO crosswords
            (user_id, title, keyword, background, data, description, creator_name,
             subject, grade, is_ai_generated, show_creator_data, is_draft)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, title, keyword, bg_path,
        json.dumps(crossword_data, ensure_ascii=False),
        desc_value,
        creator_name, subject_json, grade_json, is_ai,
        show_creator_data, is_draft
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_crossword_db(conn, id, title, keyword, bg_path, crossword_data,
                        description, creator_name, subjects, grades, user_id,
                        show_creator_data='no', is_draft=0):
    subject_json = normalize_to_json_list(subjects)
    grade_json = normalize_to_json_list(grades)
    desc_value = description if is_draft else (description or "Նկարագրություն չկա")

    conn.execute("""
        UPDATE crosswords
        SET title=?, keyword=?, background=?, data=?,
            description=?, creator_name=?, subject=?, grade=?,
            show_creator_data=?, is_draft=?
        WHERE id=? AND user_id=?
    """, (
        title, keyword, bg_path,
        json.dumps(crossword_data, ensure_ascii=False),
        desc_value,
        creator_name, subject_json, grade_json,
        show_creator_data, is_draft,
        id, user_id
    ))


def insert_participant_db(conn, data):
    cursor = conn.execute("""
        INSERT INTO participants (crossword_id, name, surname, gender, school, grade)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data.get('crossword_id'), data.get('name'), data.get('surname'),
        data.get('gender'), data.get('school', ''), data.get('class', '')
    ))
    return cursor.lastrowid


def fetch_crossword_by_id(conn, id, user_id):
    return conn.execute(
        "SELECT * FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)
    ).fetchone()


def fetch_crosswords_list(conn, user_id, page, per_page, query):
    if not query:
        total = conn.execute(
            "SELECT COUNT(*) FROM crosswords WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        total_pages = (total + per_page - 1) // per_page
        offset = (page - 1) * per_page

        rows = conn.execute("""
            SELECT id, title, keyword, background, creator_name, is_ai_generated
            FROM crosswords
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, (user_id, per_page, offset)).fetchall()
        return [dict(r) for r in rows], total_pages, total

    else:
        rows = conn.execute("""
            SELECT id, title, keyword, background, creator_name, is_ai_generated
            FROM crosswords WHERE user_id = ? ORDER BY id DESC
        """, (user_id,)).fetchall()

        all_cw = [dict(r) for r in rows]
        q_cf = query.casefold()
        matched = [
            cw for cw in all_cw
            if q_cf in (cw.get('title') or '').casefold()
               or q_cf in (cw.get('keyword') or '').casefold()
               or q_cf in (cw.get('creator_name') or '').casefold()
        ]

        total = len(matched)
        total_pages = (total + per_page - 1) // per_page
        offset = (page - 1) * per_page
        return matched[offset: offset + per_page], total_pages, total


def delete_crossword_from_db(conn, id, user_id):
    cw = conn.execute(
        "SELECT id FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)
    ).fetchone()
    if cw:
        # Կապված տվյալները՝ նախ, հետո ինքը խաչբառը
        conn.execute("DELETE FROM crossword_stats WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM comments        WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM vote_feedback   WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM results         WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM participants    WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM questions       WHERE crossword_id = ?", (id,))
        conn.execute("DELETE FROM crosswords      WHERE id = ?", (id,))
        return True
    return False


def get_single_field_db(conn, id, user_id, field):
    result = conn.execute(
        f"SELECT {field} FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)
    ).fetchone()
    return result[field] if result else None


# =====================================================
# 🔹 PARTICIPANT / RESULT ROUTES
# =====================================================

@app.route('/api/save_participant', methods=['POST'])
async def save_participant():
    data = request.get_json()

    if not data or not data.get('name') or not data.get('surname'):
        return jsonify({'status': 'error', 'message': 'Անունը և ազգանունը պարտադիր են'}), 400

    if not data.get('crossword_id'):
        return jsonify({'status': 'error', 'message': 'crossword_id բացակայում է'}), 400

    try:
        participant_id = await asyncio.to_thread(
            db_operation_wrapper, insert_participant_db, data
        )
        return jsonify({'status': 'success', 'id': participant_id}), 200
    except Exception as e:
        logger.error(
            "save_participant_failed",
            extra={"Operation": "SaveParticipant", "CrosswordId": data.get('crossword_id'), "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/save_result/<int:id>', methods=['POST'])
async def save_result(id):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Տվյալներ չեն ստացվել"}), 400

    name = data.get('name')
    surname = data.get('surname')
    gender = data.get('gender')
    school = data.get('school', '')
    grade = data.get('class', '')
    score = int(data.get('correct_count') or 0)
    total_words = int(data.get('total_words') or 0)
    duration = int(data.get('time_taken') or 0)

    try:
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("""
                INSERT INTO participants (crossword_id, name, surname, gender, school, grade)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (id, name, surname, gender, school, grade))
            participant_id = cursor.lastrowid

            await db.execute("""
                INSERT INTO results (participant_id, crossword_id, score, total_words, duration, is_completed)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (participant_id, id, score, total_words, duration, 1))

            await db.commit()
            return jsonify({"success": True, "message": "Արդյունքը պահպանվեց ✅"}), 200

    except Exception as e:
        logger.error(
            "save_result_failed",
            extra={"Operation": "SaveResult", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "message": "Սխալ պահպանման ժամանակ"}), 500


# =====================================================
# 🔹 CROSSWORD CRUD ROUTES
# =====================================================

@app.route('/choose_keyword_mode', methods=['GET', 'POST'])
@require_auth()
async def choose_keyword_mode():
    if request.method == 'POST':
        use_keyword = request.form.get('use_keyword')
        # Ռեժիմն արգելված լինելու դեպքում կանգնեցնում ենք դեռ ընտրության պահին
        mode = {'yes': 'keyword', 'ai': 'ai'}.get(use_keyword)
        denied = blocked_response(mode=mode)
        if denied:
            return denied

        if use_keyword == 'yes':
            return redirect(url_for('input_questions', mode='key'))
        elif use_keyword == 'no':
            return redirect(url_for('input_questions', use_keyword='no'))
        elif use_keyword == 'ai':
            return redirect(url_for('ai_setup'))

    settings = get_settings()
    return render_template('choose_keyword_mode.html',
                           ai_locked=settings['ai_creation_locked'],
                           keyword_locked=settings['keyword_creation_locked'])


@app.route('/save_draft', methods=['POST'])
@require_auth()
async def save_draft():
    """Պահպանում է կիսատ խաչբառը որպես սևագիր՝ առանց խիստ վալիդացիայի։"""
    denied = blocked_response()
    if denied:
        return denied

    draft_id = (request.form.get('draft_id', '') or '').strip()
    title = (request.form.get('title', '') or '').strip()
    keyword = (request.form.get('keyword', '') or '').strip().upper()
    description = (request.form.get('description', '') or '').strip()
    keyword_info = (request.form.get('keyword_info', '') or '').strip()
    creator_name = (request.form.get('creator_name', '') or '').strip()
    grades_list = request.form.getlist('grades')
    selected_subjects_raw = (request.form.get('selected_subjects', '') or '').strip()
    subjects_list = [s.strip() for s in selected_subjects_raw.split(',') if s.strip()]
    font_size = request.form.get('font_size', '20px')
    font_family = request.form.get('font_family', 'Segoe UI')

    creator_fields = request.form.getlist('creator_fields')
    if not user_has_role("teacher"):
        creator_fields = []
    show_creator_data = json.dumps(creator_fields, ensure_ascii=False) if creator_fields else 'no'

    AUDIO_EXT = {'mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a'}
    questions = []
    for key in request.form:
        if key.startswith('question_'):
            index = key.split('_')[1]
            q = (request.form.get(f'question_{index}') or '').strip()
            a = (request.form.get(f'answer_{index}') or '').strip().upper()
            if not q and not a:
                continue  # լրիվ դատարկ տողը բաց ենք թողնում
            item = {'q': q, 'a': a}
            media_file = request.files.get(f'q_media_{index}')
            if media_file and media_file.filename:
                ext = media_file.filename.rsplit('.', 1)[-1].lower() if '.' in media_file.filename else 'bin'
                fname = f"qmedia_{uuid.uuid4().hex[:12]}_{index}.{ext}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
                await save_file_async(media_file, save_path)
                item['q_media'] = f"uploads/{fname}"
                item['q_media_type'] = 'audio' if ext in AUDIO_EXT else 'image'
            questions.append(item)

    # ֆոն
    file = request.files.get('bg_upload')
    bg_choice = request.form.get('bg_choice', '')
    bg_path = ''
    if file and file.filename:
        filename = f"bg_{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
        await save_file_async(file, save_path)
        bg_path = f'uploads/{filename}'
    elif bg_choice:
        bg_path = f'backgrounds/{bg_choice}'

    # Փորձում ենք ցանց կառուցել, բայց սխալի դեպքում պարզապես անտեսում ենք
    try:
        crossword_data = generate_crossword_grid(questions, keyword) or {}
    except Exception as e:
        logger.warning(
            "draft_grid_generation_failed",
            extra={"Operation": "SaveDraft", "DraftId": draft_id, "RequestId": g.get("request_id"), "Exception": type(e).__name__},
        )
        crossword_data = {}

    # Միշտ պահում ենք raw հարցերը՝ resume-ի համար (սա է Ա7-ի հիմքը)
    crossword_data['draft_questions'] = questions
    crossword_data.update({
        'use_keyword': 'yes' if request.form.get('use_keyword') == 'yes' else 'no',
        'description': description,
        'keyword_info': keyword_info,
        'grades': grades_list,
        'subjects': subjects_list,
        'font_size': font_size,
        'font_family': font_family,
    })

    try:
        if draft_id and draft_id.isdigit():
            # արդեն կա սևագիր → թարմացնում ենք ՆՈՒՅՆԸ (նոր չենք ստեղծում)
            await asyncio.to_thread(
                db_operation_wrapper,
                update_crossword_db,
                int(draft_id), title, keyword, bg_path, crossword_data,
                description, creator_name, subjects_list, grades_list,
                g.current_user["sub"], show_creator_data, 1  # is_draft=1
            )
            crossword_id = int(draft_id)
        else:
            # առաջին պահումն է → ստեղծում ենք նոր սևագիր
            crossword_id = await asyncio.to_thread(
                db_operation_wrapper,
                insert_new_crossword_db,
                g.current_user["sub"],
                title, keyword, bg_path, crossword_data,
                description, creator_name, subjects_list, grades_list, 0,
                show_creator_data, 1  # is_draft=1
            )
    except Exception as e:
        logger.error(
            "save_draft_failed",
            extra={"Operation": "SaveDraft", "DraftId": draft_id, "UserId": g.current_user["sub"], "RequestId": g.get("request_id")},
            exc_info=True,
        )
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({
        "success": True,
        "id": crossword_id,
        "editUrl": url_for('edit_crossword', id=crossword_id)
    })

@app.route('/api/check-crossword', methods=['POST'])
@require_auth()
async def check_crossword():
    """Ստուգում է՝ որ հարցերի պատասխանները չեն հատվում մյուսների հետ (առանց պահպանելու)։"""
    data = request.get_json(silent=True) or {}
    keyword = (data.get('keyword') or '').strip().upper()

    items = []
    for i, it in enumerate(data.get('questions') or []):
        q = (it.get('q') or '').strip()
        a = (it.get('a') or '').strip().upper()
        if a:
            items.append({'q': q, 'a': a, 'idx': i})

    if not items:
        return jsonify({'skipped': []})

    try:
        grid = await asyncio.to_thread(
            generate_crossword_grid,
            [{'q': x['q'], 'a': x['a']} for x in items],
            keyword
        )
    except Exception as e:
        logger.warning(
            "check_crossword_failed",
            extra={"Operation": "CheckCrossword", "RequestId": g.get("request_id"), "Exception": type(e).__name__},
        )
        return jsonify({'skipped': []})   # ստուգումը ձախողվեց → թողնում ենք սովորական ընթացքին

    # skipped-ը համապատասխանեցնում ենք ձևի տողերի ինդեքսներին
    used, result = set(), []
    for s in (grid or {}).get('skipped') or []:
        word = (s.get('word') or '').upper()
        question = s.get('question') or ''
        match = next((x for x in items if x['idx'] not in used and x['a'] == word and x['q'] == question), None) \
             or next((x for x in items if x['idx'] not in used and x['a'] == word), None)
        if match:
            used.add(match['idx'])
            result.append({'idx': match['idx'], 'q': match['q'], 'a': match['a']})

    return jsonify({'skipped': result})


@app.route('/input', methods=['GET', 'POST'])
@require_auth()
async def input_questions():
    if request.method == 'POST':
        # mode=key / use_keyword=yes նշանակում է keyword ռեժիմ
        uses_keyword = (request.args.get('mode') == 'key'
                        or request.args.get('use_keyword') == 'yes'
                        or request.form.get('use_keyword') == 'yes')
        denied = blocked_response(mode="keyword" if uses_keyword else None)
        if denied:
            return denied

    mode = request.args.get('mode', '')
    use_keyword = request.args.get('use_keyword', '')

    # mode parameter-ից հաշվարկ
    if not use_keyword:
        if mode == 'key':
            use_keyword = 'yes'
        elif mode == 'nokey':
            use_keyword = 'no'
        else:
            use_keyword = 'yes'

    bg_folder = os.path.join(app.static_folder, "backgrounds")
    bg_images = await asyncio.to_thread(
        lambda: [f for f in os.listdir(bg_folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    )

    if request.method == 'POST':
        grades_list = request.form.getlist('grades')
        selected_subjects_raw = request.form.get('selected_subjects', '').strip()
        subjects_list = [s.strip() for s in selected_subjects_raw.split(',') if s.strip()]
        font_size = request.form.get('font_size', '20px')
        font_family = request.form.get('font_family', 'Segoe UI')
        keyword = request.form.get('keyword', '').strip().upper()
        title = request.form.get('title', 'Անանուն Խաչբառ').strip()
        description = request.form.get('description', '').strip()
        keyword_info = request.form.get('keyword_info', '').strip()
        creator_name = request.form.get('creator_name', 'Անհայտ').strip()
        # ── ՆՈՐ ──────────────────────────────────────────────────────────────
        creator_fields = request.form.getlist('creator_fields')
        if not user_has_role("teacher"):
            creator_fields = []
        show_creator_data = json.dumps(creator_fields, ensure_ascii=False) if creator_fields else 'no'

        hint_type = request.form.get('hint_type')
        hint_text = request.form.get('global_hint_text', '').strip()
        hint_file = request.files.get('global_hint_media')
        hint_value = None

        if hint_type in ['audio', 'video', 'image'] and hint_file and hint_file.filename:
            ext = hint_file.filename.rsplit('.', 1)[-1].lower() if '.' in hint_file.filename else 'bin'
            filename = f"hint_{uuid.uuid4().hex[:12]}.{ext}"
            hint_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await save_file_async(hint_file, hint_path)
            hint_value = f"uploads/{filename}"
        elif hint_type == 'text':
            hint_value = hint_text

        keyword_material_type = request.form.get('keyword_material_type', '').strip()
        keyword_material_value = ''
        keyword_material_file = request.files.get('keyword_material_file')

        if keyword_material_type == 'text':
            keyword_material_value = request.form.get('keyword_material_text', '').strip()
        elif keyword_material_type in ('image',
                                       'animation') and keyword_material_file and keyword_material_file.filename:
            ext = keyword_material_file.filename.rsplit('.', 1)[
                -1].lower() if '.' in keyword_material_file.filename else 'bin'
            filename = f"kwmat_{uuid.uuid4().hex[:12]}.{ext}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await save_file_async(keyword_material_file, save_path)
            keyword_material_value = f"uploads/{filename}"
        elif keyword_material_type == 'link':
            keyword_material_value = request.form.get('keyword_material_link', '').strip()

        bg_width = request.form.get('bg_width', '400px')
        bg_height = request.form.get('bg_height', '200px')
        bg_size_mode = request.form.get('bg_size_mode', 'fixed')

        file = request.files.get('bg_upload')
        bg_choice = request.form.get('bg_choice', '')
        bg_path = ''

        if file and file.filename:
            filename = f"bg_{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(file, save_path)
            bg_path = f'uploads/{filename}'
        elif bg_choice:
            bg_path = f'backgrounds/{bg_choice}'

        AUDIO_EXT = {'mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a'}

        questions = []
        for key in request.form:
            if key.startswith('question_'):
                index = key.split('_')[1]
                q = (request.form.get(f'question_{index}') or '').strip()
                a = (request.form.get(f'answer_{index}') or '').strip().upper()
                media_file = request.files.get(f'q_media_{index}')
                has_media = bool(media_file and media_file.filename)

                # Պատասխանը պարտադիր է, հարցը՝ տեքստ ԿԱՄ նկար/աուդիո
                if not a or (not q and not has_media):
                    continue

                item = {'q': q, 'a': a}
                if has_media:
                    ext = media_file.filename.rsplit('.', 1)[-1].lower() if '.' in media_file.filename else 'bin'
                    fname = f"qmedia_{uuid.uuid4().hex[:12]}_{index}.{ext}"
                    save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                    await save_file_async(media_file, save_path)
                    item['q_media'] = f"uploads/{fname}"
                    item['q_media_type'] = 'audio' if ext in AUDIO_EXT else 'image'
                questions.append(item)

        crossword_data = generate_crossword_grid(questions, keyword)

        # Կցված նկար/աուդիոն վերադարձնում ենք placed տարրերին
        media_map = {(it['q'], it['a']): it for it in questions if it.get('q_media')}
        for placed in crossword_data.get('placed', []):
            src = media_map.get((placed.get('question'), placed.get('word')))
            if src:
                placed['q_media'] = src['q_media']
                placed['q_media_type'] = src['q_media_type']
        crossword_data.update({
            'use_keyword': 'yes' if request.form.get('use_keyword') == 'yes' else 'no',
            'hint_type': hint_type,
            'hint_value': hint_value,
            'description': description,
            'bg_width': bg_width,
            'bg_height': bg_height,
            'bg_size_mode': bg_size_mode,
            'grades': grades_list,
            'subjects': subjects_list,
            'font_size': font_size,
            'keyword_info': keyword_info,
            'font_family': font_family,
            'keyword_material_type': keyword_material_type,
            'keyword_material_value': keyword_material_value,
        })

        # Բառ է դուրս մնացել → պահում ենք բոլոր հարցերը և թողնում սևագիր,
        # որ խմբագրման էջում չհատվածներն էլ երևան
        has_skipped = bool(crossword_data.get('skipped'))
        if has_skipped:
            crossword_data['draft_questions'] = questions

        try:
            crossword_id = await asyncio.to_thread(
                db_operation_wrapper,
                insert_new_crossword_db,
                g.current_user["sub"],
                title, keyword, bg_path, crossword_data,
                description, creator_name, subjects_list, grades_list, 0,
                show_creator_data,
                1 if has_skipped else 0
            )
        except Exception as e:
            logger.error(
                "input_questions_create_failed",
                extra={"Operation": "CreateCrossword", "UserId": g.current_user["sub"], "RequestId": g.get("request_id")},
                exc_info=True,
            )
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for('input_questions'))

        draft_id = (request.form.get('draft_id') or '').strip()
        if draft_id.isdigit():
            try:
                await asyncio.to_thread(
                    db_operation_wrapper, delete_crossword_from_db,
                    int(draft_id), g.current_user["sub"]
                )
            except Exception:
                logger.warning(
                    "draft_cleanup_failed",
                    extra={"Operation": "CreateCrossword", "DraftId": draft_id, "RequestId": g.get("request_id")},
                )

        # ← ՆՈՐ: եթե բառեր դուրս մնացին, ծանուցել և տանել խմբագրման էջ
        if flash_skipped_words(crossword_data):
            return redirect(url_for('edit_crossword', id=crossword_id))

        return redirect(url_for('view_crossword', id=crossword_id))
    is_teacher = user_has_role("teacher")
    return render_template('input_questions.html',
                           use_keyword=use_keyword,
                           bg_images=bg_images,
                           is_teacher=is_teacher)


@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@require_auth()
async def edit_crossword(id):
    if request.method == 'POST':
        denied = blocked_response()
        if denied:
            return denied

    user_id = g.current_user["sub"]
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id, id, user_id)

    if not cw:
        flash("Խաչբառը չի գտնվել կամ թույլտվություն չկա։", "error")
        return redirect(url_for('my_crosswords'))

    cw = dict(cw)
    bg_folder = os.path.join(app.static_folder, "backgrounds")
    bg_images = await asyncio.to_thread(
        lambda: [f for f in os.listdir(bg_folder) if f.lower().endswith((".jpg", "jpeg", ".png"))]
    )

    # Խաչբառի տեսակը՝ ըստ պահված նշանի, ոչ թե միայն ըստ բանալի բառի առկայության
    try:
        _saved = json.loads(cw.get('data') or '{}')
    except Exception:
        _saved = {}
    keyword_mode = bool(cw.get('keyword')) or _saved.get('use_keyword') == 'yes'

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        keyword = request.form.get('keyword', '').strip().upper()
        keyword_hint = request.form.get('keyword_hint', '')
        description = request.form.get('description', '').strip()
        keyword_info = request.form.get('keyword_info', '').strip()
        creator_name = request.form.get('creator_name', '').strip()
        # ── ՆՈՐ ──────────────────────────────────────────────────────────────
        creator_fields = request.form.getlist('creator_fields')
        if not user_has_role("teacher"):
            creator_fields = []
        show_creator_data = json.dumps(creator_fields, ensure_ascii=False) if creator_fields else 'no'

        grades = request.form.getlist('grades')
        selected_subjects_raw = request.form.get('selected_subjects', '')
        subjects = [s.strip() for s in selected_subjects_raw.split(',') if s.strip()]

        font_size = request.form.get('font_size', '20px')
        font_family = request.form.get('font_family', 'Segoe UI')
        bg_size_mode = request.form.get('bg_size_mode', 'fixed')
        bg_width = request.form.get('bg_width')
        bg_height = request.form.get('bg_height')

        bg_file = request.files.get('bg_upload')
        bg_choice = request.form.get('bg_choice', '')
        remove_bg = request.form.get('remove_background', '0') == '1'
        bg_path = cw['background']

        if bg_file and bg_file.filename:
            filename = secure_filename(bg_file.filename)
            filename = f"bg_{uuid.uuid4().hex[:12]}_{filename}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(bg_file, save_path)
            bg_path = f'uploads/{filename}'
        elif bg_choice:
            bg_path = f'backgrounds/{bg_choice}'
        elif remove_bg:
            bg_path = ''

        AUDIO_EXT = {'mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a'}

        questions = []
        for key in request.form:
            if key.startswith('question_'):
                index = key.split('_')[1]
                q = request.form.get(f'question_{index}', '').strip()
                a = request.form.get(f'answer_{index}', '').strip().upper()
                if not a:
                    continue

                media_val = request.form.get(f'q_media_existing_{index}', '').strip()
                media_type = request.form.get(f'q_media_existing_type_{index}', '').strip()

                if request.form.get(f'remove_q_media_{index}', '0') == '1':
                    media_val, media_type = '', ''

                media_file = request.files.get(f'q_media_{index}')
                if media_file and media_file.filename:
                    ext = media_file.filename.rsplit('.', 1)[-1].lower() if '.' in media_file.filename else 'bin'
                    fname = f"qmedia_{uuid.uuid4().hex[:12]}_{index}.{ext}"
                    save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                    await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
                    await save_file_async(media_file, save_path)
                    media_val = f"uploads/{fname}"
                    media_type = 'audio' if ext in AUDIO_EXT else 'image'

                # Հարցը՝ տեքստ ԿԱՄ նկար/աուդիո
                if not q and not media_val:
                    continue

                item = {'q': q, 'a': a}
                if media_val:
                    item['q_media'] = media_val
                    item['q_media_type'] = media_type
                questions.append(item)

        if keyword_mode:
            question_count = len(questions)
            if not keyword:
                flash("Բանալի բառը պարտադիր է։", "error")
                return redirect(url_for('edit_crossword', id=id))
            if len(keyword) != question_count:
                flash(
                    f"Բանալի բառը պետք է ունենա {question_count} տառ "
                    f"(հիմա ունի {len(keyword)})։",
                    "error"
                )
                return redirect(url_for('edit_crossword', id=id))

        crossword_data = generate_crossword_grid(questions, keyword)

        # Կցված նկար/աուդիոն վերադարձնում ենք placed տարրերին
        media_map = {(it['q'], it['a']): it for it in questions if it.get('q_media')}
        for placed in crossword_data.get('placed', []):
            src = media_map.get((placed.get('question'), placed.get('word')))
            if src:
                placed['q_media'] = src['q_media']
                placed['q_media_type'] = src['q_media_type']

        hint_type = request.form.get('hint_type', '')
        global_hint_text = request.form.get('global_hint_text', '').strip()
        file_hint = request.files.get('global_hint_media')
        remove_hint = request.form.get('remove_hint_media', '0') == '1'

        old_data = json.loads(cw['data'])
        global_hint_media = old_data.get('global_hint_media')
        if not global_hint_media and old_data.get('hint_type') in ('audio', 'video', 'image'):
            # Ստեղծման էջը ֆայլը պահում է hint_value-ում
            global_hint_media = old_data.get('hint_value') or None
        global_hint = None

        if hint_type == 'text':
            global_hint = global_hint_text
            global_hint_media = None
        elif hint_type in ('audio', 'video', 'image'):
            if file_hint and file_hint.filename:
                filename = secure_filename(file_hint.filename)
                filename = f"hint_{uuid.uuid4().hex[:12]}_{filename}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
                await save_file_async(file_hint, save_path)
                global_hint_media = f'static/uploads/{filename}'
            elif remove_hint:
                global_hint_media = None
        else:
            global_hint = None
            global_hint_media = None

        keyword_material_type = request.form.get('keyword_material_type', '').strip()
        keyword_material_value = old_data.get('keyword_material_value', '')
        keyword_material_file = request.files.get('keyword_material_file')
        remove_km = request.form.get('remove_keyword_material', '0') == '1'

        if remove_km:
            keyword_material_type = ''
            keyword_material_value = ''
        elif keyword_material_type == 'text':
            keyword_material_value = request.form.get('keyword_material_text', '').strip()
        elif keyword_material_type in ('image',
                                       'animation') and keyword_material_file and keyword_material_file.filename:
            ext = keyword_material_file.filename.rsplit('.', 1)[
                -1].lower() if '.' in keyword_material_file.filename else 'bin'
            filename = f"kwmat_{uuid.uuid4().hex[:12]}.{ext}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(keyword_material_file, save_path)
            keyword_material_value = f"uploads/{filename}"
        elif keyword_material_type == 'link':
            keyword_material_value = request.form.get('keyword_material_link', '').strip()
        elif keyword_material_type == '':
            keyword_material_value = ''

        crossword_data.update({
            'use_keyword': 'yes' if keyword_mode else 'no',
            'hint_type': hint_type,
            'global_hint': global_hint,
            'global_hint_media': global_hint_media,
            'keyword_hint': keyword_hint,
            'description': description,
            'keyword_info': keyword_info,
            'grades': grades,
            'subjects': subjects,
            'font_size': font_size,
            'font_family': font_family,
            'bg_size_mode': bg_size_mode,
            'bg_width': bg_width,
            'bg_height': bg_height,
            'keyword_material_type': keyword_material_type,
            'keyword_material_value': keyword_material_value,
        })

        # ── Ավարտված խաչբառ + չհատվող բառեր → ՉԵՆՔ պահպանում ──
        # Բազայում խաչբառը մնում է նախկին ավարտված վիճակով, իսկ օգտատիրոջը
        # ցույց ենք տալիս նույն էջը՝ իր մուտքագրած (չպահպանված) փոփոխություններով։
        if (crossword_data or {}).get('skipped') and not cw.get('is_draft'):
            flash_skipped_words(crossword_data)

            preview = dict(crossword_data)
            preview['placed'] = [
                {
                    'question': it.get('q', ''),
                    'word': it.get('a', ''),
                    'number': i + 1,
                    'x': 0, 'y': 0, 'dir': 'h',
                    **({'q_media': it['q_media'], 'q_media_type': it.get('q_media_type', '')}
                       if it.get('q_media') else {})
                }
                for i, it in enumerate(questions)
            ]

            cw_preview = dict(cw)
            cw_preview.update({
                'title': title,
                'description': description,
                'keyword': keyword,
                'background': bg_path,
            })

            logger.info(
                "edit_crossword_not_saved_skipped",
                extra={"Operation": "EditCrossword", "CrosswordId": id,
                       "SkippedCount": len(crossword_data.get('skipped') or []),
                       "RequestId": g.get("request_id")},
            )

            return render_template('edit_crossword.html',
                                   crossword=preview,
                                   cw=cw_preview,
                                   bg_images=bg_images,
                                   creator_name=cw.get('creator_name', ''),
                                   show_creator_data=show_creator_data,
                                   current_creator_fields=creator_fields,
                                   keyword_mode=keyword_mode,
                                   is_teacher=user_has_role("teacher"),
                                   skipped_not_saved=True)

        try:
            has_skipped = bool((crossword_data or {}).get('skipped'))
            if has_skipped:
                # Սևագիր է մնում → պահում ենք ԲՈԼՈՐ հարցերը գրված հերթականությամբ,
                # որպեսզի չհատվածները չկորչեն և հնարավոր լինի ուղղել
                crossword_data['draft_questions'] = questions
            await asyncio.to_thread(
                db_operation_wrapper,
                update_crossword_db,
                id, title, keyword, bg_path, crossword_data,
                description, creator_name, subjects, grades, g.current_user["sub"],
                show_creator_data,
                1 if has_skipped else 0  # ← դեռ բառ է դուրս մնում → մնում է սևագիր
            )
            # ← ՆՈՐ: եթե բառեր դուրս մնացին, մնա խմբագրման էջում՝ ծանուցումով
            if flash_skipped_words(crossword_data):
                return redirect(url_for('edit_crossword', id=id))
            flash("Խաչբառը հաջողությամբ թարմացվեց։")
            return redirect(url_for('view_crossword', id=id))
        except Exception as e:
            logger.error(
                "edit_crossword_save_failed",
                extra={"Operation": "EditCrossword", "CrosswordId": id, "UserId": g.current_user["sub"], "RequestId": g.get("request_id")},
                exc_info=True,
            )
            return redirect(url_for('edit_crossword', id=id))

    crossword_json = json.loads(cw['data'])

    # ── Սևագիր՝ հարցերը միշտ վերցնում ենք draft_questions-ից (գրված հերթականությամբ, առանց կորուստի) ──
    if crossword_json.get('draft_questions'):
        crossword_json['placed'] = [
            {
                'question': item.get('q', ''),
                'word': item.get('a', ''),
                'number': i + 1,
                'x': 0, 'y': 0, 'dir': 'h',  # placeholder-ներ՝ միայն դաշտերը լցնելու համար
                **({'q_media': item['q_media'],
                    'q_media_type': item.get('q_media_type', '')}
                   if item.get('q_media') else {})
            }
            for i, item in enumerate(crossword_json['draft_questions'])
        ]

    if 'hint_value' in crossword_json:
        h_type = crossword_json.get('hint_type')
        h_val = crossword_json.get('hint_value')
        if h_type == 'text':
            crossword_json['global_hint'] = h_val
        elif h_type in ['audio', 'video', 'image']:
            if h_val and isinstance(h_val, str) and h_val.startswith('hints/'):
                h_val = h_val.replace('hints/', '')
            crossword_json['global_hint_media'] = h_val

    defaults = {
        'grades': [], 'subjects': [], 'keyword_info': '',
        'font_size': '20px', 'font_family': 'Segoe UI',
        'bg_width': '400px', 'bg_height': '200px', 'bg_size_mode': 'fixed',
        'hint_type': '', 'global_hint': '', 'global_hint_media': None,
        'keyword_material_type': '', 'keyword_material_value': ''
    }
    for key, value in defaults.items():
        crossword_json.setdefault(key, value)

    # ── ընթացիկ ընտրված դաշտեր ─────────────────────────────────
    raw_scd = cw.get('show_creator_data', 'no')
    current_fields = []
    if raw_scd and raw_scd not in ('no', 'yes'):
        try:
            current_fields = json.loads(raw_scd)
        except Exception:
            current_fields = []
    elif raw_scd == 'yes':
        current_fields = ['__all__']

    return render_template('edit_crossword.html',
                           crossword=crossword_json,
                           cw=cw,
                           bg_images=bg_images,
                           creator_name=cw.get('creator_name', ''),
                           show_creator_data=cw.get('show_creator_data', 'no'),
                           current_creator_fields=current_fields,
                           keyword_mode=keyword_mode,
                           is_teacher=user_has_role("teacher"))


@app.route('/my-crosswords')
@require_auth()
async def my_crosswords():
    query = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 6
    user_id = g.current_user["sub"]

    try:
        crosswords, total_pages, total_items = await asyncio.to_thread(
            db_operation_wrapper, fetch_crosswords_list,
            user_id, page, per_page, query
        )
    except Exception as e:
        logger.error(
            "my_crosswords_list_failed",
            extra={"Operation": "ListMyCrosswords", "UserId": user_id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        flash(f"Սխալ տվյալների բազայից: {e}", "error")
        crosswords, total_pages = [], 0

    return render_template('my_crosswords.html',
                           crosswords=crosswords, page=page,
                           total_pages=total_pages, per_page=per_page, query=query)


@app.route('/delete_crossword/<int:id>', methods=['POST'])
@require_auth()
async def delete_crossword(id):
    try:
        user_id = g.current_user["sub"]
        deleted = await asyncio.to_thread(db_operation_wrapper, delete_crossword_from_db, id, user_id)
        if deleted:
            logger.info("crossword_deleted", extra={"Operation": "DeleteCrossword", "CrosswordId": id, "UserId": user_id, "RequestId": g.get("request_id")})
        flash('Խաչբառը ջնջվեց։' if deleted else 'Չեք կարող ջնջել այս խաչբառը։')
    except Exception as e:
        logger.error(
            "delete_crossword_failed",
            extra={"Operation": "DeleteCrossword", "CrosswordId": id, "RequestId": g.get("request_id")},
            exc_info=True,
        )
        flash(f"Սխալ ջնջելիս: {e}", "error")
    return redirect(url_for('my_crosswords'))


@app.route('/<int:id>')
async def view_crossword(id):
    get_flashed_messages()
    try:
        current_user_id = g.current_user["sub"]
    except Exception:
        current_user_id = None

    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)
    if not cw:
        return "Խաչբառը չի գտնվել։", 404

    cw = dict(cw)
    if cw.get('is_draft'):
        return redirect(url_for('edit_crossword', id=id))

    # Թաքցվածը հասանելի է միայն սեփականատիրոջը (ադմինը դիտում է պանելից):
    is_owner = current_user_id and str(cw.get('user_id')) == str(current_user_id)
    if not is_owner:
        if cw.get('hidden'):
            return "Խաչբառը հասանելի չէ։", 404
        if get_settings()["all_crosswords_hidden"]:
            return "Խաչբառերը ժամանակավորապես հասանելի չեն։", 404

    crossword = json.loads(cw['data'])
    crossword.setdefault('global_hint', None)

    # ── show_creator_data + ընտրված դաշտեր ────────────────────────
    raw_scd = cw.get('show_creator_data', 'no')
    selected_fields = []
    show_creator = False

    if raw_scd and raw_scd not in ('no', 'yes'):
        try:
            selected_fields = json.loads(raw_scd)
            if isinstance(selected_fields, list) and selected_fields:
                show_creator = True
        except Exception:
            selected_fields = []
    elif raw_scd == 'yes':
        show_creator = True
        selected_fields = []  # դատարկ = բոլորը

    crossword['show_creator_data'] = 'yes' if show_creator else 'no'
    crossword['creator_visible_fields'] = selected_fields

    rows = crossword.get('rows', 15)
    cols = crossword.get('cols', 15)

    h_type = crossword.get('hint_type', '')

    if h_type == 'text':
        # Տեքստային հուշում — ֆայլի ուղի չկա
        text_val = crossword.get('global_hint') or crossword.get('hint_value') or ''
        crossword['global_hint'] = text_val
        crossword['hint_value'] = text_val
        crossword['global_hint_media'] = None

    elif h_type in ('audio', 'video', 'image'):
        media = crossword.get('global_hint_media') or crossword.get('hint_value')
        if media:
            for prefix in ['static/', 'hints/']:
                if media.startswith(prefix):
                    media = media.replace(prefix, '', 1)
                    break
            if not media.startswith('uploads/'):
                media = f'uploads/{media}'
            crossword['global_hint_media'] = media
            crossword['hint_value'] = media
        crossword['global_hint'] = None

    try:
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO crossword_stats (crossword_id, view_count)
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET view_count = view_count + 1
            """, (id,))
            await db.commit()
    except Exception as e:
        # View-count ինկրեմենտի ձախողումը էջի ցուցադրումը չի խափանում → Warning
        logger.warning(
            "view_count_increment_failed",
            extra={"Operation": "IncrementViewCount", "CrosswordId": id, "Exception": type(e).__name__},
        )

    current_user_id = session.get('user_id')
    is_creator = current_user_id and 'user_id' in cw and str(current_user_id) == str(cw['user_id'])

    bg_size_mode = crossword.get('bg_size_mode', 'fixed')
    bg_width = crossword.get('bg_width', '400px')
    bg_height = crossword.get('bg_height', '200px')

    return render_template(
        'crossword.html',
        crossword=crossword, cw_id=id,
        keyword=cw.get('keyword', ''),
        title=cw.get('title', 'Խաչբառ'),
        bg=cw.get('background'),
        rows=rows, cols=cols, show_intro=True,
        description=cw.get('description', ''),
        is_creator=is_creator,
        creator_name=cw.get('creator_name', ''),
        bg_size_mode=bg_size_mode, bg_width=bg_width, bg_height=bg_height,
        creator_user_id=cw.get('user_id', ''),
        creator_visible_fields=selected_fields,
        creator_field_labels=CREATOR_FIELD_LABELS
    )


# =====================================================
# 🔹 MISC ROUTES
# =====================================================

@app.route('/')
def index():
    return jsonify({"service": "crossword", "status": "running"}), 200


@app.route("/crossword_form")
def crossword_form():
    bg_folder = os.path.join(app.static_folder, "backgrounds")
    bg_images = [f for f in os.listdir(bg_folder) if f.lower().endswith((".jpg", ".jpeg"))]
    return render_template("crossword_form.html", bg_images=bg_images)


@app.route('/dashboard')
@require_auth()
async def dashboard():
    return render_template('dashboard.html')  # ← user= հանված


@app.route('/profile')
@require_auth()
async def profile():
    return render_template('profile.html')


@app.route('/statistics')
@require_auth()
async def statistics():
    return render_template('statistics.html', user_id=g.current_user["sub"])


@app.route('/api/client-log', methods=['POST'])
async def api_client_log():
    """
    Frontend/browser JavaScript-ից եկած error-ները ընդունելու endpoint (PDF, 14.4).

    Անվտանգության կանոններ (PDF, "Production-ի կարևոր կանոններ"):
    - client-ից եկած տվյալը never trusted է
    - level-ը whitelist-ով է սահմանվում, client-ը ազատ level/source ընտրել չի կարող
    - message/page-ի երկարությունը սահմանափակված է
    - endpoint-ը rate-limit է արված IP-ով
    - request body-ն ամբողջությամբ չի գրանցվում, միայն այս validated դաշտերը
    """
    ALLOWED_CLIENT_LOG_LEVELS = {"Information", "Warning", "Error"}  # Critical client-ից թույլատրված չէ
    MAX_MESSAGE_LEN = 500
    MAX_PAGE_LEN = 200

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    if _client_log_rate_limited(client_ip):
        return jsonify({"success": False, "error": "Rate limit exceeded"}), 429

    data = request.get_json(silent=True) or {}

    level = str(data.get("level", "Error") or "Error").strip().capitalize()
    if level not in ALLOWED_CLIENT_LOG_LEVELS:
        level = "Error"

    message = str(data.get("message", "") or "Unknown client error").strip()[:MAX_MESSAGE_LEN]
    page = str(data.get("page", "") or "").strip()[:MAX_PAGE_LEN]
    client_request_id = str(data.get("requestId", "") or "").strip()[:64] or g.get("request_id")

    user_id = None
    try:
        if getattr(g, "current_user", None):
            user_id = g.current_user.get("sub")
    except Exception:
        user_id = None

    log_fields = {
        "Operation": "ClientLog",
        "RequestId": client_request_id,
        "Page": page,
        "ClientIp": client_ip,
        "UserId": user_id,
    }

    if level == "Warning":
        logger.warning(f"client_log: {message}", extra=log_fields)
    elif level == "Error":
        logger.error(f"client_log: {message}", extra=log_fields)
    else:
        logger.info(f"client_log: {message}", extra=log_fields)

    return jsonify({"success": True}), 200


@app.route('/dashboard-debug')
def dashboard_debug():
    if not FLASK_DEBUG:
        return "Not found", 404
    # Authorization header-ը ամբողջությամբ չենք վերադարձնում response-ում (կանոն 17),
    # այլ միայն presence/prefix-ը
    auth = request.headers.get("Authorization", "ՉԿԱ")
    masked_auth = (auth[:15] + "…") if auth != "ՉԿԱ" else auth
    return jsonify({"auth_header_present": auth != "ՉԿԱ", "auth_header_prefix": masked_auth})


@app.route('/debug')
async def debug():
    if not FLASK_DEBUG:
        return "Not found", 404
    def get_tables_list(conn):
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return c.fetchall()

    tables = await asyncio.to_thread(db_operation_wrapper, get_tables_list)
    return f"Բազայում առկա աղյուսակներ՝ {tables}"


@app.route('/crossword_text/<int:id>')
async def crossword_text(id):
    if not FLASK_DEBUG:
        return "Not found", 404
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)
    if not cw:
        return "Խաչբառը չի գտնվել։", 404

    crossword = json.loads(cw['data'])
    rows = crossword.get('rows', 15)
    cols = crossword.get('cols', 15)
    grid = [['.' for _ in range(cols)] for _ in range(rows)]

    for word in crossword.get('placed', []):
        x, y = word['x'], word['y']
        direction = word['dir']
        word_text = word['word']
        for i, ch in enumerate(word_text):
            if direction == 'h' and 0 <= y < rows and 0 <= x + i < cols:
                grid[y][x + i] = ch
            elif direction == 'v' and 0 <= y + i < rows and 0 <= x < cols:
                grid[y + i][x] = ch

    text_output = "\n".join(" ".join(row) for row in grid)
    return f"<pre style='font-family: monospace;'>{text_output}</pre>"


@app.route('/debug_data/<int:id>')
async def debug_data(id):
    if not FLASK_DEBUG:
        return "Not found", 404
    data_json = await asyncio.to_thread(db_operation_wrapper, get_single_field_db, id, id, 'data')
    if not data_json:
        return "Խաչբառը չի գտնվել։", 404
    return json.loads(data_json)['placed']


def prepare_grid_data(crossword):
    grid_data = {}
    for word in crossword['placed']:
        word_text = word['word']
        x, y = word['x'], word['y']
        number = word['number']
        direction = word['dir']
        is_keyword = (word['question'] == 'Բանալի բառ')

        for i, ch in enumerate(word_text):
            row = y if direction == 'h' else y + i
            col = x + i if direction == 'h' else x
            key = f"{row},{col}"

            if key not in grid_data:
                grid_data[key] = {'letter': ch, 'number': str(number) if i == 0 else '', 'is_keyword': is_keyword}
            else:
                if number and i == 0:
                    existing = grid_data[key]['number']
                    grid_data[key]['number'] = f"{existing},{number}" if existing else str(number)
                if is_keyword:
                    grid_data[key]['is_keyword'] = True
    return grid_data


# =====================================================
# 🔹 ADMIN ROUTES
# =====================================================

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin') or not session.get('admin_id'):
            flash("Մուտք գործեք ադմին որպեսություն")
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)

    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        admin = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        conn.close()

        if admin and check_password_hash(admin['password'], password):
            session['admin'] = True
            session['admin_id'] = admin['id']
            session.modified = True
            logger.info(
                "admin_login_success",
                extra={"Operation": "AdminLogin", "AdminId": admin['id'], "RequestId": g.get("request_id")},
            )
            return redirect(url_for('admin_dashboard'))

        # Սխալ մուտքանուն/գաղտնաբառ → Warning (PDF, "Wrong password / failed login attempts")
        # ⚠️ password-ն ինքը երբեք չենք լոգավորում
        logger.warning(
            "admin_login_failed",
            extra={"Operation": "AdminLogin", "Username": username, "RequestId": g.get("request_id")},
        )
        flash("Սխալ մուտքանուն կամ գաղտնաբառ")
    return render_template('admin/login.html')


@app.route('/admin/logout')
@admin_required
def admin_logout():
    admin_id = session.get('admin_id')
    session.pop('admin', None)
    session.pop('admin_id', None)
    logger.info("admin_logout", extra={"Operation": "AdminLogout", "AdminId": admin_id, "RequestId": g.get("request_id")})
    flash("Դուրս եք եկել ադմին պանելից")
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    page = request.args.get('page', 1, type=int)
    per_page = 5

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM crosswords").fetchone()[0]
    total_pages = (total + per_page - 1) // per_page
    offset = (page - 1) * per_page

    crosswords = conn.execute("""
        SELECT c.*, COUNT(p.id) AS participants
        FROM crosswords c
        LEFT JOIN participants p ON p.crossword_id = c.id
        GROUP BY c.id
        ORDER BY c.id DESC
        LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()
    conn.close()

    return render_template('admin/dashboard.html',
                           crosswords=crosswords, page=page, total_pages=total_pages)


@app.route('/admin/crossword/results/<int:id>')
@admin_required
def admin_crossword_results(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    results = conn.execute("""
        SELECT p.name, p.surname, p.school, p.grade,
               COALESCE(r.score, 0)       AS score,
               COALESCE(r.total_words, 0) AS total_words,
               COALESCE(r.duration, 0)    AS duration,
               r.end_time
        FROM results r
        JOIN participants p ON p.id = r.participant_id
        WHERE r.crossword_id = ?
        ORDER BY score DESC, duration ASC
    """, (id,)).fetchall()
    conn.close()

    return render_template('admin/results.html', results=results, crossword_id=id)


@app.route('/admin/crossword/delete/<int:id>', methods=['POST'])
@admin_required
def admin_delete_crossword(id):
    conn = sqlite3.connect(DATABASE)
    conn.execute("DELETE FROM crosswords      WHERE id=?", (id,))
    conn.execute("DELETE FROM participants    WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM results         WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM questions       WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM crossword_stats WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM comments        WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM vote_feedback   WHERE crossword_id=?", (id,))
    conn.commit()
    conn.close()
    logger.info(
        "admin_crossword_deleted",
        extra={"Operation": "AdminDeleteCrossword", "CrosswordId": id, "AdminId": session.get("admin_id"), "RequestId": g.get("request_id")},
    )
    flash("Խաչբառը ջնջվեց")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/crossword/toggle/<int:id>', methods=['POST'])
@admin_required
def admin_toggle_crossword(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT hidden FROM crosswords WHERE id=?", (id,)).fetchone()
    current = row['hidden'] if row else 0
    conn.execute("UPDATE crosswords SET hidden=? WHERE id=?", (0 if current else 1, id))
    conn.commit()
    conn.close()
    logger.info(
        "admin_crossword_visibility_toggled",
        extra={"Operation": "AdminToggleCrossword", "CrosswordId": id, "AdminId": session.get("admin_id"), "NewHidden": 0 if current else 1, "RequestId": g.get("request_id")},
    )
    flash("Խաչբառի տեսանելիությունը թարմացվեց")
    return redirect(url_for('admin_dashboard'))


@app.route("/admin/edit/<int:cw_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_full_crossword(cw_id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    cw = conn.execute("SELECT * FROM crosswords WHERE id=?", (cw_id,)).fetchone()
    if not cw:
        conn.close()
        flash("Խաչբառը գտնված չէ")
        return redirect(url_for('admin_dashboard'))

    cw_dict = dict(cw)
    cw_data = json.loads(cw_dict.get('data', '{}'))
    placed_questions = cw_data.get('placed', [])
    questions = conn.execute("SELECT * FROM questions WHERE crossword_id=?", (cw_id,)).fetchall()
    questions_list = [dict(q) for q in questions]
    bg_images = os.listdir(os.path.join(app.static_folder, "backgrounds"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        keyword = request.form.get("keyword", "").strip().upper()
        description = request.form.get("description", "").strip()
        grades_list = request.form.getlist("grades")
        grades_str = ",".join(grades_list) if grades_list else ""

        bg_file = request.files.get("bg_upload")
        bg_choice = request.form.get("bg_choice", "")
        bg_path = cw_dict.get("background", "")

        if bg_file and bg_file.filename:
            filename = secure_filename(bg_file.filename)
            filename = f"bg_{uuid.uuid4().hex[:12]}_{filename}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            bg_file.save(save_path)
            bg_path = f"uploads/{filename}"
        elif bg_choice:
            bg_path = f"backgrounds/{bg_choice}"

        new_questions = []
        for key in request.form:
            if key.startswith("question_"):
                index = key.split("_")[1]
                q_text = request.form.get(f"question_{index}", "").strip()
                a_text = request.form.get(f"answer_{index}", "").strip().upper()
                number = request.form.get(f"number_{index}", "").strip()
                direction = request.form.get(f"dir_{index}", "h")
                if q_text and a_text:
                    new_questions.append({
                        "question": q_text, "answer": a_text,
                        "number": int(number) if number.isdigit() else None,
                        "dir": direction
                    })

        new_grid = generate_crossword_grid(
            [{'q': q['question'], 'a': q['answer']} for q in new_questions], keyword
        ) or {}
        cw_data.update(new_grid)
        cw_data['description'] = description
        cw_data['grades'] = grades_list

        try:
            conn.execute("""
                UPDATE crosswords
                SET title=?, keyword=?, background=?, data=?, description=?, grade=?
                WHERE id=?
            """, (title, keyword, bg_path, json.dumps(cw_data, ensure_ascii=False),
                  description, normalize_to_json_list(grades_list), cw_id))

            conn.execute("DELETE FROM questions WHERE crossword_id=?", (cw_id,))
            for q in new_questions:
                conn.execute("""
                    INSERT INTO questions (crossword_id, question, answer, number, dir)
                    VALUES (?, ?, ?, ?, ?)
                """, (cw_id, q['question'], q['answer'], q['number'], q['dir']))

            conn.commit()
            logger.info(
                "admin_crossword_edited",
                extra={"Operation": "AdminEditFullCrossword", "CrosswordId": cw_id, "AdminId": session.get("admin_id"), "RequestId": g.get("request_id")},
            )
            flash("Խաչբառը հաջողությամբ թարմացվեց")
            return redirect(url_for("admin_dashboard"))
        except Exception as e:
            conn.rollback()
            logger.error(
                "admin_crossword_edit_failed",
                extra={"Operation": "AdminEditFullCrossword", "CrosswordId": cw_id, "RequestId": g.get("request_id")},
                exc_info=True,
            )
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for("admin_edit_full_crossword", cw_id=cw_id))

    conn.close()
    return render_template("admin/edit_full_crossword.html",
                           cw=cw_dict, placed=placed_questions,
                           questions=questions_list, bg_images=bg_images)


@app.route('/admin/change_credentials', methods=['GET', 'POST'])
@admin_required
def admin_change_credentials():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    admin = conn.execute("SELECT * FROM admins WHERE id = ?", (session['admin_id'],)).fetchone()

    if not admin:
        conn.close()
        flash("Ադմինը գտնված չէ")
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        new_username = request.form.get('username', '').strip()
        old_password = request.form.get('old_password', '')
        new_password = request.form.get('new_password', '')

        if not check_password_hash(admin['password'], old_password):
            # Սխալ հին գաղտնաբառ → Warning, password-ը երբեք չգրանցել
            logger.warning(
                "admin_change_credentials_wrong_old_password",
                extra={"Operation": "AdminChangeCredentials", "AdminId": admin['id'], "RequestId": g.get("request_id")},
            )
            flash("Հին գաղտնաբառը սխալ է ❌")
            conn.close()
            return render_template("admin/change_credentials.html", admin=admin)

        try:
            hashed = generate_password_hash(new_password)
            conn.execute("UPDATE admins SET username = ?, password = ? WHERE id = ?",
                         (new_username, hashed, admin['id']))
            conn.commit()
        except Exception as e:
            logger.error(
                "admin_change_credentials_failed",
                extra={"Operation": "AdminChangeCredentials", "AdminId": admin['id'], "RequestId": g.get("request_id")},
                exc_info=True,
            )
            flash(f"Սխալ՝ {e} ❌")
            conn.close()
            return render_template("admin/change_credentials.html", admin=admin)

        conn.close()
        session['admin'] = True
        session['admin_id'] = admin['id']
        logger.info(
            "admin_credentials_changed",
            extra={"Operation": "AdminChangeCredentials", "AdminId": admin['id'], "RequestId": g.get("request_id")},
        )
        flash("Տվյալները հաջողությամբ փոփոխվեցին ✅")
        return redirect(url_for('admin_dashboard'))

    conn.close()
    return render_template("admin/change_credentials.html", admin=admin)


@app.route('/whoami')
@require_auth()
async def whoami():
    return jsonify({"user_id": g.current_user["sub"], "claims": g.current_user})


# =====================================================
# 🔹 STARTUP
# =====================================================

if __name__ == '__main__':
    if not os.path.exists(os.path.join(BASE_DIR, 'dataset')):
        os.makedirs(os.path.join(BASE_DIR, 'dataset'))

    logger.info("startup_begin", extra={"Operation": "AppStartup", "Env": FLASK_ENV})

    check_and_add_creator_name_column()
    check_and_create_participants_table()
    check_and_add_hidden_column()
    check_and_add_total_words_column()
    check_and_add_is_ai_generated_column()
    check_and_add_keyword_material_columns()
    check_and_create_crossword_stats_table()
    check_and_add_share_count_column()
    check_and_create_comments_table()
    check_and_create_vote_feedback_table()
    check_and_add_created_at_column()
    check_and_add_show_creator_data_column()
    check_and_add_is_draft_column()
    ensure_admin_tables()
    check_and_add_voter_id_column()

    # ── .env-ի կարևոր արժեքների ստուգում ──
    if not SECRET_KEY:
        log_critical_startup_failure(
            logger, "secret_key_missing",
            Operation="AppStartup",
            Detail="SECRET_KEY-ը .env-ում չկա, օգտագործվում է dev fallback (production-ում անթույլատրելի է)",
        )

    if not MAINLOGIN_BASE:
        logger.warning(
            "mainlogin_base_missing",
            extra={"Operation": "AppStartup", "Detail": "MAINLOGIN_BASE-ը .env-ում չկա, profile-claims-ը չի աշխատի"},
        )

    if INTERNAL_API_KEY:
        # ⚠️ API key-ը երբեք ամբողջությամբ չգրանցել (PDF, գլուխ 8). միայն masked prefix/suffix + երկարություն
        logger.info(
            "internal_api_key_loaded",
            extra={
                "Operation": "AppStartup",
                "KeyPrefix": INTERNAL_API_KEY[:4] + "***",
                "KeyLength": len(INTERNAL_API_KEY),
            },
        )
    else:
        log_critical_startup_failure(
            logger, "internal_api_key_missing",
            Operation="AppStartup",
            Detail=".env չի բեռնվել կամ INTERNAL_API_KEY տողը բացակայում է",
        )

    ssl_context = (SSL_CERT_FILE, SSL_KEY_FILE) if USE_HTTPS else None

    logger.info("app_run", extra={"Operation": "AppStartup", "Host": HOST, "Port": PORT, "Debug": FLASK_DEBUG})

    try:
        app.run(host=HOST, port=PORT, debug=FLASK_DEBUG, ssl_context=ssl_context)
    except Exception as e:
        log_critical_startup_failure(
            logger, "app_terminated_unexpectedly", exception=e,
            Operation="AppStartup",
        )
        raise
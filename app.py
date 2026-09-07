import asyncio
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

import aiosqlite
import requests
from admin_api import admin_bp, ensure_admin_tables, get_settings, is_user_blocked
from ai_provider import get_provider
from auth_decorators import require_auth
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, get_flashed_messages
from flask import g
from utils.crossword_builder import generate_crossword_grid
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename


AM_TZ = timezone(timedelta(hours=4))

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# =====================================================
# 🔹 APP + CONFIG
# =====================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_fallback_key")
URL_PREFIX = os.getenv("URL_PREFIX", "")

# Ադմին պանելը սպասարկվում է AuthServer-ից (Pages/Admin/Crossword/*):
# Այստեղ միայն JSON API-ն է՝ /api/admin/*
app.register_blueprint(admin_bp)

# Import-ի պահին, ոչ թե __main__-ում. gunicorn/waitress-ի տակ __main__-ը
# չի կատարվում, իսկ առանց այս աղյուսակների admin API-ն կփլուզվեր:
ensure_admin_tables()


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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, 'utils', 'crossword.db')

UPLOAD_FOLDER = os.path.join('static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

API_URL = os.getenv("API_URL", "http://localhost:5000/api/ask")
X_API_KEY = os.getenv("X_API_KEY", "")

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

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


import logging

logging.basicConfig(level=logging.DEBUG)


@app.before_request
def log_request():
    app.logger.debug(f"📥 REQUEST: {request.method} {request.url}")


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
            timeout=15
        )
        response.encoding = "utf-8"
    except requests.RequestException as e:
        raise RuntimeError(f"❌ API-ի հետ կապի խնդիր: {e}")

    print("🟢 AI API RAW RESPONSE:", response.text)
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
    """Ծանուցում է այն հարցերի մասին, որոնց պատասխանները չհատվեցին և դուրս մնացին։"""
    skipped = (crossword_data or {}).get('skipped') or []
    if not skipped:
        return False
    lines = "\n".join(
        f"• {s.get('question', '') or '(առանց հարցի)'} — {s.get('word', '')}"
        for s in skipped
    )
    flash(
        "⚠️ Հետևյալ հարցերը չներառվեցին խաչբառում, քանի որ դրանց պատասխանները "
        "ընդհանուր տառ չունեն մյուս բառերի հետ և ոչ մի տեղ չեն հատվում․\n"
        + lines +
        "\n\nՓոխե՛ք այդ հարցերի պատասխանները , "
        "ապա նորից պահպանե՛ք։",
        "warning"
    )
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
        url = f"https://mainlogin.dshh.am:15107/api/users/{user_id}/profile-claims"
        resp = requests.get(url, timeout=10, verify=False)
        data = resp.json()
        if isinstance(data, dict):
            return data.get("claims", data)
        return {}
    except Exception as e:
        print("⚠️ profile-claims fetch error:", e)
        return {}


import os

MAINLOGIN_BASE = os.getenv("MAINLOGIN_BASE", "https://mainlogin.dshh.am:15107").rstrip("/")


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


@app.context_processor
def inject_user():
    """Ավտոմատ փոխանցում է `user`-ը ԲՈԼՈՐ կաղապարներին։"""
    try:
        return {"user": current_user_view()}
    except Exception:
        return {"user": {}}


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
        print("✅ crossword_stats table ready")
    except Exception as e:
        print("❌ crossword_stats error:", e)
    finally:
        conn.close()


# ── ՆՈՐ: share_count column (հին բազաների համար) ──────────────────────────────
def check_and_add_share_count_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crossword_stats ADD COLUMN share_count INTEGER DEFAULT 0;")
        conn.commit()
        print("✅ share_count սյունակը ավելացվեց")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"Սխալ share_count: {e}")
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
        print("✅ comments table ready")
    except Exception as e:
        print("❌ comments error:", e)
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
        print("✅ vote_feedback table ready")
    except Exception as e:
        print("❌ vote_feedback error:", e)
    finally:
        conn.close()


def check_and_add_created_at_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(e)
    finally:
        conn.close()


def check_and_add_total_words_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE results ADD COLUMN total_words INTEGER;")
        conn.commit()
        print("✅ total_words սյունակը ավելացվեց results աղյուսակում")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print("❌ Սխալ total_words:", e)
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
        print("Participants և Results աղյուսակները պատրաստ են։")
    except Exception as e:
        print(f"Սխալ աղյուսակները ստեղծելիս: {e}")
    finally:
        conn.close()


def check_and_add_hidden_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN hidden INTEGER DEFAULT 0;")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"Սխալ hidden: {e}")
    finally:
        conn.close()


def check_and_add_creator_name_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN creator_name TEXT;")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"Սխալ creator_name: {e}")
    finally:
        conn.close()


def check_and_add_is_ai_generated_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN is_ai_generated INTEGER DEFAULT 0;")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print("Սխալ:", e)
    finally:
        conn.close()


def check_and_add_keyword_material_columns():
    conn = sqlite3.connect(DATABASE)
    for col in ["keyword_material_type TEXT", "keyword_material_value TEXT"]:
        try:
            conn.execute(f"ALTER TABLE crosswords ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                print(f"Սխալ {col}:", e)
    conn.close()


# ── ՆՈՐ: show_creator_data column ────────────────────────────────────────────
def check_and_add_show_creator_data_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN show_creator_data TEXT DEFAULT 'no';")
        conn.commit()
        print("✅ show_creator_data սյունակը ավելացվեց")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"Սխալ show_creator_data: {e}")
    finally:
        conn.close()

def check_and_add_is_draft_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN is_draft INTEGER DEFAULT 0;")
        conn.commit()
        print("✅ is_draft սյունակը ավելացվեց")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"Սխալ is_draft: {e}")
    finally:
        conn.close()


@app.route('/api/creator-claims/<user_id>')
async def creator_claims(user_id):
    try:
        url = f"https://mainlogin.dshh.am:15107/api/users/{user_id}/profile-claims"
        resp = await asyncio.to_thread(
            lambda: requests.get(url, timeout=10, verify=False)
        )
        try:
            return jsonify(resp.json()), resp.status_code
        except Exception:
            return jsonify({"error": "Invalid response", "raw": resp.text[:300]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/debug_scd/<int:id>')
async def debug_scd(id):
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
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/crosswords/<int:id>/vote', methods=['POST'])
async def api_crossword_vote(id):
    try:
        data = request.get_json()
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
        return jsonify({"success": False, "error": str(e)}), 500


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

    try:
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO vote_feedback (crossword_id, vote_type, reasons, other_text, message)
                VALUES (?, ?, ?, ?, ?)
            """, (id, vote_type, reasons_json, other, message))

            await db.execute(f"""
                INSERT INTO crossword_stats (crossword_id, {col})
                VALUES (?, 1)
                ON CONFLICT(crossword_id) DO UPDATE
                SET {col} = {col} + 1
            """, (id,))
            await db.commit()
        return jsonify({"success": True})
    except Exception as e:
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
            await db.execute("DELETE FROM results      WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM participants WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM questions    WHERE crossword_id = ?", (id,))
            await db.execute("DELETE FROM crosswords   WHERE id = ?", (id,))
            await db.commit()

            return jsonify({"success": True}), 200

    except Exception as e:
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

    ai_model = request.form.get("ai_model", "claude")
    provider = get_provider(ai_model)
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
        if keyword_mode != "with":
            formatted_questions = await provider.generate_questions(topic, question_count, description)
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
        OTHER_AI_MODELS = {"claude"}  # ← սրանք «ԱՅԼ ԱԲ» են (հետագա մոդելներ ավելացրու այստեղ)
        print("🔍 ai_model =", repr(ai_model))
        crossword_data['ai_model'] = ai_model
        crossword_data['ai_kind'] = 'other_ai' if (ai_model or '').lower() in OTHER_AI_MODELS else 'dshh_ai'
        print("🔍 ai_kind =", crossword_data['ai_kind'])

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
            1,
            show_creator_data  # show_creator_data default
        )

        return redirect(url_for('edit_crossword', id=crossword_id, ai=1))

    except Exception as e:
        print("🛑 Սխալ:", str(e))
        flash("Խաչբառի ստեղծման ժամանակ առաջացավ սխալ։", "danger")
        return redirect(url_for('choose_keyword_mode'))


@app.route('/api/ai-questions-for-edit', methods=['POST'])
async def ai_questions_for_edit():
    data = request.get_json()
    topic = data.get('topic', '').strip()
    subject = data.get('subject', '')
    grade = data.get('grade', '')
    count = int(data.get('count', 3))
    existing = data.get('existing', [])

    if not topic:
        return jsonify({"error": "Թեմա չի նշված"}), 400

    full_topic = topic
    if subject:
        full_topic += f" ({subject})"
    if grade:
        full_topic += f" {grade}-րդ դասարան"

    try:
        existing_answers = set(q.get("answer", "").strip().upper() for q in existing)
        provider = get_provider("claude")
        pairs = await provider.generate_questions(full_topic, count)
        filtered_pairs = [p for p in pairs if p["a"].strip().upper() not in existing_answers]

        return jsonify({
            "pairs": [{"question": p["q"], "answer": p["a"]} for p in filtered_pairs]
        })

    except Exception as e:
        print("❌ ai_questions_for_edit սխալ:", e)
        return jsonify({"error": str(e)}), 500




@app.route('/api/ai-edit-crossword', methods=['POST'])
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
        provider = get_provider("claude")
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
            print("⚠️ AI raw:", raw[:300])
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
        print("❌ ai_edit_crossword սխալ:", repr(e))
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
async def upload_bg():
    if 'bg_file' not in request.files:
        return jsonify({'success': False, 'error': 'Ֆայլ չի ընտրվել'})

    file = request.files['bg_file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Ֆայլի անունը բացակայում է'})

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        await save_file_async(file, save_path)
        return jsonify({'success': True, 'filename': filename, 'url': f'/static/uploads/{filename}'})
    else:
        return jsonify({'success': False, 'error': 'Ֆայլի ֆորմատը թույլատրված չէ'})


@app.route('/delete-bg', methods=['POST'])
async def delete_bg():
    data = request.get_json()
    filename = data.get('filename')
    if not filename:
        return jsonify({'success': False, 'error': 'Ֆայլի անունը բացակայում է'})

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(file_path):
        await delete_file_async(file_path)
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
    except Exception:
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
        conn.execute("DELETE FROM crosswords WHERE id = ?", (id,))
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
                fname = f"qmedia_{int(time.time())}_{index}.{ext}"
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
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
        await save_file_async(file, save_path)
        bg_path = f'uploads/{filename}'
    elif bg_choice:
        bg_path = f'backgrounds/{bg_choice}'

    # Փորձում ենք ցանց կառուցել, բայց սխալի դեպքում պարզապես անտեսում ենք
    try:
        crossword_data = generate_crossword_grid(questions, keyword) or {}
    except Exception:
        crossword_data = {}

    # Միշտ պահում ենք raw հարցերը՝ resume-ի համար (սա է Ա7-ի հիմքը)
    crossword_data['draft_questions'] = questions
    crossword_data.update({
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
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({
        "success": True,
        "id": crossword_id,
        "editUrl": url_for('edit_crossword', id=crossword_id)
    })


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
        if not selected_subjects_raw:
            selected_subjects_raw = request.form.get('subject', '').strip()

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
            filename = f"hint_{int(time.time())}.{ext}"
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
            filename = f"kwmat_{int(time.time())}.{ext}"
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
            filename = secure_filename(file.filename)
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
                q = request.form.get(f'question_{index}')
                a = request.form.get(f'answer_{index}')
                if q and a:
                    item = {'q': q.strip(), 'a': a.strip().upper()}
                    media_file = request.files.get(f'q_media_{index}')
                    if media_file and media_file.filename:
                        ext = media_file.filename.rsplit('.', 1)[-1].lower() if '.' in media_file.filename else 'bin'
                        fname = f"qmedia_{int(time.time())}_{index}.{ext}"
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

        try:
            crossword_id = await asyncio.to_thread(
                db_operation_wrapper,
                insert_new_crossword_db,
                g.current_user["sub"],
                title, keyword, bg_path, crossword_data,
                description, creator_name, subjects_list, grades_list, 0,
                show_creator_data
            )
        except Exception as e:
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for('input_questions'))

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
            filename = f"bg_{int(time.time())}_{filename}"
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
                if q and a:
                    item = {'q': q, 'a': a}

                    media_val = request.form.get(f'q_media_existing_{index}', '').strip()
                    media_type = request.form.get(f'q_media_existing_type_{index}', '').strip()

                    if request.form.get(f'remove_q_media_{index}', '0') == '1':
                        media_val, media_type = '', ''

                    media_file = request.files.get(f'q_media_{index}')
                    if media_file and media_file.filename:
                        ext = media_file.filename.rsplit('.', 1)[-1].lower() if '.' in media_file.filename else 'bin'
                        fname = f"qmedia_{int(time.time())}_{index}.{ext}"
                        save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                        await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
                        await save_file_async(media_file, save_path)
                        media_val = f"uploads/{fname}"
                        media_type = 'audio' if ext in AUDIO_EXT else 'image'

                        if media_val:
                            item['q_media'] = media_val
                            item['q_media_type'] = media_type

                        questions.append(item)

                # ── ՆՈՐ՝ բանալի բառով խաչբառի ստուգում ──────────────────────────
                if cw.get('keyword'):  # այս խաչբառը ի սկզբանե բանալի բառով է
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
        global_hint = None

        if hint_type == 'text':
            global_hint = global_hint_text
            global_hint_media = None
        elif hint_type in ('audio', 'video', 'image'):
            if file_hint and file_hint.filename:
                filename = secure_filename(file_hint.filename)
                filename = f"hint_{int(time.time())}_{filename}"
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
            filename = f"kwmat_{int(time.time())}.{ext}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(keyword_material_file, save_path)
            keyword_material_value = f"uploads/{filename}"
        elif keyword_material_type == 'link':
            keyword_material_value = request.form.get('keyword_material_link', '').strip()
        elif keyword_material_type == '':
            keyword_material_value = ''

        crossword_data.update({
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

        try:
            has_skipped = bool((crossword_data or {}).get('skipped'))
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
            return redirect(url_for('edit_crossword', id=id))

    crossword_json = json.loads(cw['data'])

    # ── Սևագրի fallback՝ եթե ցանցը դատարկ է, հարցերը վերցնում ենք draft_questions-ից ──
    if not crossword_json.get('placed') and crossword_json.get('draft_questions'):
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
        flash('Խաչբառը ջնջվեց։' if deleted else 'Չեք կարող ջնջել այս խաչբառը։')
    except Exception as e:
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
    except Exception:
        pass

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


@app.route('/dashboard-debug')
def dashboard_debug():
    auth = request.headers.get("Authorization", "ՉԿԱ")
    return jsonify({"auth_header": auth[:100], "all_headers": dict(request.headers)})


@app.route('/debug')
async def debug():
    def get_tables_list(conn):
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return c.fetchall()

    tables = await asyncio.to_thread(db_operation_wrapper, get_tables_list)
    return f"Բազայում առկա աղյուսակներ՝ {tables}"


@app.route('/crossword_text/<int:id>')
async def crossword_text(id):
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

    if INTERNAL_API_KEY:
        print(
            f"🔑 INTERNAL_API_KEY loaded: {INTERNAL_API_KEY[:6]}…{INTERNAL_API_KEY[-4:]} (len={len(INTERNAL_API_KEY)})")
    else:
        print("❌ INTERNAL_API_KEY EMPTY — .env չի բեռնվել կամ տողը բացակայում է!")

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "15400"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    ssl_context = None
    if os.getenv("USE_HTTPS", "false").lower() == "true":
        ssl_context = ("certs/cert.pem", "certs/key.pem")

    app.run(host=host, port=port, debug=debug, ssl_context=ssl_context)


import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, g, jsonify, request

from auth import introspect
import hashlib
from logging_config import LOG_DIR
from utils import admin_query

AM_TZ = timezone(timedelta(hours=4))

admin_bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "utils", "crossword.db"))

# Լրիվ իրավունք vs միայն դիտում
WRITE_ROLES = ("Admin",)
READ_ROLES = ("Admin", "AdminViewer")


# =====================================================
# 🔹 DB
# =====================================================

def _conn():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_admin_tables():
    """Ադմին պանելի համար անհրաժեշտ աղյուսակներն ու սյունակները:"""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_blocked_users (
                user_id    TEXT PRIMARY KEY,
                reason     TEXT,
                blocked_by TEXT,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                actor       TEXT,
                action      TEXT NOT NULL,
                entity_type TEXT,
                entity_id   TEXT,
                detail      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_audit_created ON admin_audit_log (created_at DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_by TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in ["hidden_reason TEXT", "hidden_at TIMESTAMP"]:
            try:
                conn.execute(f"ALTER TABLE crosswords ADD COLUMN {col}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        conn.commit()
        print("✅ admin_api tables ready")
    except Exception as e:
        print("❌ admin_api table error:", e)
    finally:
        conn.close()


# =====================================================
# 🔹 GLOBAL SWITCHES
# =====================================================

# Ամբողջ համակարգի համար գործող անջատիչներ: Բոլորը False են լռելյայն,
# այսինքն՝ ոչինչ արգելված չէ, քանի դեռ ադմինը չի միացրել:
SETTING_KEYS = (
    "creation_locked",         # ոչ ոք չի կարող նոր խաչբառ ստեղծել
    "ai_creation_locked",      # AI-ով ստեղծելը անջատված է
    "keyword_creation_locked",  # keyword ռեժիմով ստեղծելը անջատված է
    "sharing_locked",          # կիսվելը անջատված է
    "comments_locked",         # մեկնաբանելը անջատված է
    "all_crosswords_hidden",   # բոլոր խաչբառերը թաքցված են հանրությունից
)


def get_settings():
    """Բոլոր անջատիչները՝ որպես {key: bool}: Երբեք չի բարձրացնում սխալ."""
    values = {k: False for k in SETTING_KEYS}
    try:
        conn = _conn()
    except sqlite3.Error:
        return values
    try:
        for r in conn.execute("SELECT key, value FROM admin_settings"):
            if r["key"] in values:
                values[r["key"]] = str(r["value"]).lower() in ("1", "true", "yes")
    except sqlite3.OperationalError:
        pass  # աղյուսակը դեռ չկա
    finally:
        conn.close()
    return values


def is_locked(key):
    """app.py-ից՝ մեկ անջատիչ ստուգելու կարճ ձև."""
    return get_settings().get(key, False)


# =====================================================
# 🔹 AUTH
# =====================================================

def _internal_key():
    return os.getenv("INTERNAL_API_KEY", "")


def admin_guard(*roles):
    """X-Api-Key կամ Bearer token + role:"""
    allowed = set(r.lower() for r in (roles or READ_ROLES))

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = _internal_key()
            provided = request.headers.get("X-Api-Key", "")
            if key and provided == key:
                g.admin_actor = request.headers.get("X-Admin-Actor") or "authserver"
                g.admin_can_write = True
                return fn(*args, **kwargs)

            authz = request.headers.get("Authorization", "")
            if not authz.lower().startswith("bearer "):
                return jsonify(error="Missing Bearer token"), 401

            user = introspect(authz.split(" ", 1)[1].strip())
            if user is None:
                return jsonify(error="Invalid or expired token"), 401

            user_roles = {str(r).strip().lower() for r in (user.get("roles") or [])}
            if not (user_roles & allowed):
                return jsonify(error="Forbidden"), 403

            g.admin_actor = user.get("name") or user.get("email") or user.get("sub")
            g.admin_can_write = "admin" in user_roles
            g.current_user = user
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _require_write():
    """AdminViewer-ը չի կարող փոփոխել:"""
    if not getattr(g, "admin_can_write", False):
        return jsonify(error="Forbidden — read-only role"), 403
    return None


def _audit(conn, action, entity_type=None, entity_id=None, detail=None):
    conn.execute(
        "INSERT INTO admin_audit_log (actor, action, entity_type, entity_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (getattr(g, "admin_actor", "unknown"), action, entity_type,
         str(entity_id) if entity_id is not None else None, detail),
    )


# =====================================================
# 🔹 HELPERS
# =====================================================

def _paged():
    page = max(1, request.args.get("page", 1, type=int) or 1)
    count = request.args.get("count", 50, type=int) or 50
    count = max(1, min(count, 500))
    return page, count


def _queried(items, paged=True):
    """?sort / ?filter (admin_query) կիրառում է արդեն պատրաստ items-ի վրա:

    Վերադարձնում է (response_dict, None) կամ (None, 400-ի պատասխան):
    paged=True դեպքում արդյունքը կտրվում է ?page / ?count-ով, totalCount-ը՝
    ֆիլտրից հետո:
    """
    try:
        items = admin_query.apply_request(items, request.args)
    except admin_query.QueryError as e:
        return None, (jsonify(e.to_dict()), 400)
    if not paged:
        return {"totalCount": len(items), "items": items}, None
    page, count = _paged()
    return {
        "totalCount": len(items),
        "page": page,
        "pageSize": count,
        "items": admin_query.page_slice(items, page, count),
    }, None


def _get(row, key, default=None):
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return v if v is not None else default


def _blocked_ids(conn):
    return {r["user_id"] for r in conn.execute("SELECT user_id FROM admin_blocked_users")}


def _cw_row(r, blocked=None):
    hidden = bool(_get(r, "hidden"))
    # Հարցերի քանակը data JSON placed-ից (questions աղյուսակը սովորաբար դատարկ է)
    try:
        _placed = json.loads(_get(r, "data", "") or "{}").get("placed", [])
        _qcount = sum(1 for p in _placed
                      if p.get("question") and p.get("question") != "Բանալի բառ")
    except Exception:
        _qcount = _get(r, "question_count", 0)
    return {
        "id": str(r["id"]),
        "title": _get(r, "title", ""),
        "keyword": _get(r, "keyword", ""),
        "subject": _get(r, "subject", ""),
        "grade": _get(r, "grade", ""),
        "description": _get(r, "description", ""),
        "creatorName": _get(r, "creator_name", ""),
        "creatorId": str(_get(r, "user_id", "")),
        "creatorBlocked": bool(blocked and str(_get(r, "user_id", "")) in blocked),
        "status": "Hidden" if hidden else "Visible",
        "isHidden": hidden,
        "hiddenReason": _get(r, "hidden_reason", ""),
        "isAiGenerated": bool(_get(r, "is_ai_generated")),
        "isDraft": bool(_get(r, "is_draft")),
        "createdAt": _get(r, "created_at", ""),
        "questionCount": _qcount,
        "participantCount": _get(r, "participant_count", 0),
        "completedCount": _get(r, "completed_count", 0),
        "viewCount": _get(r, "view_count", 0),
        "likeCount": _get(r, "like_count", 0),
        "dislikeCount": _get(r, "dislike_count", 0),
        "shareCount": _get(r, "share_count", 0),
        "downloadCount": _get(r, "download_count", 0),
        "printCount": _get(r, "print_count", 0),
        "commentCount": _get(r, "comment_count", 0),
    }


# Ցուցակի հիմնական SELECT-ը՝ բոլոր հաշվիչներով
_CW_SELECT = """
    SELECT c.*,
           (SELECT COUNT(*) FROM questions    q WHERE q.crossword_id = c.id) AS question_count,
           (SELECT COUNT(*) FROM participants p WHERE p.crossword_id = c.id) AS participant_count,
           (SELECT COUNT(*) FROM results      r WHERE r.crossword_id = c.id
                                                 AND r.is_completed = 1)     AS completed_count,
           (SELECT COUNT(*) FROM comments     m WHERE m.crossword_id = c.id) AS comment_count,
           s.view_count, s.like_count, s.dislike_count,
           s.share_count, s.download_count, s.print_count
    FROM crosswords c
    LEFT JOIN crossword_stats s ON s.crossword_id = c.id
"""


# =====================================================
# 🔹 DASHBOARD
# =====================================================

@admin_bp.route("/stats", methods=["GET"])
@admin_guard(*READ_ROLES)
def admin_stats():
    conn = _conn()
    try:
        def one(sql, params=()):
            row = conn.execute(sql, params).fetchone()
            return (row[0] or 0) if row else 0

        total_cw = one("SELECT COUNT(*) FROM crosswords")
        hidden_cw = one("SELECT COUNT(*) FROM crosswords WHERE hidden = 1")
        draft_cw = one("SELECT COUNT(*) FROM crosswords WHERE COALESCE(is_draft, 0) = 1")
        ai_cw = one("SELECT COUNT(*) FROM crosswords WHERE COALESCE(is_ai_generated, 0) = 1")
                # Հարցերն ապրում են data JSON placed-ում, ոչ questions աղյուսակում
        total_q = 0
        for _r in conn.execute("SELECT data FROM crosswords"):
            try:
                _pl = json.loads(_r["data"] or "{}").get("placed", [])
                total_q += sum(1 for p in _pl
                               if p.get("question") and p.get("question") != "Բանալի բառ")
            except Exception:
                pass
        total_p = one("SELECT COUNT(*) FROM participants")
        total_r = one("SELECT COUNT(*) FROM results")
        completed = one("SELECT COUNT(*) FROM results WHERE is_completed = 1")
        creators = one("SELECT COUNT(DISTINCT user_id) FROM crosswords WHERE user_id IS NOT NULL")
        blocked = one("SELECT COUNT(*) FROM admin_blocked_users")
        comments = one("SELECT COUNT(*) FROM comments")

        avg_score = conn.execute(
            "SELECT AVG(1.0 * score / NULLIF(total_words, 0)) FROM results "
            "WHERE is_completed = 1 AND total_words > 0"
        ).fetchone()[0]
        avg_duration = conn.execute(
            "SELECT AVG(duration) FROM results WHERE is_completed = 1 AND duration > 0"
        ).fetchone()[0]

        views = one("SELECT SUM(view_count) FROM crossword_stats")
        likes = one("SELECT SUM(like_count) FROM crossword_stats")
        dislikes = one("SELECT SUM(dislike_count) FROM crossword_stats")
        downloads = one("SELECT SUM(download_count) FROM crossword_stats")
        shares = one("SELECT SUM(share_count) FROM crossword_stats")
        prints = one("SELECT SUM(print_count) FROM crossword_stats")

        # Activity window for the per-day series and the in-period counts.
        # Default 30 keeps the previous behaviour; the engagement totals above
        # come from counter columns without timestamps, so they stay all-time.
        days = request.args.get("days", 30, type=int) or 30
        days = max(1, min(days, 365))
        since = (datetime.now(AM_TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        crosswords_in_period = one(
            "SELECT COUNT(*) FROM crosswords WHERE created_at >= ?", (since,))
        participants_in_period = one(
            "SELECT COUNT(*) FROM participants WHERE start_time >= ?", (since,))

        daily = [
            {"date": r["d"], "crosswords": r["n"]}
            for r in conn.execute(
                "SELECT DATE(created_at) AS d, COUNT(*) AS n FROM crosswords "
                "WHERE created_at >= ? GROUP BY DATE(created_at) ORDER BY d", (since,)
            )
        ]
        daily_p = [
            {"date": r["d"], "participants": r["n"]}
            for r in conn.execute(
                "SELECT DATE(start_time) AS d, COUNT(*) AS n FROM participants "
                "WHERE start_time >= ? GROUP BY DATE(start_time) ORDER BY d", (since,)
            )
        ]

        top = [
            {
                "id": str(r["id"]),
                "title": r["title"] or "",
                "creatorName": r["creator_name"] or "",
                "participantCount": r["participant_count"] or 0,
                "viewCount": r["view_count"] or 0,
            }
            for r in conn.execute(
                "SELECT c.id, c.title, c.creator_name, s.view_count, "
                "  (SELECT COUNT(*) FROM participants p WHERE p.crossword_id = c.id) AS participant_count "
                "FROM crosswords c LEFT JOIN crossword_stats s ON s.crossword_id = c.id "
                "ORDER BY participant_count DESC, COALESCE(s.view_count, 0) DESC LIMIT 10"
            )
        ]

        # subject-ը պահվում է որպես JSON list string (["Մայրենի"]) — parse ենք անում
        # ու հաշվում ամեն առանձին առարկան առանձին
        from collections import Counter
        subj_counter = Counter()
        for r in conn.execute("SELECT subject FROM crosswords"):
            raw = r["subject"]
            if not raw or not str(raw).strip():
                subj_counter["—"] += 1
                continue
            try:
                parsed = json.loads(raw)
                items = parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                items = [raw]
            names = [str(x).strip() for x in items if str(x).strip()]
            if not names:
                subj_counter["—"] += 1
            for name in names:
                subj_counter[name] += 1
        subjects = [
            {"subject": name, "count": cnt}
            for name, cnt in subj_counter.most_common(12)
        ]

        return jsonify({
            "totalCrosswords": total_cw,
            "visibleCrosswords": total_cw - hidden_cw,
            "hiddenCrosswords": hidden_cw,
            "draftCrosswords": draft_cw,
            "aiGeneratedCrosswords": ai_cw,
            "totalQuestions": total_q,
            "totalParticipants": total_p,
            "totalResults": total_r,
            "completedResults": completed,
            "completionRate": round(100.0 * completed / total_r, 1) if total_r else 0.0,
            "averageScorePercent": round(100.0 * avg_score, 1) if avg_score else 0.0,
            "averageDurationSeconds": int(avg_duration) if avg_duration else 0,
            "totalCreators": creators,
            "blockedCreators": blocked,
            "totalComments": comments,
            "totalViews": views,
            "totalLikes": likes,
            "totalDislikes": dislikes,
            "totalDownloads": downloads,
            "totalShares": shares,
            "totalPrints": prints,
            "periodDays": days,
            "crosswordsInPeriod": crosswords_in_period,
            "participantsInPeriod": participants_in_period,
            "crosswordsPerDay": daily,
            "participantsPerDay": daily_p,
            "topCrosswords": top,
            "bySubject": subjects,
        })
    finally:
        conn.close()


# =====================================================
# 🔹 CROSSWORDS
# =====================================================

@admin_bp.route("/crosswords", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_crosswords():
    page, count = _paged()
    q = (request.args.get("q") or "").strip()
    subject = (request.args.get("subject") or "").strip()
    grade = (request.args.get("grade") or "").strip()
    status = (request.args.get("status") or "").strip().lower()

    where, params = [], []
    if q:
        where.append("(c.title LIKE ? OR c.creator_name LIKE ? OR c.keyword LIKE ?)")
        params += [f"%{q}%"] * 3
    if subject:
        where.append("c.subject = ?")
        params.append(subject)
    if grade:
        where.append("c.grade = ?")
        params.append(grade)
    if status == "hidden":
        where.append("c.hidden = 1")
    elif status == "visible":
        where.append("COALESCE(c.hidden, 0) = 0")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = _conn()
    try:
        if admin_query.has_query(request.args):
            # sort/filter-ը աշխատում է JSON դաշտերի վրա → նախ բոլոր տողերը, հետո էջը
            rows = conn.execute(f"{_CW_SELECT}{clause} ORDER BY c.id DESC", params).fetchall()
            blocked = _blocked_ids(conn)
            body, err = _queried([_cw_row(r, blocked) for r in rows])
            return err or jsonify(body)

        total = conn.execute(
            f"SELECT COUNT(*) FROM crosswords c{clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"{_CW_SELECT}{clause} ORDER BY c.id DESC LIMIT ? OFFSET ?",
            params + [count, (page - 1) * count],
        ).fetchall()
        blocked = _blocked_ids(conn)
        return jsonify({
            "totalCount": total,
            "page": page,
            "pageSize": count,
            "items": [_cw_row(r, blocked) for r in rows],
        })
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>", methods=["GET"])
@admin_guard(*READ_ROLES)
def get_crossword(cw_id):
    conn = _conn()
    try:
        row = conn.execute(f"{_CW_SELECT} WHERE c.id = ?", (cw_id,)).fetchone()
        if not row:
            return jsonify(error="Not found"), 404
        item = _cw_row(row, _blocked_ids(conn))
        # Հարցերն ապրում են data JSON placed-ում, ոչ թե questions աղյուսակում
        try:
            placed = (json.loads(row["data"] or "{}")).get("placed", [])
        except Exception:
            placed = []
        item["questions"] = [
            {"id": str(i), "question": p.get("question", ""), "answer": p.get("word", "")}
            for i, p in enumerate(placed)
            if p.get("question") and p.get("question") != "Բանալի բառ" and p.get("word")
        ]
        return jsonify(item)
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>/update", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def update_crossword(cw_id):
    denied = _require_write()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    fields, params = [], []
    for key, col in (("title", "title"), ("subject", "subject"), ("grade", "grade"),
                     ("description", "description"), ("keyword", "keyword"),
                     ("creatorName", "creator_name")):
        if key in body:
            fields.append(f"{col} = ?")
            params.append(body[key])

    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM crosswords WHERE id = ?", (cw_id,)).fetchone():
            return jsonify(error="Not found"), 404

        if fields:
            conn.execute(
                f"UPDATE crosswords SET {', '.join(fields)} WHERE id = ?", params + [cw_id]
            )

        # Հարցերն ապրում են data JSON placed-ում. Խմբագրում ենք ՄԻԱՅՆ տեքստը,
        # ցանցի երկրաչափությունը (x, y, dir, number) թողնում ենք անփոփոխ։
        questions = body.get("questions")
        if isinstance(questions, list):
            drow = conn.execute("SELECT data FROM crosswords WHERE id = ?", (cw_id,)).fetchone()
            try:
                data_json = json.loads(drow["data"] or "{}")
            except Exception:
                data_json = {}
            placed = data_json.get("placed", [])
            for i, item in enumerate(questions):
                if i >= len(placed):
                    break
                q = (item.get("question") or "").strip()
                if q:
                    placed[i]["question"] = q
                # ⚠️ answer/word-ը ՉԵՆՔ փոխում — տես ներքևի զգուշացումը
            data_json["placed"] = placed
            conn.execute(
                "UPDATE crosswords SET data = ? WHERE id = ?",
                (json.dumps(data_json, ensure_ascii=False), cw_id),
            )

        _audit(conn, "crossword.update", "crossword", cw_id,
               json.dumps(sorted(body.keys()), ensure_ascii=False))
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>/hide", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def hide_crossword(cw_id):
    denied = _require_write()
    if denied:
        return denied

    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return jsonify(error="reason is required"), 400

    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE crosswords SET hidden = 1, hidden_reason = ?, hidden_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (reason, cw_id),
        )
        if cur.rowcount == 0:
            return jsonify(error="Not found"), 404
        _audit(conn, "crossword.hide", "crossword", cw_id, reason)
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>/unhide", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def unhide_crossword(cw_id):
    denied = _require_write()
    if denied:
        return denied

    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE crosswords SET hidden = 0, hidden_reason = NULL, hidden_at = NULL "
            "WHERE id = ?", (cw_id,),
        )
        if cur.rowcount == 0:
            return jsonify(error="Not found"), 404
        _audit(conn, "crossword.unhide", "crossword", cw_id)
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>/delete", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def delete_crossword_admin(cw_id):
    denied = _require_write()
    if denied:
        return denied

    conn = _conn()
    try:
        row = conn.execute("SELECT title FROM crosswords WHERE id = ?", (cw_id,)).fetchone()
        if not row:
            return jsonify(error="Not found"), 404

        # Կապված տողերը՝ ձեռքով (results-ը ON DELETE CASCADE չունի)
        conn.execute(
            "DELETE FROM results WHERE crossword_id = ? OR participant_id IN "
            "(SELECT id FROM participants WHERE crossword_id = ?)", (cw_id, cw_id),
        )
        conn.execute("DELETE FROM participants    WHERE crossword_id = ?", (cw_id,))
        conn.execute("DELETE FROM questions       WHERE crossword_id = ?", (cw_id,))
        conn.execute("DELETE FROM comments        WHERE crossword_id = ?", (cw_id,))
        conn.execute("DELETE FROM vote_feedback   WHERE crossword_id = ?", (cw_id,))
        conn.execute("DELETE FROM crossword_stats WHERE crossword_id = ?", (cw_id,))
        conn.execute("DELETE FROM crosswords      WHERE id = ?", (cw_id,))
        _audit(conn, "crossword.delete", "crossword", cw_id, row["title"])
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/crosswords/<int:cw_id>/results", methods=["GET"])
@admin_guard(*READ_ROLES)
def crossword_results(cw_id):
    conn = _conn()
    try:
        cw = conn.execute(
            "SELECT id, title, creator_name FROM crosswords WHERE id = ?", (cw_id,)
        ).fetchone()
        if not cw:
            return jsonify(error="Not found"), 404

        rows = conn.execute("""
            SELECT p.id, p.name, p.surname, p.gender, p.school, p.grade, p.start_time,
                   r.score, r.duration, r.is_completed, r.end_time, r.total_words
            FROM participants p
            LEFT JOIN results r ON r.participant_id = p.id
            WHERE p.crossword_id = ?
            ORDER BY p.id DESC
        """, (cw_id,)).fetchall()

        items = []
        for r in rows:
            total_words = r["total_words"] or 0
            score = r["score"] or 0
            items.append({
                "id": str(r["id"]),
                "name": r["name"] or "",
                "surname": r["surname"] or "",
                "gender": r["gender"] or "",
                "school": r["school"] or "",
                "grade": r["grade"] or "",
                "startTime": r["start_time"] or "",
                "endTime": r["end_time"] or "",
                "score": score,
                "totalWords": total_words,
                "scorePercent": round(100.0 * score / total_words, 1) if total_words else 0.0,
                "durationSeconds": r["duration"] or 0,
                "isCompleted": bool(r["is_completed"]),
            })

        if admin_query.has_query(request.args):
            try:
                items = admin_query.apply_request(items, request.args)
            except admin_query.QueryError as e:
                return jsonify(e.to_dict()), 400

        return jsonify({
            "crosswordId": str(cw["id"]),
            "crosswordTitle": cw["title"] or "",
            "creatorName": cw["creator_name"] or "",
            "totalCount": len(items),
            "items": items,
        })
    finally:
        conn.close()


# =====================================================
# 🔹 USERS (creators)
# =====================================================

@admin_bp.route("/users", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_users():
    q = (request.args.get("q") or "").strip().lower()
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT c.user_id,
                   MAX(c.creator_name)                           AS creator_name,
                   COUNT(*)                                      AS crossword_count,
                   SUM(CASE WHEN c.hidden = 1 THEN 1 ELSE 0 END) AS hidden_count,
                   SUM(CASE WHEN COALESCE(c.is_ai_generated, 0) = 1 THEN 1 ELSE 0 END) AS ai_count,
                   MIN(c.created_at)                             AS first_created,
                   MAX(c.created_at)                             AS last_created,
                   (SELECT COUNT(*) FROM participants p
                     WHERE p.crossword_id IN
                           (SELECT x.id FROM crosswords x WHERE x.user_id = c.user_id)
                   ) AS participant_count
            FROM crosswords c
            WHERE c.user_id IS NOT NULL AND TRIM(c.user_id) <> ''
            GROUP BY c.user_id
            ORDER BY crossword_count DESC
        """).fetchall()

        blocked = {r["user_id"]: r for r in conn.execute("SELECT * FROM admin_blocked_users")}

        items = []
        for r in rows:
            uid = str(r["user_id"])
            name = r["creator_name"] or ""
            if q and q not in name.lower() and q not in uid.lower():
                continue
            b = blocked.get(uid)
            items.append({
                "id": uid,
                "creatorName": name,
                "crosswordCount": r["crossword_count"] or 0,
                "hiddenCount": r["hidden_count"] or 0,
                "aiCount": r["ai_count"] or 0,
                "participantCount": r["participant_count"] or 0,
                "firstCreatedAt": r["first_created"] or "",
                "lastCreatedAt": r["last_created"] or "",
                "isBlocked": b is not None,
                "blockReason": (b["reason"] if b else "") or "",
                "blockedAt": (b["blocked_at"] if b else "") or "",
            })

        if admin_query.has_query(request.args):
            body, err = _queried(items, paged=False)
            return err or jsonify(body)
        return jsonify({"totalCount": len(items), "items": items})
    finally:
        conn.close()


@admin_bp.route("/users/<user_id>/block", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def block_user(user_id):
    denied = _require_write()
    if denied:
        return denied

    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return jsonify(error="reason is required"), 400

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO admin_blocked_users (user_id, reason, blocked_by, blocked_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, "
            "  blocked_by = excluded.blocked_by, blocked_at = CURRENT_TIMESTAMP",
            (user_id, reason, getattr(g, "admin_actor", "unknown")),
        )
        _audit(conn, "user.block", "user", user_id, reason)
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/users/<user_id>/unblock", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def unblock_user(user_id):
    denied = _require_write()
    if denied:
        return denied

    conn = _conn()
    try:
        conn.execute("DELETE FROM admin_blocked_users WHERE user_id = ?", (user_id,))
        _audit(conn, "user.unblock", "user", user_id)
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


def is_user_blocked(user_id):
    """app.py-ից օգտագործվում է՝ արգելափակված օգտատիրոջ ստեղծագործելը կասեցնելու համար:"""
    if not user_id:
        return False
    conn = _conn()
    try:
        return conn.execute(
            "SELECT 1 FROM admin_blocked_users WHERE user_id = ?", (str(user_id),)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


# =====================================================
# 🔹 MODERATION (comments + feedback)
# =====================================================

@admin_bp.route("/comments", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_comments():
    page, count = _paged()
    q = (request.args.get("q") or "").strip()

    where, params = [], []
    if q:
        where.append("(m.comment_text LIKE ? OR m.name LIKE ? OR c.title LIKE ?)")
        params += [f"%{q}%"] * 3
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    def item(r):
        return {
            "id": str(r["id"]),
            "crosswordId": str(r["crossword_id"]),
            "crosswordTitle": r["title"] or "",
            "author": r["name"] or "",
            "text": r["comment_text"] or "",
            "createdAt": r["created_at"] or "",
        }

    select = ("SELECT m.id, m.crossword_id, m.name, m.comment_text, m.created_at, c.title "
              f"FROM comments m LEFT JOIN crosswords c ON c.id = m.crossword_id{clause} "
              "ORDER BY m.id DESC")

    conn = _conn()
    try:
        if admin_query.has_query(request.args):
            body, err = _queried([item(r) for r in conn.execute(select, params)])
            return err or jsonify(body)

        total = conn.execute(
            "SELECT COUNT(*) FROM comments m "
            f"LEFT JOIN crosswords c ON c.id = m.crossword_id{clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            select + " LIMIT ? OFFSET ?",
            params + [count, (page - 1) * count],
        ).fetchall()
        return jsonify({
            "totalCount": total,
            "page": page,
            "pageSize": count,
            "items": [item(r) for r in rows],
        })
    finally:
        conn.close()


@admin_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def delete_comment(comment_id):
    denied = _require_write()
    if denied:
        return denied

    conn = _conn()
    try:
        row = conn.execute(
            "SELECT crossword_id, comment_text FROM comments WHERE id = ?", (comment_id,)
        ).fetchone()
        if not row:
            return jsonify(error="Not found"), 404

        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        conn.execute(
            "UPDATE crossword_stats SET comment_count = MAX(0, COALESCE(comment_count, 0) - 1) "
            "WHERE crossword_id = ?", (row["crossword_id"],),
        )
        _audit(conn, "comment.delete", "comment", comment_id, row["comment_text"])
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


@admin_bp.route("/feedback", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_feedback():
    page, count = _paged()
    vote = (request.args.get("vote") or "").strip().lower()

    where, params = [], []
    if vote in ("like", "dislike"):
        where.append("v.vote_type = ?")
        params.append(vote)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    def reasons(raw):
        try:
            parsed = json.loads(raw) if raw else []
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except (ValueError, TypeError):
            return [raw] if raw else []

    def item(r):
        return {
            "id": str(r["id"]),
            "crosswordId": str(r["crossword_id"]),
            "crosswordTitle": r["title"] or "",
            "voteType": r["vote_type"] or "",
            "reasons": reasons(r["reasons"]),
            "otherText": r["other_text"] or "",
            "message": r["message"] or "",
            "createdAt": r["created_at"] or "",
        }

    select = ("SELECT v.*, c.title FROM vote_feedback v "
              f"LEFT JOIN crosswords c ON c.id = v.crossword_id{clause} "
              "ORDER BY v.id DESC")

    conn = _conn()
    try:
        if admin_query.has_query(request.args):
            body, err = _queried([item(r) for r in conn.execute(select, params)])
            return err or jsonify(body)

        total = conn.execute(
            f"SELECT COUNT(*) FROM vote_feedback v{clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            select + " LIMIT ? OFFSET ?",
            params + [count, (page - 1) * count],
        ).fetchall()
        return jsonify({
            "totalCount": total,
            "page": page,
            "pageSize": count,
            "items": [item(r) for r in rows],
        })
    finally:
        conn.close()


# =====================================================
# 🔹 SETTINGS (global switches)
# =====================================================

@admin_bp.route("/settings", methods=["GET"])
@admin_guard(*READ_ROLES)
def get_admin_settings():
    return jsonify(get_settings())


@admin_bp.route("/settings", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def set_admin_settings():
    denied = _require_write()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    changes = {k: bool(v) for k, v in body.items() if k in SETTING_KEYS}
    if not changes:
        return jsonify(error=f"Expected at least one of: {', '.join(SETTING_KEYS)}"), 400

    conn = _conn()
    try:
        for key, value in changes.items():
            conn.execute(
                "INSERT INTO admin_settings (key, value, updated_by, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "  updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP",
                (key, "1" if value else "0", getattr(g, "admin_actor", "unknown")),
            )
            _audit(conn, "setting.update", "setting", key, "on" if value else "off")
        conn.commit()
    finally:
        conn.close()

    return jsonify(get_settings())


# =====================================================
# 🔹 MONITORING (audit log)
# =====================================================

@admin_bp.route("/audit", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_audit():
    page, count = _paged()
    action = (request.args.get("action") or "").strip()

    where, params = [], []
    if action:
        where.append("action LIKE ?")
        params.append(f"{action}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    def item(r):
        return {
            "id": str(r["id"]),
            "actor": r["actor"] or "",
            "action": r["action"] or "",
            "entityType": r["entity_type"] or "",
            "entityId": r["entity_id"] or "",
            "detail": r["detail"] or "",
            "createdAt": r["created_at"] or "",
        }

    conn = _conn()
    try:
        if admin_query.has_query(request.args):
            rows = conn.execute(
                f"SELECT * FROM admin_audit_log{clause} ORDER BY id DESC", params)
            body, err = _queried([item(r) for r in rows])
            return err or jsonify(body)

        total = conn.execute(
            f"SELECT COUNT(*) FROM admin_audit_log{clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM admin_audit_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [count, (page - 1) * count],
        ).fetchall()
        return jsonify({
            "totalCount": total,
            "page": page,
            "pageSize": count,
            "items": [item(r) for r in rows],
        })
    finally:
        conn.close()


@admin_bp.route("/health", methods=["GET"])
@admin_guard(*READ_ROLES)
def admin_health():
    conn = _conn()
    try:
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    finally:
        conn.close()
    return jsonify({
        "ok": db_ok,
        "serverTime": datetime.now(AM_TZ).isoformat(),
        "actor": getattr(g, "admin_actor", ""),
        "canWrite": getattr(g, "admin_can_write", False),
    })


# =====================================================
# 🔹 SYSTEM LOGS (Error + Critical)
# =====================================================

LOG_SCAN_MAX_LINES = int(os.getenv("ADMIN_LOG_SCAN_MAX_LINES", "20000"))

_LOG_KNOWN_FIELDS = {"Timestamp", "Severity", "Service", "Logger", "Message",
                     "Operation", "RequestId", "Exception", "Traceback"}


def _norm_level(s):
    s = (s or "").strip().lower()
    return {"err": "error", "crit": "critical", "fatal": "critical"}.get(s, s)


def _parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def _parse_query_date(value, end_of_day=False):
    """'2026-09-23' կամ ISO datetime → aware datetime (AM_TZ)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=AM_TZ)
    if end_of_day and len(value.strip()) == 10:   # միայն ամսաթիվ → ամբողջ օրը
        dt += timedelta(days=1)
    return dt


def _tail_lines(path, max_lines, block=64 * 1024):
    """Ֆայլի ՎԵՐՋԻՑ կարդում է մինչև max_lines տող՝ առանց ամբողջ ֆայլը հիշողություն բեռնելու։"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf, lines = b"", []
            while pos > 0 and len(lines) <= max_lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                lines = buf.split(b"\n")
            if pos > 0:
                lines = lines[1:]          # առաջին տողը կիսատ է
            return [l.decode("utf-8", "replace") for l in lines[-max_lines:] if l.strip()]
    except FileNotFoundError:
        return []


def _error_log_files(include_rotated):
    base = os.path.join(LOG_DIR, "error.log")
    files = [base]
    if include_rotated:
        i = 1
        while os.path.exists(f"{base}.{i}"):
            files.append(f"{base}.{i}")
            i += 1
    return files


def _log_item(line, obj, level):
    return {
        "id": hashlib.sha1(line.encode("utf-8")).hexdigest()[:16],
        "timestamp": obj.get("Timestamp", ""),
        "level": "Critical" if level == "critical" else "Error",
        "service": obj.get("Service", ""),
        "logger": obj.get("Logger", ""),
        "message": obj.get("Message", ""),
        "operation": obj.get("Operation", ""),
        "requestId": obj.get("RequestId", ""),
        "exception": obj.get("Exception", ""),
        "traceback": obj.get("Traceback", ""),
        "context": {k: v for k, v in obj.items() if k not in _LOG_KNOWN_FIELDS},
    }


def _read_error_logs(include_rotated=False):
    """Վերադարձնում է Error/Critical գրառումները՝ նորերը առաջինը։"""
    records, remaining = [], LOG_SCAN_MAX_LINES
    for path in _error_log_files(include_rotated):
        if remaining <= 0:
            break
        lines = _tail_lines(path, remaining)
        remaining -= len(lines)
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = {"Severity": "ERROR", "Message": line}
            if not isinstance(obj, dict):
                continue
            level = _norm_level(obj.get("Severity"))
            if level not in ("error", "critical"):
                continue
            records.append(_log_item(line, obj, level))
    return records


@admin_bp.route("/logs", methods=["GET"])
@admin_guard(*READ_ROLES)
def list_error_logs():
    """
    GET /api/admin/logs
      ?level=error|critical|all   (default: all → Error + Critical)
      &q=տեքստ                    (message / exception / operation-ում որոնում)
      &operation=GenerateAiCrossword
      &requestId=abc123
      &from=2026-09-01&to=2026-09-23
      &rotated=1                  (նաև error.log.1, .2 ...)
      &traceback=0                (traceback-ը չուղարկել՝ թեթև ցուցակի համար)
      &page=1&count=50
    """
    page, count = _paged()
    level = _norm_level(request.args.get("level") or "all")
    q = (request.args.get("q") or "").strip().lower()
    operation = (request.args.get("operation") or "").strip().lower()
    request_id = (request.args.get("requestId") or "").strip()
    dt_from = _parse_query_date(request.args.get("from"))
    dt_to = _parse_query_date(request.args.get("to"), end_of_day=True)
    include_rotated = request.args.get("rotated") == "1"
    with_tb = request.args.get("traceback", "1") != "0"

    items = []
    for it in _read_error_logs(include_rotated):
        lvl = it["level"].lower()
        if level in ("error", "critical") and lvl != level:
            continue
        if operation and (it["operation"] or "").lower() != operation:
            continue
        if request_id and it["requestId"] != request_id:
            continue
        if q and q not in " ".join(
            str(it[k] or "") for k in ("message", "exception", "operation")
        ).lower():
            continue
        if dt_from or dt_to:
            ts = _parse_ts(it["timestamp"])
            if ts is None or (dt_from and ts < dt_from) or (dt_to and ts >= dt_to):
                continue
        if not with_tb:
            it = {**it, "traceback": ""}
        items.append(it)

    if admin_query.has_query(request.args):
        body, err = _queried(items)
        return err or jsonify(body)

    start = (page - 1) * count
    return jsonify({
        "totalCount": len(items),
        "page": page,
        "pageSize": count,
        "items": items[start:start + count],
    })


@admin_bp.route("/logs/summary", methods=["GET"])
@admin_guard(*READ_ROLES)
def error_logs_summary():
    """Dashboard-ի badge/քարտերի համար՝ քանակներ 24 ժամում և 7 օրում։"""
    from collections import Counter

    now = datetime.now(AM_TZ)
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
    c24 = {"error": 0, "critical": 0}
    c7 = {"error": 0, "critical": 0}
    top_ops = Counter()
    last_critical = None

    for it in _read_error_logs(include_rotated=True):
        ts = _parse_ts(it["timestamp"])
        if ts is None:
            continue
        lvl = it["level"].lower()
        if lvl == "critical" and last_critical is None:
            last_critical = it   # առաջինը ամենանորն է
        if ts >= week_ago:
            c7[lvl] += 1
        if ts >= day_ago:
            c24[lvl] += 1
            top_ops[it["operation"] or "—"] += 1

    return jsonify({
        "last24h": {"errors": c24["error"], "criticals": c24["critical"]},
        "last7d": {"errors": c7["error"], "criticals": c7["critical"]},
        "topOperations24h": [{"operation": k, "count": v} for k, v in top_ops.most_common(10)],
        "lastCritical": None if not last_critical else {
            "timestamp": last_critical["timestamp"],
            "message": last_critical["message"],
            "operation": last_critical["operation"],
        },
    })


@admin_bp.route("/logs/clear", methods=["POST"])
@admin_guard(*WRITE_ROLES)
def clear_error_logs():
    denied = _require_write()
    if denied:
        return denied

    path = os.path.join(LOG_DIR, "error.log")
    try:
        open(path, "w", encoding="utf-8").close()   # դատարկում ենք, չենք ջնջում
    except OSError as e:
        return jsonify(error=f"Cannot clear log: {e}"), 500

    conn = _conn()
    try:
        _audit(conn, "logs.clear", "log", "error.log")
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True)
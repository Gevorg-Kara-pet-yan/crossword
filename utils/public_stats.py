"""
Public Stats API — dshh.am կայքի ստատիստիկայի էջի համար։

GET /api/public/stats            → վերջին 30 օրվա ժամանակային տվյալներով
GET /api/public/stats?days=7     → 1..365

Առանց մուտքի (public)։ Վերադարձնում է ՄԻԱՅՆ ընդհանրացված թվեր.
մասնակիցների անուններ, դպրոցներ, մեկնաբանությունների տեքստեր չկան։
Հաշվարկվում են միայն հրապարակված խաչբառերը (ոչ սևագիր, ոչ թաքցված)։
"""

import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from utils.admin_api import get_settings

AM_TZ = timezone(timedelta(hours=4))

public_stats_bp = Blueprint("public_stats", __name__, url_prefix="/api/public")

CACHE_SECONDS = int(os.getenv("PUBLIC_STATS_CACHE_SECONDS", "300"))  # 5 րոպե
ALLOWED_ORIGINS = os.getenv("PUBLIC_STATS_ALLOWED_ORIGINS", "*")       # օր.՝ https://dshh.am

_cache = {}  # days → (timestamp, payload)

# Հրապարակված խաչբառի պայմանը
_PUBLISHED = "COALESCE(c.is_draft, 0) = 0 AND COALESCE(c.hidden, 0) = 0"


def _conn():
    conn = sqlite3.connect(current_app.config["CROSSWORD_DB"])
    conn.row_factory = sqlite3.Row
    return conn


def _parse_list(raw):
    """'["Մայրենի","Մաթեմատիկա"]' կամ 'Մայրենի, Մաթեմատիկա' → ցանկ։"""
    if not raw or not str(raw).strip():
        return []
    try:
        parsed = json.loads(raw)
        items = parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        items = [raw]
    out = []
    for it in items:
        for part in str(it).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _question_count(data_raw):
    try:
        placed = json.loads(data_raw or "{}").get("placed", [])
    except (ValueError, TypeError, AttributeError):
        return 0
    return sum(1 for p in placed
               if isinstance(p, dict) and p.get("question") and p.get("question") != "Բանալի բառ")


def _build(days):
    conn = _conn()
    try:
        def one(sql, params=()):
            row = conn.execute(sql, params).fetchone()
            return (row[0] or 0) if row else 0

        # ── Ընդհանուր ──
        total_cw = one(f"SELECT COUNT(*) FROM crosswords c WHERE {_PUBLISHED}")
        ai_cw = one(f"SELECT COUNT(*) FROM crosswords c WHERE {_PUBLISHED} "
                    "AND COALESCE(c.is_ai_generated, 0) = 1")
        keyword_cw = one(f"SELECT COUNT(*) FROM crosswords c WHERE {_PUBLISHED} "
                         "AND TRIM(COALESCE(c.keyword, '')) <> ''")
        creators = one(f"SELECT COUNT(DISTINCT c.user_id) FROM crosswords c WHERE {_PUBLISHED} "
                       "AND c.user_id IS NOT NULL AND TRIM(c.user_id) <> ''")

        total_q = 0
        subj_counter, grade_counter = Counter(), Counter()
        for r in conn.execute(f"SELECT c.data, c.subject, c.grade FROM crosswords c WHERE {_PUBLISHED}"):
            total_q += _question_count(r["data"])
            for s in _parse_list(r["subject"]):
                subj_counter[s] += 1
            for gr in _parse_list(r["grade"]):
                grade_counter[gr] += 1

        # ── Մասնակցություն ──
        participants = one("SELECT COUNT(*) FROM participants")
        total_results = one("SELECT COUNT(*) FROM results")
        completed = one("SELECT COUNT(*) FROM results WHERE is_completed = 1")
        avg_score = conn.execute(
            "SELECT AVG(1.0 * score / total_words) FROM results "
            "WHERE is_completed = 1 AND total_words > 0"
        ).fetchone()[0]
        avg_duration = conn.execute(
            "SELECT AVG(duration) FROM results WHERE is_completed = 1 AND duration > 0"
        ).fetchone()[0]

        # ── Ակտիվություն ──
        s = conn.execute("""
            SELECT COALESCE(SUM(view_count), 0)     AS views,
                   COALESCE(SUM(like_count), 0)     AS likes,
                   COALESCE(SUM(dislike_count), 0)  AS dislikes,
                   COALESCE(SUM(download_count), 0) AS downloads,
                   COALESCE(SUM(print_count), 0)    AS prints,
                   COALESCE(SUM(share_count), 0)    AS shares
            FROM crossword_stats
        """).fetchone()
        comments = one("SELECT COUNT(*) FROM comments")

        # ── Ժամանակային ──
        since = (datetime.now(AM_TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        cw_period = one(f"SELECT COUNT(*) FROM crosswords c WHERE {_PUBLISHED} "
                        "AND c.created_at >= ?", (since,))
        p_period = one("SELECT COUNT(*) FROM participants WHERE start_time >= ?", (since,))
        cw_per_day = [
            {"date": r["d"], "count": r["n"]}
            for r in conn.execute(
                f"SELECT DATE(c.created_at) AS d, COUNT(*) AS n FROM crosswords c "
                f"WHERE {_PUBLISHED} AND c.created_at >= ? GROUP BY d ORDER BY d", (since,))
        ]
        p_per_day = [
            {"date": r["d"], "count": r["n"]}
            for r in conn.execute(
                "SELECT DATE(start_time) AS d, COUNT(*) AS n FROM participants "
                "WHERE start_time >= ? GROUP BY d ORDER BY d", (since,))
        ]

        # ── Ամենապահանջվածները (եթե բոլորը թաքցված չեն) ──
        top = []
        if not get_settings().get("all_crosswords_hidden"):
            base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            for r in conn.execute(f"""
                SELECT c.id, c.title,
                       (SELECT COUNT(*) FROM participants p WHERE p.crossword_id = c.id) AS pc,
                       COALESCE(st.view_count, 0) AS vc
                FROM crosswords c
                LEFT JOIN crossword_stats st ON st.crossword_id = c.id
                WHERE {_PUBLISHED}
                ORDER BY pc DESC, vc DESC
                LIMIT 10
            """):
                item = {"id": str(r["id"]), "title": r["title"] or "",
                        "participantCount": r["pc"], "viewCount": r["vc"]}
                if base:
                    item["url"] = f"{base}/{r['id']}"
                top.append(item)

        return {
            "program": "Խաչբառ",
            "generatedAt": datetime.now(AM_TZ).isoformat(timespec="seconds"),
            "periodDays": days,

            "totalCrosswords": total_cw,
            "aiGeneratedCrosswords": ai_cw,
            "selfCreatedCrosswords": total_cw - ai_cw,
            "keywordCrosswords": keyword_cw,
            "totalQuestions": total_q,
            "totalCreators": creators,

            "totalParticipants": participants,
            "completedResults": completed,
            "completionRate": round(100.0 * completed / total_results, 1) if total_results else 0.0,
            "averageScorePercent": round(100.0 * avg_score, 1) if avg_score else 0.0,
            "averageDurationSeconds": int(avg_duration) if avg_duration else 0,

            "totalViews": s["views"],
            "totalLikes": s["likes"],
            "totalDislikes": s["dislikes"],
            "totalDownloads": s["downloads"],
            "totalPrints": s["prints"],
            "totalShares": s["shares"],
            "totalComments": comments,

            "crosswordsInPeriod": cw_period,
            "participantsInPeriod": p_period,
            "crosswordsPerDay": cw_per_day,
            "participantsPerDay": p_per_day,
            "bySubject": [{"subject": k, "count": v} for k, v in subj_counter.most_common()],
            "byGrade": [{"grade": k, "count": v} for k, v in grade_counter.most_common()],
            "topCrosswords": top,
        }
    finally:
        conn.close()


@public_stats_bp.route("/stats", methods=["GET"])
def public_stats():
    days = request.args.get("days", 30, type=int) or 30
    days = max(1, min(days, 365))

    cached = _cache.get(days)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        payload = cached[1]
    else:
        try:
            payload = _build(days)
        except sqlite3.Error as e:
            current_app.logger.error("public_stats_failed: %s", e, exc_info=True)
            return jsonify(error="Ստատիստիկան ժամանակավորապես հասանելի չէ"), 503
        _cache[days] = (time.time(), payload)

    resp = jsonify(payload)
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGINS
    resp.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return resp
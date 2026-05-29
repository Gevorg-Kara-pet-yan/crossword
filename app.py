import io
import os
import sqlite3
import json
import asyncio
import aiosqlite
from functools import wraps
import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import time
from utils.crossword_builder import generate_crossword_grid
from dotenv import load_dotenv
from ai_provider import get_provider
from auth_decorators import require_auth
from flask import g
from flask import Blueprint


crossword_bp = Blueprint('crossword', __name__, url_prefix='/crossword')


load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("app.secret_key", "dev_fallback_key")
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, 'utils', 'crossword.db')

UPLOAD_FOLDER = os.path.join('static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

API_URL = os.getenv("API_URL", "http://localhost:5000/api/ask")
X_API_KEY = os.getenv("X_API_KEY", "")

import logging
logging.basicConfig(level=logging.DEBUG)

@app.before_request
def log_request():
    app.logger.debug(f"📥 REQUEST: {request.method} {request.url}")
# =====================================================
# 🔹 CUSTOM AI API CONFIG
# =====================================================

def fetch_crossword_by_id_public(conn, id):
    return conn.execute(
        "SELECT * FROM crosswords WHERE id = ?", (id,)
    ).fetchone()

def ask_raw(message: str) -> str:
    if not X_API_KEY:
        raise RuntimeError("❌ API key չկա. Խնդրում եմ `.env` ֆայլում ավելացրեք X_API_KEY")

    payload = {"message": message}
    headers = {
        "X-API-Key": X_API_KEY,
        "Content-Type": "application/json; charset=utf-8"
    }

    try:
        response = requests.post(API_URL, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=15)
        response.encoding = "utf-8"
    except requests.RequestException as e:
        raise RuntimeError(f"❌ API-ի հետ կապի խնդիր: {e}")

    print("🟢 AI API RAW RESPONSE:", response.text)
    return response.text


def extract_answer_text(raw: str) -> str:
    """Վերցնում է answer field-ը եթե response-ը {"ok":true,"answer":"..."} ձևաչափով է"""
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
    """DB-ից վերցված subject կամ grade արժեքը վերածում է list-ի։"""
    if not value:
        return []
    try:
        result = json.loads(value)
        if isinstance(result, list):
            return [str(v).strip() for v in result if str(v).strip()]
        return [str(result).strip()] if str(result).strip() else []
    except Exception:
        return [v.strip() for v in value.split(',') if v.strip()]


def normalize_to_json_list(value):
    """subject կամ grade արժեքը վերածում է JSON list string-ի DB-ի մեջ պահելու համար։"""
    if not value:
        return json.dumps([], ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps([str(v).strip() for v in value if str(v).strip()], ensure_ascii=False)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return json.dumps([str(v).strip() for v in parsed if str(v).strip()], ensure_ascii=False)
        return json.dumps([str(parsed).strip()], ensure_ascii=False)
    except Exception:
        pass
    items = [v.strip() for v in str(value).split(',') if v.strip()]
    return json.dumps(items, ensure_ascii=False)




def normalize(text):
    return text.strip().upper()


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
# 🔹 API - CROSSWORDS LIST
# =====================================================

@app.route('/api/users/<user_id>/crosswords', methods=['GET'])
@require_auth()
async def api_crosswords(user_id):

    current_user_id = g.current_user["sub"]

    # user-ը միայն իր crossword-ները կարող է տեսնել
    if str(current_user_id) != str(user_id):
        return jsonify({
            "success": False,
            "error": "Access denied"
        }), 403

    base_url = request.host_url.rstrip("/")

    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            rows = await db.execute_fetchall("""
                SELECT
                    id,
                    title,
                    keyword,
                    is_ai_generated,
                    subject,
                    grade,
                    created_at
                FROM crosswords
                WHERE user_id = ?
                ORDER BY id DESC
            """, (user_id,))

            result = []

            for row in rows:
                cw = dict(row)

                result.append({
                    "id": cw["id"],
                    "title": cw["title"],
                    "keyword": cw["keyword"],
                    "isAiGenerated": bool(cw["is_ai_generated"]),
                    "subject": parse_list_field(cw.get("subject")),
                    "grade": parse_list_field(cw.get("grade")),
                    "createdAt": cw.get("created_at"),
                    "downloadUrl": f"{base_url}/download/{cw['id']}",
                    "externalUrl": f"{base_url}/crossword/{cw['id']}"
                })

            return jsonify(result)

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# =====================================================
# 🔹 API - CROSSWORD STATS
# =====================================================

@app.route('/api/crosswords/<int:id>/stats', methods=['GET'])
async def api_crossword_stats(id):

    try:
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = sqlite3.Row

            # views / downloads / prints / likes պահելու համար
            # պետք է stats table

            row = await db.execute_fetchone("""
                SELECT
                    view_count,
                    like_count,
                    dislike_count,
                    download_count,
                    read_duration_seconds,
                    comment_count,
                    print_count
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
                    "printCount": 0
                })

            stats = dict(row)

            return jsonify({
                "viewCount": stats.get("view_count", 0),
                "likeCount": stats.get("like_count", 0),
                "dislikeCount": stats.get("dislike_count", 0),
                "downloadCount": stats.get("download_count", 0),
                "readDurationSeconds": stats.get("read_duration_seconds", 0),
                "commentCount": stats.get("comment_count", 0),
                "printCount": stats.get("print_count", 0)
            })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def check_and_create_crossword_stats_table():
    conn = sqlite3.connect(DATABASE)

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crossword_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER UNIQUE,

                view_count INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                dislike_count INTEGER DEFAULT 0,
                download_count INTEGER DEFAULT 0,
                read_duration_seconds INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                print_count INTEGER DEFAULT 0,

                FOREIGN KEY (crossword_id)
                REFERENCES crosswords(id)
            )
        """)

        conn.commit()
        print("✅ crossword_stats table ready")

    except Exception as e:
        print("❌ crossword_stats error:", e)

    finally:
        conn.close()


def check_and_add_created_at_column():
    conn = sqlite3.connect(DATABASE)

    try:
        conn.execute("""
            ALTER TABLE crosswords
            ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """)

        conn.commit()

    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(e)

    finally:
        conn.close()



# =====================================================
# 🔹 MAIN ROUTE
# =====================================================

@app.route('/generate_ai_crossword', methods=['POST'])
@require_auth("Teacher", "OtherLecturer")
async def generate_ai_crossword():
    ai_model = request.form.get("ai_model", "claude")
    provider = get_provider(ai_model)
    topic = request.form.get('topic', 'Ընդհանուր')
    creator_name = request.form.get('creator_name', 'AI Assistant')
    keyword_mode = request.form.get("keyword_mode")
    description = request.form.get("description", "").strip()
    question_count = int(request.form.get("question_count", 10))
    question_count = max(2, min(40, question_count))
    manual_keyword = normalize(request.form.get("keyword", "").strip())

    try:
        print("AI MODEL:", ai_model)
        print("PROVIDER:", provider)
        print("HAS API KEY:", bool(X_API_KEY))
        # ===============================
        # 🟢 WITHOUT KEYWORD
        # ===============================
        if keyword_mode != "with":
            formatted_questions = await provider.generate_questions(topic, question_count, description)
            keyword = ""
            crossword_data = generate_crossword_grid(formatted_questions, keyword)
            if not crossword_data:
                raise ValueError("Grid generation failed")

        # ===============================
        # 🔵 WITH KEYWORD
        # ===============================
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
                    if 4 <= len(q["a"]) <= 7
                       and normalize(q["a"]) != normalized_topic
                ]))
                if not candidates:
                    raise ValueError("Հարմար բանալի բառ չգտնվեց")
                success = False
                crossword_data = None
                keyword = ""
                for candidate in sorted(candidates, key=len, reverse=True):
                    filtered_questions = [
                        q for q in formatted_questions
                        if normalize(q["a"]) != candidate
                    ]
                    selected_questions = select_questions_for_keyword(candidate, filtered_questions)
                    if not selected_questions:
                        continue
                    grid = generate_crossword_grid(selected_questions, candidate)
                    if grid:
                        crossword_data = grid
                        keyword = candidate
                        formatted_questions = selected_questions
                        success = True
                        break
                if not success:
                    raise ValueError("Չհաջողվեց կառուցել բանալի բառով խաչբառ")

        # ===============================
        # 💾 SAVE TO DB
        # ===============================
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
            1
        )

        flash(
            f"✨ Խաչբառը գեներացվել է արհեստական բանականության կողմից։\n"
            f"⚠️ Հնարավոր են սխալներ — խնդրում ենք ստուգել հարցերի ճշտությունը և տառասխալների առկայությունը։\n"
            f"🔑 Բանալի բառ՝ {keyword if keyword else 'Չկա'}",
            "warning"
        )
        return redirect(url_for('edit_crossword', id=crossword_id))

    except Exception as e:
        print("🛑 Սխալ:", str(e))
        flash("Խաչբառի գեներացման ժամանակ առաջացավ սխալ։", "danger")
        return redirect(url_for('choose_keyword_mode'))


@app.route('/api/ai-questions-for-edit', methods=['POST'])
async def ai_questions_for_edit():
    data = request.get_json()

    topic   = data.get('topic', '').strip()
    subject = data.get('subject', '')
    grade   = data.get('grade', '')
    count   = int(data.get('count', 3))
    existing = data.get('existing', [])

    if not topic:
        return jsonify({"error": "Թեմա չի նշված"}), 400

    full_topic = topic
    if subject:
        full_topic += f" ({subject})"
    if grade:
        full_topic += f" {grade}-րդ դասարան"

    try:
        existing_answers = set(
            q.get("answer", "").strip().upper() for q in existing
        )

        provider = get_provider("claude")  # կամ dynamic հետո
        pairs = await provider.generate_questions(full_topic, count)

        filtered_pairs = [
            p for p in pairs
            if p["a"].strip().upper() not in existing_answers
        ]

        return jsonify({
            "pairs": [
                {"question": p["q"], "answer": p["a"]}
                for p in filtered_pairs
            ]
        })

    except Exception as e:
        print("❌ ai_questions_for_edit սխալ:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/ai-edit-crossword', methods=['POST'])
def ai_edit_crossword():
    data = request.get_json()

    qa_pairs = data.get("qa_pairs", [])
    subject  = data.get("subject", "")
    grade    = data.get("grade", "")
    topic    = data.get("topic", "")

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

    Վերադարձրու JSON այս ֆորմատով՝

    [
      {{
        "question": "...",
        "answer": "...",
        "changes": "ինչ տառասխալ է ուղղվել կամ 'չկա'"
      }}
    ]
    """

    provider = get_provider("claude")
    ai_response = provider.call_for_json(prompt)
    return jsonify({"pairs": ai_response})


@app.route('/ai_setup')
def ai_setup():
    return render_template('ai_setup.html')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def check_and_add_total_words_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE results ADD COLUMN total_words INTEGER;")
        conn.commit()
        print("✅ total_words սյունակը ավելացվեց results աղյուսակում")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            pass
        else:
            print("❌ Սխալ total_words սյունակ ավելացնելիս:", e)
    finally:
        conn.close()


def check_and_create_participants_table():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""
                     CREATE TABLE IF NOT EXISTS participants
                     (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         crossword_id INTEGER NOT NULL,
                         name TEXT NOT NULL,
                         surname TEXT NOT NULL,
                         gender TEXT NOT NULL,
                         school TEXT,
                         grade TEXT,
                         start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                     );
                     """)

        conn.execute("""
                     CREATE TABLE IF NOT EXISTS results
                     (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         participant_id INTEGER NOT NULL,
                         crossword_id INTEGER NOT NULL,
                         score INTEGER,
                         duration INTEGER,
                         is_completed INTEGER DEFAULT 0,
                         end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         FOREIGN KEY (participant_id) REFERENCES participants (id),
                         FOREIGN KEY (crossword_id) REFERENCES crosswords (id)
                     );
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
        print("hidden սյունակը ավելացվեց crosswords աղյուսակում")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            pass
        else:
            print(f"Սխալ hidden սյունակը ավելացնելիս: {e}")
    finally:
        conn.close()


def check_and_add_creator_name_column():
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN creator_name TEXT;")
        conn.commit()
        print("creator_name սյունակը հաջողությամբ ավելացվեց crosswords աղյուսակում։")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            pass
        else:
            print(f"Զգուշացում: Չկարողացա ստուգել/ավելացնել creator_name սյունակը: {e}")
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
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN keyword_material_type TEXT;")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print("Սխալ keyword_material_type:", e)
    try:
        conn.execute("ALTER TABLE crosswords ADD COLUMN keyword_material_value TEXT;")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print("Սխալ keyword_material_value:", e)
    finally:
        conn.close()


@app.route('/api/save_participant', methods=['POST'])
async def save_participant():
    data = request.get_json()

    if not data or not data.get('name') or not data.get('surname'):
        return jsonify({'status': 'error', 'message': 'Անունը և ազգանունը պարտադիր են'}), 400

    # crossword_id պետք է frontend-ից գա
    if not data.get('crossword_id'):
        return jsonify({'status': 'error', 'message': 'crossword_id բացակայում է'}), 400

    try:
        participant_id = await asyncio.to_thread(
            db_operation_wrapper,
            insert_participant_db,
            data
        )
        return jsonify({'status': 'success', 'id': participant_id}), 200

    except Exception as e:
        print(f"Error saving participant: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


async def save_file_async(file, save_path):
    return await asyncio.to_thread(file.save, save_path)


async def delete_file_async(file_path):
    return await asyncio.to_thread(os.remove, file_path)


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


def insert_new_crossword_db(conn, user_id, title, keyword, bg_path, crossword_data, description, creator_name, subject, grade, is_ai=0):
    subject_json = normalize_to_json_list(subject)
    grade_json   = normalize_to_json_list(grade)

    conn.execute(
        """
        INSERT INTO crosswords (user_id, title, keyword, background, data, description, creator_name, subject, grade, is_ai_generated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, title, keyword, bg_path,
         json.dumps(crossword_data, ensure_ascii=False),
         description or "Նկարագրություն չկա",
         creator_name,
         subject_json,
         grade_json,
         is_ai)
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]



def update_crossword_db(conn, id, title, keyword, bg_path, crossword_data,
                         description, creator_name, subjects, grades, user_id):
    subject_json = normalize_to_json_list(subjects)
    grade_json   = normalize_to_json_list(grades)

    conn.execute("""
        UPDATE crosswords
        SET title=?, keyword=?, background=?, data=?,
            description=?, creator_name=?, subject=?, grade=?
        WHERE id=? AND user_id=?
    """, (
        title, keyword, bg_path,
        json.dumps(crossword_data, ensure_ascii=False),
        description or "Նկարագրություն չկա",
        creator_name, subject_json, grade_json,
        id, user_id
    ))

def insert_participant_db(conn, data):
    cursor = conn.execute(
        """INSERT INTO participants (crossword_id, name, surname, gender, school, grade)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            data.get('crossword_id'),
            data.get('name'),
            data.get('surname'),
            data.get('gender'),
            data.get('school', ''),
            data.get('class', '')
        )
    )
    return cursor.lastrowid


def fetch_crossword_by_id(conn, id, user_id):
    return conn.execute(
        "SELECT * FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)
    ).fetchone()

def fetch_crosswords_list(conn, user_id, page, per_page, query):
    if not query:
        total = conn.execute(
            "SELECT COUNT(*) FROM crosswords WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0]

        total_pages = (total + per_page - 1) // per_page
        offset = (page - 1) * per_page

        rows = conn.execute("""
                            SELECT id, title, keyword, background, creator_name, is_ai_generated
                            FROM crosswords
                            WHERE user_id = ?
                            ORDER BY id DESC LIMIT ?
                            OFFSET ?
                            """, (user_id, per_page, offset)).fetchall()
        crosswords = [dict(r) for r in rows]
        return crosswords, total_pages, total
    else:
        rows = conn.execute("""
                            SELECT id, title, keyword, background, creator_name, is_ai_generated
                            FROM crosswords
                            WHERE user_id = ?
                            ORDER BY id DESC
                            """, (user_id,)).fetchall()

        all_cw = [dict(r) for r in rows]

        q_cf = query.casefold()
        matched = []
        for cw in all_cw:
            title        = (cw.get('title') or '').casefold()
            keyword      = (cw.get('keyword') or '').casefold()
            creator_name = (cw.get('creator_name') or '').casefold()

            if q_cf in title or q_cf in keyword or q_cf in creator_name:
                matched.append(cw)

        total       = len(matched)
        total_pages = (total + per_page - 1) // per_page
        offset      = (page - 1) * per_page
        crosswords  = matched[offset: offset + per_page]

        return crosswords, total_pages, total


def delete_crossword_from_db(conn, id, user_id):
    cw = conn.execute("SELECT id FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)).fetchone()
    if cw:
        conn.execute("DELETE FROM crosswords WHERE id = ?", (id,))
        return True
    return False


def get_single_field_db(conn, id, user_id, field):
    result = conn.execute(f"SELECT {field} FROM crosswords WHERE id = ? AND user_id = ?", (id, user_id)).fetchone()
    return result[field] if result else None


@app.route('/save_result/<int:id>', methods=['POST'])
async def save_result(id):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Տվյալներ չեն ստացվել"}), 400

    name        = data.get('name')
    surname     = data.get('surname')
    gender      = data.get('gender')
    school      = data.get('school', '')
    grade       = data.get('class', '')
    score       = int(data.get('correct_count') or 0)
    total_words = int(data.get('total_words') or 0)
    duration    = int(data.get('time_taken') or 0)

    try:
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                """INSERT INTO participants (crossword_id, name, surname, gender, school, grade)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (id, name, surname, gender, school, grade)
            )
            participant_id = cursor.lastrowid

            await db.execute(
                """INSERT INTO results 
                   (participant_id, crossword_id, score, total_words, duration, is_completed)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (participant_id, id, score, total_words, duration, 1)
            )

            await db.commit()
            return jsonify({"success": True, "message": "Արդյունքը պահպանվեց ✅"}), 200

    except Exception as e:
        print(f"Բազայի սխալ save_result-ում: {e}")
        return jsonify({"success": False, "message": "Սխալ պահպանման ժամանակ"}), 500


@app.route('/upload-bg', methods=['POST'])
async def upload_bg():
    if 'bg_file' not in request.files:
        return jsonify({'success': False, 'error': 'Ֆայլ չի ընտրվել'})

    file = request.files['bg_file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Ֆայլի անունը բացակայում է'})

    if file and allowed_file(file.filename):
        filename  = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        await save_file_async(file, save_path)
        return jsonify({
            'success': True,
            'filename': filename,
            'url': f'/static/uploads/{filename}'
        })
    else:
        return jsonify({'success': False, 'error': 'Ֆայլի ֆորմատը թույլատրված չէ'})


@app.route('/delete-bg', methods=['POST'])
async def delete_bg():
    data     = request.get_json()
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
    fields  = ['data', 'title', 'keyword', 'description', 'grade', 'subject']
    db_data = {}

    for field in fields:
        val = await asyncio.to_thread(
            db_operation_wrapper,
            get_single_field_db,
            crossword_id,
            user_id,
            field
        )
        db_data[field] = val

    if not db_data['data']:
        return "Խաչբառը չի գտնվել։", 404

    try:
        data_json = json.loads(db_data['data']) if isinstance(db_data['data'], str) else db_data['data']
    except:
        return "Տվյալների սխալ ձևաչափ։", 500

    title   = db_data['title'] or "Անանուն Խաչբառ"
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
            num       = item.get('number', '?')
            q_text    = item.get('question', '---')
            answer    = item.get('word', '---')
            direction = "Հորիզոնական" if item.get('dir') == 'h' else "Ուղղահայաց"
            content  += f"{num}. [{direction}] ՀԱՐՑ: {q_text}\n"
            content  += f"   ՊԱՏԱՍԽԱՆ: {answer}\n"
            content  += "-" * 35 + "\n"
    else:
        content += "Հարցեր չեն գտնվել։"

    output = io.BytesIO()
    output.write(content.encode('utf-8'))
    output.seek(0)

    safe_filename = "".join([c for c in title if c.isalnum() or c in (' ', '_')]).strip()

    return send_file(
        output,
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"{safe_filename}.txt"
    )


@app.route('/choose_keyword_mode', methods=['GET', 'POST'])
def choose_keyword_mode():
    if request.method == 'POST':
        use_keyword = request.form.get('use_keyword')
        return redirect(url_for('input_questions', use_keyword=use_keyword))
    return render_template('choose_keyword_mode.html')


@app.route('/input', methods=['GET', 'POST'])
@require_auth("Teacher", "OtherLecturer")
async def input_questions():
    use_keyword = request.args.get('use_keyword', 'yes')
    bg_folder   = os.path.join(app.static_folder, "backgrounds")

    bg_images = await asyncio.to_thread(
        lambda: [f for f in os.listdir(bg_folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    )

    if request.method == 'POST':
        grades_list = request.form.getlist('grades')
        grade_to_save = ", ".join(grades_list) if grades_list else "Նշված չէ"

        selected_subjects_raw = request.form.get('selected_subjects', '').strip()
        if not selected_subjects_raw:
            selected_subjects_raw = request.form.get('subject', '').strip()

        subjects_list = request.form.getlist('selected_subjects')



        font_size    = request.form.get('font_size', '20px')
        font_family  = request.form.get('font_family', 'Segoe UI')
        keyword      = request.form.get('keyword', '').strip().upper()
        title        = request.form.get('title', 'Անանուն Խաչբառ').strip()
        description  = request.form.get('description', '').strip()
        keyword_info = request.form.get('keyword_info', '').strip()
        creator_name = request.form.get('creator_name', 'Անհայտ').strip()

        hint_type  = request.form.get('hint_type')
        hint_text  = request.form.get('global_hint_text', '').strip()
        hint_file  = request.files.get('global_hint_media')
        hint_value = None

        if hint_type in ['audio', 'video', 'image'] and hint_file and hint_file.filename:
            ext       = hint_file.filename.rsplit('.', 1)[-1].lower() if '.' in hint_file.filename else 'bin'
            filename  = f"hint_{int(time.time())}.{ext}"
            hint_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await save_file_async(hint_file, hint_path)
            hint_value = f"uploads/{filename}"
        elif hint_type == 'text':
            hint_value = hint_text

        keyword_material_type  = request.form.get('keyword_material_type', '').strip()
        keyword_material_value = ''
        keyword_material_file  = request.files.get('keyword_material_file')

        if keyword_material_type == 'text':
            keyword_material_value = request.form.get('keyword_material_text', '').strip()
        elif keyword_material_type in ('image', 'animation') and keyword_material_file and keyword_material_file.filename:
            ext       = keyword_material_file.filename.rsplit('.', 1)[-1].lower() if '.' in keyword_material_file.filename else 'bin'
            filename  = f"kwmat_{int(time.time())}.{ext}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await save_file_async(keyword_material_file, save_path)
            keyword_material_value = f"uploads/{filename}"
        elif keyword_material_type == 'link':
            keyword_material_value = request.form.get('keyword_material_link', '').strip()

        bg_width     = request.form.get('bg_width', '400px')
        bg_height    = request.form.get('bg_height', '200px')
        bg_size_mode = request.form.get('bg_size_mode', 'fixed')

        file      = request.files.get('bg_upload')
        bg_choice = request.form.get('bg_choice', '')
        bg_path   = ''

        if file and file.filename:
            filename  = secure_filename(file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(file, save_path)
            bg_path = f'uploads/{filename}'
        elif bg_choice:
            bg_path = f'backgrounds/{bg_choice}'

        questions = []
        for key in request.form:
            if key.startswith('question_'):
                index = key.split('_')[1]
                q = request.form.get(f'question_{index}')
                a = request.form.get(f'answer_{index}')
                if q and a:
                    questions.append({'q': q.strip(), 'a': a.strip().upper()})

        crossword_data = generate_crossword_grid(questions, keyword)
        crossword_data.update({
            'hint_type':              hint_type,
            'hint_value':             hint_value,
            'description':            description,
            'bg_width':               bg_width,
            'bg_height':              bg_height,
            'bg_size_mode':           bg_size_mode,
            'grades':                 grades_list,
            'subjects':               subjects_list,
            'font_size':              font_size,
            'keyword_info':           keyword_info,
            'font_family':            font_family,
            'keyword_material_type':  keyword_material_type,
            'keyword_material_value': keyword_material_value,
        })

        try:
            crossword_id = await asyncio.to_thread(
                db_operation_wrapper,
                insert_new_crossword_db,
                g.current_user["sub"],  # ← 1-ի փոխարեն
                title,
                keyword,
                bg_path,
                crossword_data,
                description,
                creator_name,
                subjects_list,
                grades_list
            )
        except Exception as e:
            print(f"ERROR DURING SAVE: {e}")
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for('input_questions'))

        return redirect(url_for('view_crossword', id=crossword_id))

    return render_template('input_questions.html', use_keyword=use_keyword, bg_images=bg_images)


@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@require_auth("Teacher", "OtherLecturer")
async def edit_crossword(id):
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
        title        = request.form.get('title', '').strip()
        keyword      = request.form.get('keyword', '').strip().upper()
        keyword_hint = request.form.get('keyword_hint', '')
        description  = request.form.get('description', '').strip()
        keyword_info = request.form.get('keyword_info', '').strip()
        creator_name = request.form.get('creator_name', '').strip()

        grades                = request.form.getlist('grades')
        selected_subjects_raw = request.form.get('selected_subjects', '')
        subjects              = [s.strip() for s in selected_subjects_raw.split(',') if s.strip()]

        font_size    = request.form.get('font_size', '20px')
        font_family  = request.form.get('font_family', 'Segoe UI')
        bg_size_mode = request.form.get('bg_size_mode', 'fixed')
        bg_width     = request.form.get('bg_width')
        bg_height    = request.form.get('bg_height')

        bg_file   = request.files.get('bg_upload')
        bg_choice = request.form.get('bg_choice', '')
        bg_path   = cw['background']

        if bg_file and bg_file.filename:
            filename  = secure_filename(bg_file.filename)
            filename  = f"bg_{int(time.time())}_{filename}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(bg_file, save_path)
            bg_path = f'uploads/{filename}'
        elif bg_choice:
            bg_path = f'backgrounds/{bg_choice}'

        questions = []
        for key in request.form:
            if key.startswith('question_'):
                index = key.split('_')[1]
                q = request.form.get(f'question_{index}', '').strip()
                a = request.form.get(f'answer_{index}', '').strip().upper()
                if q and a:
                    questions.append({'q': q, 'a': a})

        crossword_data = generate_crossword_grid(questions, keyword)

        hint_type        = request.form.get('hint_type', '')
        global_hint_text = request.form.get('global_hint_text', '').strip()
        file_hint        = request.files.get('global_hint_media')

        old_data          = json.loads(cw['data'])
        global_hint_media = old_data.get('global_hint_media')
        global_hint       = None

        if hint_type == 'text':
            global_hint       = global_hint_text
            global_hint_media = None
        elif hint_type in ('audio', 'video', 'image'):
            if file_hint and file_hint.filename:
                filename  = secure_filename(file_hint.filename)
                filename  = f"hint_{int(time.time())}_{filename}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
                await save_file_async(file_hint, save_path)
                global_hint_media = f'static/uploads/{filename}'
                print("GLOBAL HINT MEDIA =", global_hint_media)
        else:
            global_hint       = None
            global_hint_media = None

        # ── Բանալի բառի նյութ ──────────────────────────────
        keyword_material_type  = request.form.get('keyword_material_type', '').strip()
        keyword_material_value = old_data.get('keyword_material_value', '')  # հին արժեքը default
        keyword_material_file  = request.files.get('keyword_material_file')

        if keyword_material_type == 'text':
            keyword_material_value = request.form.get('keyword_material_text', '').strip()
        elif keyword_material_type in ('image', 'animation') and keyword_material_file and keyword_material_file.filename:
            ext       = keyword_material_file.filename.rsplit('.', 1)[-1].lower() if '.' in keyword_material_file.filename else 'bin'
            filename  = f"kwmat_{int(time.time())}.{ext}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            await asyncio.to_thread(os.makedirs, app.config['UPLOAD_FOLDER'], exist_ok=True)
            await save_file_async(keyword_material_file, save_path)
            keyword_material_value = f"uploads/{filename}"
        elif keyword_material_type == 'link':
            keyword_material_value = request.form.get('keyword_material_link', '').strip()
        elif keyword_material_type == '':
            keyword_material_value = ''
        # ────────────────────────────────────────────────────

        crossword_data.update({
            'hint_type':              hint_type,
            'global_hint':            global_hint,
            'global_hint_media':      global_hint_media,
            'keyword_hint':           keyword_hint,
            'description':            description,
            'keyword_info':           keyword_info,
            'grades':                 grades,
            'subjects':               subjects,
            'font_size':              font_size,
            'font_family':            font_family,
            'bg_size_mode':           bg_size_mode,
            'bg_width':               bg_width,
            'bg_height':              bg_height,
            'keyword_material_type':  keyword_material_type,
            'keyword_material_value': keyword_material_value,
        })

        try:
            await asyncio.to_thread(
                db_operation_wrapper,
                update_crossword_db,
                id,
                title,
                keyword,
                bg_path,
                crossword_data,
                description,
                creator_name,
                subjects,
                grades,
                g.current_user["sub"]
            )
            flash("Խաչբառը հաջողությամբ թարմացվեց։")
            return redirect(url_for('view_crossword', id=id))
        except Exception as e:
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for('edit_crossword', id=id))

    crossword_json = json.loads(cw['data'])

    if 'hint_value' in crossword_json:
        h_type = crossword_json.get('hint_type')
        h_val  = crossword_json.get('hint_value')
        if h_type == 'text':
            crossword_json['global_hint'] = h_val
        elif h_type in ['audio', 'video', 'image']:
            if h_val and isinstance(h_val, str) and h_val.startswith('hints/'):
                h_val = h_val.replace('hints/', '')
            crossword_json['global_hint_media'] = h_val

    defaults = {
        'grades':           [],
        'subjects':         [],
        'keyword_info':     '',
        'font_size':        '20px',
        'font_family':      'Segoe UI',
        'bg_width':         '400px',
        'bg_height':        '200px',
        'bg_size_mode':     'fixed',
        'hint_type':        '',
        'global_hint':      '',
        'global_hint_media': None,
        'keyword_material_type': '',
        'keyword_material_value': ''
    }

    for key, value in defaults.items():
        crossword_json.setdefault(key, value)

    return render_template('edit_crossword.html',
                           crossword=crossword_json,
                           cw=cw,
                           bg_images=bg_images,
                           creator_name=cw.get('creator_name', ''))


@app.route('/my-crosswords')
@require_auth()
async def my_crosswords():
    query = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 6
    user_id = g.current_user["sub"]

    try:
        crosswords, total_pages, total_items = await asyncio.to_thread(
            db_operation_wrapper,
            fetch_crosswords_list,
            user_id,
            page,
            per_page,
            query
        )
    except Exception as e:
        flash(f"Սխալ տվյալների բազայից տվյալներ վերցնելիս: {e}", "error")
        crosswords, total_pages = [], 0

    return render_template('my_crosswords.html',
                           crosswords=crosswords,
                           page=page,
                           total_pages=total_pages,
                           per_page=per_page,
                           query=query)


@app.route('/delete_crossword/<int:id>', methods=['POST'])
@require_auth("Teacher", "OtherLecturer")
async def delete_crossword(id):
    try:
        user_id = g.current_user["sub"]
        deleted = await asyncio.to_thread(db_operation_wrapper, delete_crossword_from_db, id, user_id)
        if deleted:
            flash('Խաչբառը ջնջվեց։')
        else:
            flash('Չեք կարող ջնջել այս խաչբառը։')
    except Exception as e:
        flash(f"Սխալ ջնջելիս: {e}", "error")

    return redirect(url_for('my_crosswords'))


@app.route('/<int:id>')
async def view_crossword(id):
    # is_creator check-ը արդեն ճիշտ է՝ session-ով է, թողնել նույնը
    # կամ եթե g.current_user կա՝
    try:
        current_user_id = g.current_user["sub"]
    except:
        current_user_id = None
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)

    if not cw:
        return "Խաչբառը չի գտնվել։", 404

    cw        = dict(cw)
    crossword = json.loads(cw['data'])
    crossword.setdefault('global_hint', None)

    rows  = crossword.get('rows', 15)
    cols  = crossword.get('cols', 15)
    media = crossword.get('global_hint_media') or crossword.get('hint_value')
    if media:
        for prefix in ['static/', 'hints/']:
            if media.startswith(prefix):
                media = media.replace(prefix, '', 1)
                break
        if not media.startswith('uploads/'):
            media = f'uploads/{media}'
        crossword['global_hint_media'] = media
        crossword['hint_value']        = media

    current_user_id = session.get('user_id')
    is_creator      = False
    if current_user_id and 'user_id' in cw:
        if str(current_user_id) == str(cw['user_id']):
            is_creator = True

    bg_size_mode = crossword.get('bg_size_mode', 'fixed')
    bg_width     = crossword.get('bg_width', '400px')
    bg_height    = crossword.get('bg_height', '200px')

    return render_template(
        'crossword.html',
        crossword=crossword,
        cw_id=id,
        keyword=cw.get('keyword', ''),
        title=cw.get('title', 'Խաչբառ'),
        bg=cw.get('background'),
        rows=rows,
        cols=cols,
        show_intro=True,
        description=cw.get('description', ''),
        is_creator=is_creator,
        creator_name=cw.get('creator_name', ''),
        bg_size_mode=bg_size_mode,
        bg_width=bg_width,
        bg_height=bg_height
    )


@app.route('/')
def index():
    return redirect("https://mainlogin.dshh.am:15107/Login/Login")


@app.route("/crossword_form")
def crossword_form():
    bg_folder = os.path.join(app.static_folder, "backgrounds")
    bg_images = [f for f in os.listdir(bg_folder) if f.lower().endswith((".jpg", ".jpeg"))]
    return render_template("crossword_form.html", bg_images=bg_images)



@app.route('/dashboard')
def dashboard():
    app.logger.debug("✅ dashboard route hit")
    return render_template('dashboard.html')

@app.route('/dashboard-debug')
def dashboard_debug():
    auth = request.headers.get("Authorization", "ՉԿԱ")
    return jsonify({
        "auth_header": auth[:100] if len(auth) > 50 else auth,
        "all_headers": dict(request.headers)
    })
@app.route('/debug')
async def debug():
    def get_tables_list(conn):
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return c.fetchall()

    tables = await asyncio.to_thread(db_operation_wrapper, get_tables_list)
    return f"Բազայում առկա աղյուսակներ՝ {tables}"


def prepare_grid_data(crossword):
    grid_data = {}
    for word in crossword['placed']:
        word_text = word['word']
        x         = word['x']
        y         = word['y']
        number    = word['number']
        direction = word['dir']
        is_keyword = (word['question'] == 'Բանալի բառ')

        for i, ch in enumerate(word_text):
            if direction == 'h':
                row = y
                col = x + i
            else:
                row = y + i
                col = x

            key = f"{row},{col}"

            if key not in grid_data:
                grid_data[key] = {
                    'letter':     ch,
                    'number':     str(number) if i == 0 else '',
                    'is_keyword': is_keyword
                }
            else:
                if grid_data[key]['letter'] != ch:
                    print(f"⚠️ Կոնֆլիկտ ({row}, {col}) դիրքում: {grid_data[key]['letter']} vs {ch}")

                if number and i == 0:
                    if grid_data[key]['number']:
                        grid_data[key]['number'] = f"{grid_data[key]['number']},{number}"
                    else:
                        grid_data[key]['number'] = str(number)

                if is_keyword:
                    grid_data[key]['is_keyword'] = True

    return grid_data


@app.route('/crossword_text/<int:id>')
async def crossword_text(id):
    cw = await asyncio.to_thread(db_operation_wrapper, fetch_crossword_by_id_public, id)

    if not cw:
        return "Խաչբառը չի գտնվել։", 404

    crossword = json.loads(cw['data'])
    rows      = crossword.get('rows', 15)
    cols      = crossword.get('cols', 15)
    grid      = [['.' for _ in range(cols)] for _ in range(rows)]

    for word in crossword.get('placed', []):
        x, y      = word['x'], word['y']
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

    crossword = json.loads(data_json)
    return crossword['placed']


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        print(f"ADMIN_REQUIRED CHECK - session: {dict(session)}")
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

        conn  = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        admin = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        conn.close()

        if admin and check_password_hash(admin['password'], password):
            session['admin']    = True
            session['admin_id'] = admin['id']
            session.modified    = True
            print(f"✅ LOGIN OK - session: {dict(session)}")
            return redirect(url_for('admin_dashboard'))

        flash("Սխալ մուտքանուն կամ գաղտնաբառ")
    return render_template('admin/login.html')


@app.route('/admin/logout')
@admin_required
def admin_logout():
    session.pop('admin', None)
    session.pop('admin_id', None)
    flash("Դուրս եք եկել ադմին պանելից")
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    page     = request.args.get('page', 1, type=int)
    per_page = 5

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    total       = conn.execute("SELECT COUNT(*) FROM crosswords").fetchone()[0]
    total_pages = (total + per_page - 1) // per_page
    offset      = (page - 1) * per_page

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
                           crosswords=crosswords,
                           page=page,
                           total_pages=total_pages)


@app.route('/admin/crossword/results/<int:id>')
@admin_required
def admin_crossword_results(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    results = conn.execute("""
                           SELECT p.name,
                                  p.surname,
                                  p.school,
                                  p.grade,
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
    return render_template('admin/results.html',
                           results=results,
                           crossword_id=id)


@app.route('/admin/crossword/delete/<int:id>', methods=['POST'])
@admin_required
def admin_delete_crossword(id):
    conn = sqlite3.connect(DATABASE)
    conn.execute("DELETE FROM crosswords WHERE id=?", (id,))
    conn.execute("DELETE FROM participants WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM results WHERE crossword_id=?", (id,))
    conn.execute("DELETE FROM questions WHERE crossword_id=?", (id,))
    conn.commit()
    conn.close()
    flash("Խաչբառը ջնջվեց")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/crossword/toggle/<int:id>', methods=['POST'])
@admin_required
def admin_toggle_crossword(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    row     = conn.execute("SELECT hidden FROM crosswords WHERE id=?", (id,)).fetchone()
    current = row['hidden'] if row else 0
    conn.execute("UPDATE crosswords SET hidden=? WHERE id=?", (0 if current else 1, id))
    conn.commit()
    conn.close()
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

    cw_dict           = dict(cw)
    cw_dict['grades'] = cw_dict.get('grades', '')

    cw_data           = json.loads(cw_dict.get('data', '{}'))
    placed_questions  = cw_data.get('placed', [])

    questions      = conn.execute("SELECT * FROM questions WHERE crossword_id=?", (cw_id,)).fetchall()
    questions_list = [dict(q) for q in questions]

    bg_images = os.listdir(os.path.join(app.static_folder, "backgrounds"))

    if request.method == "POST":
        title       = request.form.get("title", "").strip()
        keyword     = request.form.get("keyword", "").strip().upper()
        description = request.form.get("description", "").strip()
        grades_list = request.form.getlist("grades")
        grades_str  = ",".join(grades_list) if grades_list else ""

        bg_file   = request.files.get("bg_upload")
        bg_choice = request.form.get("bg_choice", "")
        bg_path   = cw_dict.get("background", "")

        if bg_file and bg_file.filename:
            filename  = secure_filename(bg_file.filename)
            filename  = f"bg_{int(time.time())}_{filename}"
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            bg_file.save(save_path)
            bg_path = f"uploads/{filename}"
        elif bg_choice:
            bg_path = f"backgrounds/{bg_choice}"

        new_questions = []
        for key in request.form:
            if key.startswith("question_"):
                index     = key.split("_")[1]
                q_text    = request.form.get(f"question_{index}", "").strip()
                a_text    = request.form.get(f"answer_{index}", "").strip().upper()
                number    = request.form.get(f"number_{index}", "").strip()
                direction = request.form.get(f"dir_{index}", "h")
                if q_text and a_text:
                    new_questions.append({
                        "question": q_text,
                        "answer":   a_text,
                        "number":   int(number) if number.isdigit() else None,
                        "dir":      direction
                    })

        cw_data['placed']      = new_questions
        cw_data['description'] = description
        cw_data['grades']      = grades_list

        try:
            conn.execute("""
                UPDATE crosswords
                SET title=?, keyword=?, background=?, data=?, description=?, grades=?
                WHERE id=?
            """, (title, keyword, bg_path, json.dumps(cw_data, ensure_ascii=False),
                  description, grades_str, cw_id))

            conn.execute("DELETE FROM questions WHERE crossword_id=?", (cw_id,))
            for q in new_questions:
                conn.execute("""
                    INSERT INTO questions (crossword_id, question, answer, number, dir)
                    VALUES (?, ?, ?, ?, ?)
                """, (cw_id, q['question'], q['answer'], q['number'], q['dir']))

            conn.commit()
            flash("Խաչբառը և հարցերը հաջողությամբ թարմացվեցին")
            return redirect(url_for("admin_dashboard"))
        except Exception as e:
            conn.rollback()
            flash(f"Սխալ տվյալների բազայում՝ {e}", "error")
            return redirect(url_for("admin_edit_full_crossword", cw_id=cw_id))

    conn.close()
    return render_template("admin/edit_full_crossword.html",
                           cw=cw_dict,
                           placed=placed_questions,
                           questions=questions_list,
                           bg_images=bg_images)


@app.route('/admin/change_credentials', methods=['GET', 'POST'])
@admin_required
def admin_change_credentials():
    conn  = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    admin = conn.execute(
        "SELECT * FROM admins WHERE id = ?",
        (session['admin_id'],)
    ).fetchone()

    if not admin:
        conn.close()
        flash("Ադմինը գտնված չէ")
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        new_username = request.form.get('username', '').strip()
        old_password = request.form.get('old_password', '')
        new_password = request.form.get('new_password', '')

        if not check_password_hash(admin['password'], old_password):
            flash("Հին գաղտնաբառը սխալ է ❌")
            conn.close()
            return render_template("admin/change_credentials.html", admin=admin)

        try:
            hashed = generate_password_hash(new_password)
            conn.execute(
                "UPDATE admins SET username = ?, password = ? WHERE id = ?",
                (new_username, hashed, admin['id'])
            )
            conn.commit()
            print("DEBUG: DB updated successfully")
        except Exception as e:
            print(f"DEBUG ERROR: {e}")
            flash(f"Սխալ՝ {e} ❌")
            conn.close()
            return render_template("admin/change_credentials.html", admin=admin)

        conn.close()
        session['admin']    = True
        session['admin_id'] = admin['id']
        flash("Տվյալները հաջողությամբ փոփոխվեցին ✅")
        return redirect(url_for('admin_dashboard'))

    conn.close()
    return render_template("admin/change_credentials.html", admin=admin)


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
    check_and_add_created_at_column()

    app.run(host="0.0.0.0", port=15400, debug=True, ssl_context='adhoc')
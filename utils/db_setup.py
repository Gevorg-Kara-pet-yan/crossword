import os
import aiosqlite
import asyncio
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, 'crossword.db')

async def init_db():
    print("Սկսվում է տվյալների բազայի ինիցիալիզացիան...")

    async with aiosqlite.connect(db_path) as conn:
        # 1. Օգտատերերի աղյուսակ
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        ''')

        # 2. Խաչբառների աղյուսակ (արդեն ներառված են subject և grade դաշտերը)
        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS crosswords
                           (
                               id
                               INTEGER
                               PRIMARY
                               KEY
                               AUTOINCREMENT,
                               user_id
                               INTEGER,
                               title
                               TEXT,
                               keyword
                               TEXT,
                               background
                               TEXT,
                               data
                               TEXT,
                               description
                               TEXT,
                               creator_name
                               TEXT,
                               subject
                               TEXT,
                               grade
                               TEXT,
                               is_ai_generated
                               INTEGER
                               DEFAULT
                               0,
                               hidden
                               INTEGER
                               DEFAULT
                               0,
                               created_at
                               TIMESTAMP
                               DEFAULT
                               CURRENT_TIMESTAMP,
                               FOREIGN
                               KEY
                           (
                               user_id
                           ) REFERENCES users
                           (
                               id
                           )
                               )
                           ''')

        # 3. Մասնակիցների աղյուսակ
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                surname TEXT NOT NULL,
                gender TEXT NOT NULL,
                school TEXT,
                grade TEXT,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(crossword_id) REFERENCES crosswords(id)
            )
        ''')

        # 4. Արդյունքների աղյուսակ
        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS results
                           (
                               id
                               INTEGER
                               PRIMARY
                               KEY
                               AUTOINCREMENT,
                               participant_id
                               INTEGER
                               NOT
                               NULL,
                               crossword_id
                               INTEGER
                               NOT
                               NULL,
                               score
                               INTEGER,
                               duration
                               INTEGER,
                               is_completed
                               INTEGER
                               DEFAULT
                               0,
                               end_time
                               TIMESTAMP
                               DEFAULT
                               CURRENT_TIMESTAMP,
                               FOREIGN
                               KEY
                           (
                               participant_id
                           ) REFERENCES participants
                           (
                               id
                           ),
                               FOREIGN KEY
                           (
                               crossword_id
                           ) REFERENCES crosswords
                           (
                               id
                           )
                               )
                           ''')

        # Թարմացում total_words
        new_result_columns = [("total_words", "INTEGER")]
        for col_name, col_type in new_result_columns:
            try:
                await conn.execute(f"ALTER TABLE results ADD COLUMN {col_name} {col_type}")
                print(f"✅ Սյունակ {col_name}-ը ավելացվեց results աղյուսակում:")
            except Exception:
                pass

        # 5. Հարցերի աղյուսակ
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crossword_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                FOREIGN KEY(crossword_id) REFERENCES crosswords(id) ON DELETE CASCADE
            )
        ''')

        # 6. Ադմինների աղյուսակ
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        ''')

        # 7. Default ադմին
        async with conn.execute("SELECT * FROM admins WHERE username = ?", ('admin',)) as cursor:
            if not await cursor.fetchone():
                hashed_pw = generate_password_hash('12345')
                await conn.execute("INSERT INTO admins (username, password) VALUES (?, ?)", ('admin', hashed_pw))
                print("Ադմին օգտատերը ստեղծվեց՝ username='admin', password='12345'")

        # --- ԿԱՐԵՎՈՐ: Սյունակների թարմացում (Upgrade) ---
        # Եթե բազան արդեն կար, բայց առանց subject/grade սյունակների
        new_columns = [
            ("subject", "TEXT"),
            ("grade", "TEXT"),
            ("is_ai_generated", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in new_columns:
            try:
                await conn.execute(f"ALTER TABLE crosswords ADD COLUMN {col_name} {col_type}")
                print(f"✅ Սյունակ {col_name}-ը ավելացվեց:")
            except Exception:
                # Եթե սյունակը արդեն կա, SQLite-ը սխալ կտա, մենք պարզապես անցնում ենք առաջ
                pass

        await conn.commit()
    print("✅ Տվյալների բազան պատրաստ է։")

if __name__ == "__main__":
    try:
        asyncio.run(init_db())
    except Exception as e:
        print(f"❌ Սխալ: {e}")
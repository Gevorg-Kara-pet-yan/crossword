# 🔄 Database / Migrations
## 📦 Database Migration Process

Since the project uses **SQLite** and does not rely on a dedicated migration library (e.g., Alembic), schema migrations are managed **manually**.

---

## 1️⃣ How to Create a Migration

Whenever you need to modify the database schema (e.g., to add a new column), you should create a dedicated Python script:

migrations/M_YYYYMMDD_feature_name.py


Inside the script, define a function that connects to `utils/crossword.db` and executes the required SQL commands.

### Example: Adding a New Column

```python
```python
import sqlite3
import os

def run_migration():
    db_path = os.path.join(os.path.dirname(__file__), '..', 'utils', 'crossword.db')
    conn = sqlite3.connect(db_path)
    try:
        # Add a new column
        conn.execute("ALTER TABLE crosswords ADD COLUMN created_at TEXT;")
        conn.commit()
        print("Migration successfully executed: 'created_at' column added.")
    except sqlite3.OperationalError as e:
        # Ignore if the column already exists
        if "duplicate column name" in str(e):
            print("Column already exists. Migration skipped.")
        else:
            raise e
    finally:
        conn.close()

if __name__ == '__main__':
    run_migration()

```

2️⃣ How to Apply a Migration

Run the created script using Python:

```python
python migrations/M_YYYYMMDD_feature_name.py
```

3️⃣ How to Perform a Rollback

Rollback must be done manually, since SQLite has limitations with ALTER TABLE (e.g., dropping columns is not straightforward).

## Methods:

### 1.Safe but Complex:

- Create a new table with the desired schema

- Transfer the data

- Delete the old table

- Rename the new table

### 2.Simple:

- For minor changes (e.g., updating a default value), fix it with another migration script

⚠️ Creating rollback scripts is highly recommended for any significant schema changes.
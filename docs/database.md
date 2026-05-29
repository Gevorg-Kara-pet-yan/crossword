# 💾 Database / Schema  
## 📊 Database Schema (SQLite)

The system revolves around the **`crosswords`** table, which stores all generated crossword puzzles, their metadata, and grid data.

---

## 📑 `crosswords` Table

| Field Name   | Type    | Constraints                         | Description |
|--------------|---------|--------------------------------------|-------------|
| **id**       | INTEGER | PRIMARY KEY, NOT NULL                | Unique identifier for each crossword entry. |
| **user_id**  | INTEGER | NOT NULL                             | ID of the user who created the crossword (currently hardcoded as `1` in the application). |
| **title**    | TEXT    | NOT NULL                             | Title of the crossword puzzle. |
| **keyword**  | TEXT    | —                                    | The crossword’s keyword, if used. |
| **background** | TEXT  | —                                    | Relative path of the background image (`uploads/...` or `backgrounds/...`). |
| **data**     | TEXT    | NOT NULL                             | Full crossword grid data and design configuration stored as a JSON string. |
| **description** | TEXT | —                                    | Short description of the crossword. |

---

## 🔑 Foreign Key Relations


> **Note:** The `users` table is **not yet implemented** in the system, but this field is reserved for future integration to link crosswords to their creators.


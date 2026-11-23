# 💻 Coding Guidelines  
Official coding standards for the Crossword Generator System

---

## ✍️ Python Coding Style (PEP 8)

### 🆔 Naming Conventions

| Code Element        | Style        | Example(s)                              |
|--------------------|-------------|-----------------------------------------|
| **Functions & Methods** | `snake_case` | `delete_crossword_from_db`, `upload_bg` |
| **Variables**          | `snake_case` | `user_id`, `total_pages`                |
| **Classes**            | `PascalCase` | `CrosswordBuilder`, Flask view classes  |

---

### 📐 Spacing & Formatting Rules

- Use **4 spaces** for indentation (**no tabs**)
- Keep **route definitions** directly above the function they belong to  
  ❌ Not recommended:
```python
@app.route('/home')
def home():
```
  
  

- Keep line length within 79–88 characters

- Maintain consistent blank lines between logical code blocks

⚡ Asynchronous I/O Conventions
When to Use Async

Any function performing heavy I/O operations such as:

- SQLite database queries

- File upload / delete operations

- JSON read/write

Must be either:

1. An async def function, or

2. Executed inside a thread using:

```python
await asyncio.to_thread(...)
```

Required Wrapper

All synchronous SQLite operations must be wrapped using:

```python
db_operation_wrapper
```


Example:

```python
await asyncio.to_thread(
    db_operation_wrapper,
    fetch_crossword_by_id,
    id,
    1
)
```

📝 Commenting & Documentation Style

📚 Docstrings

Each major function should include a Python docstring:

```python
def insert_new_crossword_db(...):
    """
    Inserts a new crossword record into the database.

    Args:
        title (str): Crossword title
        data (dict): Crossword grid and metadata

    Returns:
        int: Newly created crossword ID
    """
```

Docstring must describe:

- Function purpose

- Function parameters

- Return value

🧾 Inline Comments

Use inline comments sparingly and only for complex or async-related operations:

```python
# ⚠️ async file write
await save_file_async(path, content)
```

🔄 Git Commit Message Rules

Follow the Conventional Commits standard:

| Type       | Purpose                                        | Example                                  |
|------------|------------------------------------------------|------------------------------------------|
| feat       | Add new functionality                          | feat: add crossword edit route           |
| fix        | Bug fixes                                      | fix: correct DB fetch error              |
| refactor   | Code cleanup without behavioral changes        | refactor: convert DB ops to asyncio.to_thread |
| docs       | Documentation updates                          | docs: update architecture.md             |
| style      | Pure formatting or styling changes             | style: format input_questions.html       |


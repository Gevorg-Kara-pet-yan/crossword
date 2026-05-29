# 📁 Folder Structure  
## 📂 Project Directory Layout

| Directory / File                 | Description |
|----------------------------------|-------------|
| `/`                              | Project root directory |
| `app.py`                         | Main Flask application file; contains routes, DB functions, and async I/O logic |
| `/static`                        | Static files (CSS, JS, images) |
| `/static/css`                    | Stylesheets |
| `/static/js`                     | JavaScript files |
| `/static/backgrounds`            | Default background images |
| `/static/uploads`                | User-uploaded background and media files |
| `/static/hints`                  | User-uploaded hint-related media |
| `/templates`                     | Jinja2 frontend templates |
| `input_questions.html`           | Form for entering questions and generating a crossword |
| `edit_crossword.html`            | Page for editing existing crosswords |
| `view_crossword.html`            | Page for displaying a crossword puzzle |
| `my_crosswords.html`             | List of crosswords created by the current user |
| `/utils`                         | Utility modules |
| `/utils/crossword.db`            | SQLite database file |
| `/utils/crossword_builder.py`    | Module that generates the crossword grid (imported, source not shown) |
| `/docs`                          | Documentation folder (this section) |


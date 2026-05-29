# 🏛️ Architecture  
## 🧱 High-Level Architecture

The system is built as a **Monolithic Flask Web Application** operating under a standard **Client–Server model**, combining backend logic, frontend rendering, and database access within a single codebase.

A distinguishing characteristic of this architecture is the integration of **asynchronous I/O** using `asyncio.to_thread` to prevent blocking during I/O-heavy operations (such as file handling and SQLite database interactions).

---

## 🌐 Frontend / Backend / Services Overview

| Component            | Description |
|---------------------|-------------|
| **Frontend (Client)** | Implemented with **HTML/CSS/JavaScript**, using the **Jinja2** template engine provided by Flask. Responsible for rendering user input forms and displaying generated crossword puzzles. |
| **Backend (Server)** | The core service built in **Python** using the **Flask** microframework. Manages routing, input validation, crossword generation logic, and DB/File I/O operations. |
| **Database Service** | Uses an **SQLite** database stored in a local file (`crossword.db`). All database operations are wrapped inside `db_operation_wrapper` and executed asynchronously via `asyncio.to_thread`. |
| **I/O Service** | File operations (upload and delete) are executed through asynchronous functions (`save_file_async`, `delete_file_async`) that utilize `asyncio.to_thread` to offload blocking I/O to separate threads. |
| **Crossword Builder** | An external module located at `utils/crossword_builder.py`, responsible for performing CPU-intensive crossword grid generation. |

---

## ⚙️ Core Technologies Used

| Category | Technology | Notes |
|----------|-----------|-------|
| **Language** | Python | Main programming language for backend and application logic. |
| **Web Framework** | Flask | Lightweight microframework used to build the web application. |
| **Asynchronous I/O** | asyncio, functools.wraps | Enables non-blocking I/O operations and asynchronous route handling. |
| **Database** | SQLite | Lightweight file-based database for storing application data (crosswords and metadata). |
| **Utilities** | werkzeug.utils, os, json | Used for secure file handling, filesystem operations, and JSON serialization. |


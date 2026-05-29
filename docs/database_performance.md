# 🚀 Database / Performance
## 📈 Performance and Load Testing

This document outlines the key performance and efficiency considerations of the system.

---

## 1️⃣ Load Handling (I/O Bottlenecks)

Since the application is based on Flask and runs in a single-threaded environment, **I/O-heavy operations** can cause blocking.

**Solution:** The application uses the `asyncio.to_thread` mechanism.

- **DB Operations:**  
  All `db_operation_wrapper` calls are executed in a separate thread, allowing Flask’s main thread to continue processing other requests while waiting for the database.

- **File I/O:**  
  File upload/delete operations (`save_file_async`, `delete_file_async`) are also executed asynchronously in separate threads, minimizing blocking caused by network and disk operations.

**Result:**  
This significantly improves system concurrency and allows processing more simultaneous requests without degrading overall performance.

---

## 2️⃣ Profiling CPU-Heavy Operations

**Issue:**  
Crossword grid generation (`generate_crossword_grid` in `utils/crossword_builder.py`) is a CPU-intensive algorithm.

**Current Behavior:**  
Generation happens synchronously inside the `input_questions` and `edit_crossword` asynchronous routes.

**Requirement:**  
If the generation time exceeds a few seconds, this function should be moved to a **background task queue** (e.g., Celery or Redis Queue), so the request is not blocked for too long, and the user receives an immediate response (`202 Accepted`).

---

## 3️⃣ Database Efficiency

**SQLite Limitations:**  
SQLite cannot handle multiple concurrent write operations.

**Implementation Choice:**  
All database operations run within a single thread through `db_operation_wrapper`, ensuring data integrity while limiting write concurrency.

**Efficient Reads:**  
The `fetch_crosswords_list` function uses `LIMIT` and `OFFSET` for pagination, improving performance when dealing with large lists.

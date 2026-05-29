# 📜 Changelog
## 🔄 Historical Change Log

### 1.0.0 – November 2025 (Initial Release)

**New Functionality (feat):**
- Added the main crossword generation logic (Grid Builder).
- Introduced asynchronous (async) route support in Flask using `asyncio.to_thread`.
- Added CRUD operations (Create, Read, Update, Delete) for crosswords.
- Added asynchronous media upload for background images and hints.
- Added the ability to download crossword data in JSON format.

**Structural Changes (refactor):**
- All DB operations are wrapped in `db_operation_wrapper`, ensuring transaction integrity.
- SQLite was chosen as the database backend.

**Performance Improvements (perf):**
- Optimized I/O bottlenecks by moving file and DB operations to separate threads.

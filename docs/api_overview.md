# 🌐 API Overview  
## 💡 API Architecture Overview

The **Crossword Generator System** exposes an API that is primarily *internal*, since the project is implemented as a **monolithic Flask application**.  
However, it provides several REST-style endpoints used for file operations and returning crossword-related data.

---

## 🔒 Authentication

The current implementation **does not include real authentication or authorization**.

### Current State
- **Model:**  
  `user_id = 1` is hardcoded in all DB operations  
  *(e.g., `insert_new_crossword_db`, `fetch_crossword_by_id`)*

- **Session Layer:**  
  Flask `session` object exists for future use:
```python
session.get('user_id')
```

but not applied at the database level.

🚀 Future Improvement

Authentication should be implemented using one of the following:

| Option             | Description                                |
|-------------------|--------------------------------------------|
| JWT-based auth     | Token-based access for stateless endpoints |
| Session-based auth | Standard login model with Flask session cookies |

This will allow:
✔️ Proper user-specific data isolation
✔️ Secure access control

🏷️ Versioning

The current API does not use explicit versioning like:

```python
/api/v1/...
```

Current Endpoint Style

Endpoints are defined at the root level:

```python
/upload-bg
/crossword/<int:id>

```
📈 Future Direction

Versioned routing should be introduced as the system grows to ensure backward compatibility and endpoint lifecycle management.

🧪 How to Access or Test the API

The API provides endpoints mainly for file I/O and simple data retrieval.

Recommended Tools
| Tool               | Purpose                                                                  |
|-------------------|--------------------------------------------------------------------------|
| Postman / Insomnia | Best for testing POST requests with multipart/form-data (e.g., /upload-bg) |
| Swagger / OpenAPI  | ❌ Not available — no schema exposed                                      |
| Manual Reference   | All endpoints are documented in api/endpoints.md                          |

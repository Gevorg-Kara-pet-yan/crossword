# 🔑 Access Control
## 🛑 Access Control and Permissions

This document describes how the system manages user roles and permissions to ensure data security.

---

## 1️⃣ Roles and Permissions (RBAC/ABAC)

The current system operates in a single-user model (hard-coded `user_id = 1`). Future development should implement **Role-Based Access Control (RBAC).**

| Role (Future)       | Allowed Actions                       | Current Implementation                                           |
|--------------------|--------------------------------------|-----------------------------------------------------------------|
| Creator / Owner     | Create, View, Edit, Delete their own crosswords | The code enforces that DB operations are performed only for the given `user_id`. |
| Viewer              | View published crosswords             | Not implemented                                                 |
| Admin               | Manage all users and all crosswords   | Not implemented                                                 |

---

## 2️⃣ Ownership Enforcement (Row-Level Security)

The system currently ensures that each user can access or modify only the records they created.

- **Implementation:**  
  All DB read/write functions (e.g., `fetch_crossword_by_id`, `update_crossword_db`, `delete_crossword_from_db`) always include the `user_id` parameter in the `WHERE` clause.

- **SQL Example:**

```sql
-- Ensures that the update occurs only if both ID and user_id match
UPDATE crosswords SET ... WHERE id = ? AND user_id = ?

Route Verification:
In view_crossword and edit_crossword routes, the incoming user_id is checked against the crossword creator's ID (is_creator logic).

3️⃣ Session Management

Session Key: Flask sessions are protected using app.secret_key (see security.md).

Token Expiration: Since there is no real authentication, token expiration is not applied. In the future, if JWTs are implemented, tokens should have a short lifetime (e.g., 15–30 minutes) to minimize potential damage from stolen tokens.

4️⃣ Audit Logs

Current Status: The system does not store logs of actions (e.g., "User X updated crossword Y at Time Z").

Requirement: Systems with high-security requirements should implement a module that logs all POST/PUT/DELETE operations into a separate audit_logs table.

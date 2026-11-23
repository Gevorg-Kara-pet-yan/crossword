# 💾 Database (Security Additions)
## 🔒 Database Security

This section outlines database configuration and access control security requirements, especially relevant when moving to PostgreSQL/MySQL in production.

---

## 1️⃣ Restricting Database Access

- **IP Whitelisting:**  
  The database server should accept connections only from the internal IP addresses of the application servers and from the administrator via VPN/jump box.

- **Separation of Database Accounts:**  
  Separate DB users should be created for different purposes:

| User               | Permissions                                                      |
|-------------------|------------------------------------------------------------------|
| app_user (Primary)  | Can perform only CRUD operations on the main crosswords table. |
| admin_user          | Has schema-modification (migration) and administrative privileges. |
| read_only_user (Optional) | Can only read data (useful for reporting).                  |

---

## 2️⃣ SQL Injection Prevention

- **Protection:** The current code is fully protected against SQL Injection by using prepared statements (`?` placeholders) for all input data with `sqlite3`.  

```python
conn.execute(
    "SELECT * FROM crosswords WHERE id = ? AND user_id = ?",
    (id, user_id)  # Data passed as a separate tuple
).fetchone()
```

#### - Dangerous Practice: It is strictly forbidden to use Python string formatting (f-strings or %s) to insert user-provided data into SQL queries.

## 3️⃣ Data Encryption

- At-Rest Encryption: Data stored on disk must be encrypted.
Solution: Use full disk encryption or the database service’s built-in encryption features (e.g., AWS RDS encryption).

- In-Transit Encryption: All connections between application servers and the database must use SSL/TLS encryption.

## ⚡ Performance & Scalability (Security Additions)
### 📈 Performance and Scalability Security Measures

This section focuses on security considerations related to system performance and scalability.

## 1️⃣ DoS/DDoS Attack Mitigation

The system must be protected against Denial of Service (DoS) attacks that attempt to overload it.

- Rate Limiting: Use Nginx or a dedicated service (e.g., Cloudflare) to limit the number of requests a single IP address can make within a given time frame.

- Firewall Rules: Block suspicious or abusive IP addresses at the network layer.

## 2️⃣ Heavy CPU Task Management

- Problem: The  generate_crossword_grid  function can serve as a DoS vector if an attacker continuously sends extremely complex or large generation requests.

- Solution:

    - Job Queues: As mentioned in scalability.md, heavy crossword generation should be moved to background tasks (Celery), preventing blocking of Flask’s main thread.

    - Input Restrictions: Limit the size of requests (e.g., maximum number of input questions) to prevent infinite resource consumption.

## 3️⃣ Caching Security

If a caching layer (Redis) is used, ensure sensitive data is not stored in cache, or is encrypted if necessary.

- Sensitive Data Exclusion:

    - Strictly prohibit caching user password hashes or personally identifiable information (PII).

    - The data field (containing crossword content) can be cached, as it is public content.

- Redis Security:

    - Cache servers must be password-protected if the network is not fully isolated.

    - Access should be restricted to application servers only.
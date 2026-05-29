# 📈 Database / Scalability
## ⚖️ System Scalability Strategies

This document explains how the **Crossword Generator** system can scale as the number of users and data volume grow.

---

## 1️⃣ Database Scaling

**Issue:**  
SQLite is ideal for small projects but becomes insufficient under heavy load or when handling multiple concurrent write operations.

**Solution:**  
Under increased load, the system should migrate to **MySQL** or **PostgreSQL**, which provide native network access and support multithreaded write operations.

---

## 2️⃣ Horizontal Scaling

**Current State:**  
The monolithic Flask application can run multiple processes using WSGI servers (e.g., Gunicorn or uWSGI).

**Requirements:**

- **Load Balancer:**  
  Introduce a load balancer (e.g., Nginx) to distribute incoming traffic across multiple Flask instances.

- **Session Management:**  
  Since the project uses Flask sessions, introduce session stickiness or store sessions in a centralized cache (e.g., Redis) to maintain continuity across multiple instances.

---

## 3️⃣ Caching Layer

Caching can significantly reduce database load.

- **Redis / Memcached:** Integrate Redis for caching purposes.

**Types of Caching:**

- **Result Caching:** Cache large, frequently requested crossword JSON payloads returned by the `view_crossword` route.  
- **List Caching:** Cache paginated results of the `my_crosswords` page, especially when the search query is empty.

---

## 4️⃣ Task Queues

**Model:**  
Heavy operations (such as crossword generation) should be decoupled from the main request flow.

**Requirement:**  
Use **Celery** (task queue) and **Redis/RabbitMQ** (message broker).

**Flow:**

1. User sends data via `POST /input`  
2. The application stores the crossword as Pending and returns `202 Accepted`  
3. A Celery worker generates the crossword grid and updates the database when complete

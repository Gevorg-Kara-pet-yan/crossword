# ⚙️ Deployment (Security Additions)
## 🔒 Secure Deployment Configuration

This section details the security measures that should be applied when deploying to production.

---

## 1️⃣ Server Firewall Configuration

- **Policy:** "Deny All, Allow Necessary."  
- **Nginx / Load Balancer:** Only ports **80 (HTTP)** and **443 (HTTPS)** should be open to external access.  
- **App Server (Gunicorn/uWSGI):** These servers should listen on the local interface only (e.g., `127.0.0.1:8000`) to prevent direct access from external networks. Access must go through Nginx.  
- **DB / Redis:** Should only be accessible from the internal IP addresses of the application servers.

---

## 2️⃣ SSL/TLS Certificate Management

- **TLS 1.3/1.2:** Only TLS 1.2 or 1.3 protocols should be used; older protocols (SSLv3, TLS 1.0/1.1) must be disabled in the Nginx configuration.  
- **Certificate Issuance:** Use trusted issuers (e.g., Let’s Encrypt or Cloudflare) to automatically issue and renew certificates in a timely manner.

---

## 3️⃣ Secure Secrets Management

It is critical that sensitive data (DB passwords, API keys, `app.secret_key`) are **not stored in the codebase** or in unsecured environment variables.

- **CI/CD Pipeline:** Use secure storage for secrets.  
- **HashiCorp Vault:** Full solution for centralized secrets management.  
- **GitHub/GitLab Secrets:** Use built-in secrets management to securely pass secrets during CI/CD stages.  
- **Production Environment:** Secrets should be injected via environment variables or through container platform features (e.g., Kubernetes Secrets).

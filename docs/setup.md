# 🛠️ Setup  
## ⚙️ Environment Setup Instructions

This document is intended for developers who want to install and run the **Crossword Generator System** in a local development environment.

---

## 1️⃣ Software Requirements

Ensure the following software components are installed:

| Software | Purpose |
|---------|---------|
| **Python 3.8+** | Primary programming language of the system |
| **Git** | Version control for source code |


## 2️⃣ Clone the Repository (if applicable)

```python
git clone <repository_url>
cd crossword-generator
```

3️⃣ Install Dependencies

It is recommended to use a virtual environment to isolate project dependencies.

🔧 Create a Virtual Environment
```python
python3 -m venv venv
```

▶️ Activate the Virtual Environment

Linux/macOS:

```python
source venv/bin/activate
```

Windows (Command Prompt):

```python
venv\Scripts\activate
```

📦 Install Required Packages

(Assumes the project contains a requirements.txt file with Flask, Werkzeug, asyncio, etc.)

```python
pip install -r requirements.txt
```

4️⃣ Configure Environment Variables

Some app settings are currently hardcoded in app.py. Key values to know:

🔐 Secret Key
```python
app.secret_key = 'af0934dj!@3!sdgfasdf1231jf9'
```


⚠️ Security Note:
Replace this with a secure value and store it in an .env file for both development and production environments.

📁 File Upload Directory

Defined as:

```python
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
```


✔️ This directory is automatically created when the application runs.

5️⃣ Database Initialization / Migrations

The system uses SQLite and requires the crossword.db file with the appropriate schema.

Component	         Location

Database File	  utils/crossword.db

If an initialization script (init_db.py) is not included, create the database manually using:

```python
CREATE TABLE IF NOT EXISTS crosswords (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    keyword TEXT,
    background TEXT,
    data TEXT NOT NULL,
    description TEXT
);
```

6️⃣ Running the Application Locally

Start the Flask application:

```python
python app.py
```

🌍 Access the App
http://0.0.0.0:15500


(According to the app.run() configuration in app.py)

🛠️ Debug Mode

The system runs with:

debug=True
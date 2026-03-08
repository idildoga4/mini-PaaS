import sqlite3
from datetime import datetime

DB_PATH = "data/paas_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            github_url TEXT NOT NULL,
            status TEXT NOT NULL,
            port INTEGER,
            subdomain TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Eski veritabanında subdomain sütunu yoksa ekle
    try:
        cursor.execute("ALTER TABLE deployments ADD COLUMN subdomain TEXT")
    except Exception:
        pass  # Sütun zaten varsa hata vermeden geç

    conn.commit()
    conn.close()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
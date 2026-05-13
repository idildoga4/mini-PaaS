import sqlite3
import os

DB_PATH = "data/auth.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL UNIQUE,
            password     TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            github_token TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN github_token TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

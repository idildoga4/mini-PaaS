import sqlite3
import os

DB_PATH = "data/github.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS github_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL UNIQUE,
            github_token TEXT,
            updated_at   TEXT NOT NULL
        )
    """)
    # Deploy bilgisi — push-to-deploy için repo → kullanıcı eşlemesi
    c.execute("""
        CREATE TABLE IF NOT EXISTS repo_mappings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name    TEXT NOT NULL,
            user_email   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
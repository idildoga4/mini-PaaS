import sqlite3
import os
from datetime import datetime

DB_PATH = "data/paas_data.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS deployments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email   TEXT NOT NULL DEFAULT '',
            project_name TEXT NOT NULL,
            github_url   TEXT NOT NULL,
            status       TEXT NOT NULL,
            port         INTEGER,
            subdomain    TEXT,
            created_at   TEXT NOT NULL
        )
    """)
    # Eski kurulumlar için eksik kolonları ekle
    for col, definition in [("subdomain", "TEXT"), ("user_email", "TEXT NOT NULL DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE deployments ADD COLUMN {col} {definition}")
        except Exception:
            pass

    # Aktif (doğrulanmış) kullanıcılar
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT NOT NULL UNIQUE,
            password   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            github_token TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN github_token TEXT")
    except Exception:
        pass

    # Kayıt sırasında bekleyen doğrulama kodları
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_verifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT NOT NULL UNIQUE,
            password   TEXT NOT NULL,
            code       TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
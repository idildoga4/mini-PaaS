import sqlite3
import os

DB_PATH = "data/deploy.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Projeler tablosu — her proje bir kez oluşturulur
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            github_url   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            UNIQUE(user_email, project_name)
        )
    """)

    # Deployment'lar tablosu — her deploy için bir kayıt
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

    conn.commit()
    conn.close()

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
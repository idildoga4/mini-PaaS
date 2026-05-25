import sqlite3
import os

# FAZ 6: github-service yalnızca github.db'ye bağlanır.
# data/ klasöründe görülen deploy.db bir kalıntı dosyadır — silinmeli.
DB_PATH = "data/github.db"


def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # FAZ 6: WAL mode — eş zamanlı okuma/yazma güvenli hale gelir.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS github_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL UNIQUE,
            github_token TEXT,
            updated_at   TEXT NOT NULL
        )
    """)
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
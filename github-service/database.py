"""
FAZ 7: SQLite → PostgreSQL geçişi
- sqlite3 kaldırıldı, psycopg2 eklendi
- WAL mode, check_same_thread, PRAGMA'lar kaldırıldı
- AUTOINCREMENT → SERIAL
- CONNECTION_URL env variable'dan geliyor
- RealDictCursor ile row dict dönüşümü
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://paas_user:paas_pass@postgres:5432/github_db"
)


def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS github_tokens (
            id           SERIAL PRIMARY KEY,
            email        TEXT NOT NULL UNIQUE,
            github_token TEXT,
            updated_at   TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS repo_mappings (
            id           SERIAL PRIMARY KEY,
            repo_name    TEXT NOT NULL,
            user_email   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def get_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn
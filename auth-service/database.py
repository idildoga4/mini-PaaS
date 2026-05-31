"""
FAZ 7: SQLite → PostgreSQL geçişi
- sqlite3 kaldırıldı, psycopg2 eklendi
- WAL mode, check_same_thread, PRAGMA'lar kaldırıldı
- ALTER TABLE migration bloğu kaldırıldı (PostgreSQL'de IF NOT EXISTS yeterli)
- Connection string DATABASE_URL env variable'dan geliyor
- Row dict dönüşümü: sqlite3.Row yerine RealDictCursor kullanılıyor
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://paas_user:paas_pass@postgres:5432/auth_db"
)


def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           SERIAL PRIMARY KEY,
            email        TEXT NOT NULL UNIQUE,
            password     TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            github_token TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn
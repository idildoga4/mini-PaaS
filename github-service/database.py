"""
FAZ 7: SQLite → PostgreSQL geçişi
- sqlite3 kaldırıldı, psycopg2 eklendi
- WAL mode, check_same_thread, PRAGMA'lar kaldırıldı
- CONNECTION_URL env variable'dan geliyor
- RealDictCursor ile row dict dönüşümü

FAZ 8: init_db() retry mekanizması eklendi.
- Swarm restart'larında postgres DNS geç çözülebiliyor.
- 5 deneme × 3 saniye bekleme = maksimum 15 saniye tolerans.
"""

import os
import time
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://paas_user:paas_pass@postgres:5432/github_db"
)

_MAX_RETRIES = 5
_RETRY_DELAY = 3  # saniye


def init_db():
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
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
            print(f"[DB] PostgreSQL bağlantısı başarılı (deneme {attempt}/{_MAX_RETRIES})")
            return
        except psycopg2.OperationalError as e:
            last_error = e
            print(f"[DB] PostgreSQL bağlantı hatası (deneme {attempt}/{_MAX_RETRIES}): {e}")
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)

    raise RuntimeError(
        f"[DB] PostgreSQL'e {_MAX_RETRIES} denemede bağlanılamadı: {last_error}"
    )


def get_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn
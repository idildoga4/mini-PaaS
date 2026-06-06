"""
FAZ 7: SQLite → PostgreSQL geçişi
FAZ 8: init_db() retry mekanizması eklendi.
FAZ 8 (teknik borç): DATABASE_URL'deki şifre artık Docker Secret'tan okunuyor.
- /run/secrets/postgres_password → Docker Swarm Secret
- POSTGRES_PASSWORD env variable → docker-compose / local geliştirme fallback
- Şifre artık docker-stack.yml'de düz metin olarak görünmüyor.
"""

import os
import time
import psycopg2
import psycopg2.extras
from secrets_helper import get_secret

_MAX_RETRIES = 5
_RETRY_DELAY = 3  # saniye


def _build_database_url() -> str:
    """
    DATABASE_URL'i dinamik oluşturur.
    Şifre önce Docker Secret'tan, yoksa env variable'dan okunur.
    """
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")

    host     = os.getenv("POSTGRES_HOST", "postgres")
    port     = os.getenv("POSTGRES_PORT", "5432")
    user     = os.getenv("POSTGRES_USER", "paas_user")
    db       = os.getenv("POSTGRES_DB",   "deploy_db")
    password = get_secret("postgres_password", "POSTGRES_PASSWORD")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = _build_database_url()


def init_db():
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            conn = get_connection()
            c = conn.cursor()

            c.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id           SERIAL PRIMARY KEY,
                    user_email   TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    github_url   TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    UNIQUE(user_email, project_name)
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS deployments (
                    id             SERIAL PRIMARY KEY,
                    user_email     TEXT NOT NULL DEFAULT '',
                    project_name   TEXT NOT NULL,
                    github_url     TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    port           INTEGER,
                    subdomain      TEXT,
                    container_name TEXT,
                    error_message  TEXT,
                    created_at     TEXT NOT NULL
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
    database_url = _build_database_url()
    conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


# ── FAZ 4 A.1: upsert_project (PostgreSQL) ───────────────────────────────────
def upsert_project(conn, user_email: str, project_name: str, github_url: str,
                   container_name: str = "") -> int:
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO projects (user_email, project_name, github_url, created_at)
        VALUES (%s, %s, %s, NOW()::text)
        ON CONFLICT (user_email, project_name)
        DO UPDATE SET github_url = EXCLUDED.github_url
        RETURNING id
        """,
        (user_email, project_name, github_url)
    )
    row = c.fetchone()
    conn.commit()
    project_id = row["id"]
    print(f"[DB] Proje upsert: {user_email}/{project_name} → id={project_id}")
    return project_id
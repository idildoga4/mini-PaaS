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
    db       = os.getenv("POSTGRES_DB",   "github_db")
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
                    updated_at   TEXT NOT NULL,
                    UNIQUE (repo_name, user_email)
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

def upsert_project(repo_name: str, user_email: str, project_name: str):
    """
    Kullanıcının repo eşleşmesini veritabanına kaydeder.
    Eğer repo_name ve user_email zaten varsa, projeyi günceller (PostgreSQL ON CONFLICT).
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        current_time = str(time.time())
        
        c.execute("""
            INSERT INTO repo_mappings (repo_name, user_email, project_name, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (repo_name, user_email) 
            DO UPDATE SET 
                project_name = EXCLUDED.project_name,
                updated_at = EXCLUDED.updated_at
        """, (repo_name, user_email, project_name, current_time))
        
        conn.commit()
        print(f"[DB] Upsert basarili: {repo_name} -> {project_name}")
    except Exception as e:
        conn.rollback()
        print(f"[DB] Upsert hatasi: {e}")
        raise e
    finally:
        conn.close()
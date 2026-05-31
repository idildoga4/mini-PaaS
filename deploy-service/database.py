"""
FAZ 7: SQLite → PostgreSQL geçişi
- sqlite3 kaldırıldı, psycopg2 eklendi
- WAL mode, _migrate(), PRAGMA'lar, check_same_thread kaldırıldı
- AUTOINCREMENT → SERIAL
- ? → %s placeholder
- lastrowid → RETURNING id
- datetime('now') → NOW()::text
- upsert_project: ayrı SELECT+INSERT/UPDATE yerine tek ON CONFLICT sorgusu
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://paas_user:paas_pass@postgres:5432/deploy_db"
)


def init_db():
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


def get_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


# ── FAZ 4 A.1: upsert_project (PostgreSQL) ───────────────────────────────────
def upsert_project(conn, user_email: str, project_name: str, github_url: str,
                   container_name: str = "") -> int:
    """
    Push-to-deploy akışında projects tablosuna kayıt oluşturur ya da günceller.
    PostgreSQL ON CONFLICT ... DO UPDATE ile tek sorguda yapılıyor.
    Dönüş: projects satırının id değeri
    """
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
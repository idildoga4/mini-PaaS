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
    # FAZ 4 A.2: container_name kolonu eklendi (kullanıcı-proje bazlı benzersiz ad)
    c.execute("""
        CREATE TABLE IF NOT EXISTS deployments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email     TEXT NOT NULL DEFAULT '',
            project_name   TEXT NOT NULL,
            github_url     TEXT NOT NULL,
            status         TEXT NOT NULL,
            port           INTEGER,
            subdomain      TEXT,
            container_name TEXT,
            created_at     TEXT NOT NULL
        )
    """)

    conn.commit()

    # ── Mevcut veritabanı migration (tek seferlik, güvenli) ──────────────────
    # Eski deploy.db dosyaları container_name kolonuna sahip olmayabilir.
    # ALTER TABLE IF NOT EXISTS kolonu SQLite'ta desteklenmediği için
    # PRAGMA table_info ile kontrol edip ekliyoruz.
    _migrate(conn)

    conn.close()


def _migrate(conn):
    """
    Mevcut veritabanına eksik kolonları ekler.
    Kolon zaten varsa hiçbir şey yapmaz (idempotent).
    """
    c = conn.cursor()

    # deployments tablosundaki mevcut kolonları öğren
    c.execute("PRAGMA table_info(deployments)")
    existing_cols = {row[1] for row in c.fetchall()}

    if "container_name" not in existing_cols:
        c.execute("ALTER TABLE deployments ADD COLUMN container_name TEXT")
        print("[DB migration] deployments.container_name kolonu eklendi")

    conn.commit()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── FAZ 4 A.1: upsert_project ────────────────────────────────────────────────
def upsert_project(conn, user_email: str, project_name: str, github_url: str,
                   container_name: str = "") -> int:
    """
    Push-to-deploy akışında projects tablosuna kayıt oluşturur ya da günceller.

    • Proje yoksa → INSERT (push ile ilk kez görülen proje)
    • Proje varsa → github_url güncelle (repo değişmiş olabilir)

    Bu fix sayesinde push-to-deploy ile gelen deploy'lar Projects
    sayfasında görünür hale gelir.

    Dönüş: projects satırının id değeri
    """
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (user_email, project_name)
    )
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            """
            INSERT INTO projects (user_email, project_name, github_url, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (user_email, project_name, github_url)
        )
        conn.commit()
        project_id = cursor.lastrowid
        print(f"[DB] Proje upsert → INSERT: {user_email}/{project_name}")
    else:
        project_id = row[0]
        cursor.execute(
            """
            UPDATE projects
            SET github_url = ?
            WHERE id = ?
            """,
            (github_url, project_id)
        )
        conn.commit()
        print(f"[DB] Proje upsert → UPDATE: {user_email}/{project_name}")

    return project_id
"""
migrate_to_postgres.py
======================
Mevcut SQLite veritabanlarındaki verileri PostgreSQL'e taşır.

Kullanım (proje kökünden, PostgreSQL ayaktayken):
    python migrate_to_postgres.py

Ön koşullar:
    1. PostgreSQL servisi ayakta ve erişilebilir olmalı
       - docker-compose: localhost:5433
       - Swarm: docker service ps mini-paas_postgres çalışıyor olmalı
    2. .env dosyasında POSTGRES_PASSWORD ayarlı olmalı
    3. psycopg2-binary kurulu olmalı: pip install psycopg2-binary

Güvenli: ON CONFLICT DO NOTHING — tekrar çalıştırılsa mevcut veriler bozulmaz.
"""

import sqlite3
import psycopg2
import psycopg2.extras
import os
import sys

POSTGRES_USER     = os.getenv("POSTGRES_USER",     "paas_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "paas_pass")
POSTGRES_HOST     = os.getenv("POSTGRES_HOST",     "localhost")
POSTGRES_PORT     = os.getenv("POSTGRES_PORT",     "5433")


def pg_conn(db_name: str):
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=int(POSTGRES_PORT),
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=db_name,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def migrate_auth():
    sqlite_path = "auth-service/data/auth.db"
    if not os.path.exists(sqlite_path):
        print(f"[auth] {sqlite_path} bulunamadı, atlanıyor")
        return

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = pg_conn("auth_db")
    dc = dst.cursor()

    rows = src.execute("SELECT * FROM users").fetchall()
    print(f"[auth] {len(rows)} kullanıcı taşınıyor...")

    for row in rows:
        dc.execute(
            """
            INSERT INTO users (id, email, password, created_at, github_token)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (email) DO NOTHING
            """,
            (row["id"], row["email"], row["password"],
             row["created_at"], row["github_token"])
        )

    if rows:
        # SERIAL sequence'ı güncelle — sonraki INSERT'lerin id çakışmaması için
        dc.execute("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))")

    dst.commit()
    dst.close()
    src.close()
    print("[auth] ✓ Tamamlandı")


def migrate_deploy():
    sqlite_path = "deploy-service/data/deploy.db"
    if not os.path.exists(sqlite_path):
        print(f"[deploy] {sqlite_path} bulunamadı, atlanıyor")
        return

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = pg_conn("deploy_db")
    dc = dst.cursor()

    # Önce tabloları oluştur (servis henüz ayağa kalkmadıysa)
    dc.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id           SERIAL PRIMARY KEY,
            user_email   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            github_url   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            UNIQUE(user_email, project_name)
        )
    """)
    dc.execute("""
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
    dst.commit()

    # Projects
    projects = src.execute("SELECT * FROM projects").fetchall()
    print(f"[deploy] {len(projects)} proje taşınıyor...")
    for row in projects:
        dc.execute(
            """
            INSERT INTO projects (id, user_email, project_name, github_url, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_email, project_name) DO NOTHING
            """,
            (row["id"], row["user_email"], row["project_name"],
             row["github_url"], row["created_at"])
        )
    if projects:
        dc.execute("SELECT setval('projects_id_seq', (SELECT MAX(id) FROM projects))")

    # Deployments
    cols = {r[1] for r in src.execute("PRAGMA table_info(deployments)").fetchall()}
    deployments = src.execute("SELECT * FROM deployments").fetchall()
    print(f"[deploy] {len(deployments)} deployment taşınıyor...")
    for row in deployments:
        dc.execute(
            """
            INSERT INTO deployments
                (id, user_email, project_name, github_url, status, port,
                 subdomain, container_name, error_message, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                row["id"],
                row["user_email"],
                row["project_name"],
                row["github_url"],
                row["status"],
                row["port"],
                row["subdomain"],
                row["container_name"] if "container_name" in cols else None,
                row["error_message"]  if "error_message"  in cols else None,
                row["created_at"],
            )
        )
    if deployments:
        dc.execute("SELECT setval('deployments_id_seq', (SELECT MAX(id) FROM deployments))")

    dst.commit()
    dst.close()
    src.close()
    print("[deploy] ✓ Tamamlandı")


def migrate_github():
    sqlite_path = "github-service/data/github.db"
    if not os.path.exists(sqlite_path):
        print(f"[github] {sqlite_path} bulunamadı, atlanıyor")
        return

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = pg_conn("github_db")
    dc = dst.cursor()

    # Önce tabloları oluştur
    dc.execute("""
        CREATE TABLE IF NOT EXISTS github_tokens (
            id           SERIAL PRIMARY KEY,
            email        TEXT NOT NULL UNIQUE,
            github_token TEXT,
            updated_at   TEXT NOT NULL
        )
    """)
    dc.execute("""
        CREATE TABLE IF NOT EXISTS repo_mappings (
            id           SERIAL PRIMARY KEY,
            repo_name    TEXT NOT NULL,
            user_email   TEXT NOT NULL,
            project_name TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)
    dst.commit()

    # GitHub tokens
    tokens = src.execute("SELECT * FROM github_tokens").fetchall()
    print(f"[github] {len(tokens)} token taşınıyor...")
    for row in tokens:
        dc.execute(
            """
            INSERT INTO github_tokens (id, email, github_token, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email) DO NOTHING
            """,
            (row["id"], row["email"], row["github_token"], row["updated_at"])
        )
    if tokens:
        dc.execute("SELECT setval('github_tokens_id_seq', (SELECT MAX(id) FROM github_tokens))")

    # Repo mappings
    mappings = src.execute("SELECT * FROM repo_mappings").fetchall()
    print(f"[github] {len(mappings)} repo mapping taşınıyor...")
    for row in mappings:
        dc.execute(
            """
            INSERT INTO repo_mappings (id, repo_name, user_email, project_name, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (row["id"], row["repo_name"], row["user_email"],
             row["project_name"], row["updated_at"])
        )
    if mappings:
        dc.execute("SELECT setval('repo_mappings_id_seq', (SELECT MAX(id) FROM repo_mappings))")

    dst.commit()
    dst.close()
    src.close()
    print("[github] ✓ Tamamlandı")


if __name__ == "__main__":
    print("=" * 50)
    print("Mini PaaS — SQLite → PostgreSQL Migration")
    print("=" * 50)
    print(f"Hedef: {POSTGRES_HOST}:{POSTGRES_PORT} ({POSTGRES_USER})")
    print()

    try:
        migrate_auth()
        migrate_deploy()
        migrate_github()
        print()
        print("✓ Migration tamamlandı. Artık SQLite volume'ları kaldırabilirsin.")
    except psycopg2.OperationalError as e:
        print(f"\n✗ PostgreSQL bağlantı hatası: {e}")
        print("  Kontrol et:")
        print(f"    - postgres servisi ayakta mı? (port {POSTGRES_PORT})")
        print(f"    - POSTGRES_PASSWORD doğru mu?")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Beklenmeyen hata: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

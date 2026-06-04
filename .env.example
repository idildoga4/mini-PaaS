"""
test_b2.py — Orphan Container Health Check Testi
=================================================
Deploy Service startup'ta Running deployment'ların container'ı
gerçekten çalışıyor mu diye builder-service'e sorarak kontrol eder.
Bu test bu davranışı doğrular.

Kullanım (proje kökünden):
    python test_b2.py

Ön koşullar:
    - Sistem docker stack ile ayakta olmalı
    - pip install psycopg2-binary (zaten kurulu olmalı)
"""

import psycopg2
import psycopg2.extras
import requests
import os
import time

POSTGRES_HOST     = os.getenv("POSTGRES_HOST",     "localhost")
POSTGRES_PORT     = int(os.getenv("POSTGRES_PORT", "5433"))
POSTGRES_USER     = os.getenv("POSTGRES_USER",     "paas_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "paas_pass")

DEPLOY_SERVICE_URL = os.getenv("DEPLOY_SERVICE_URL", "http://localhost:5003")


def pg_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname="deploy_db",
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def test_orphan_check():
    print("=" * 50)
    print("Orphan Container Health Check Testi")
    print("=" * 50)

    # 1. Sahte bir Running deployment ekle (var olmayan container)
    conn = pg_conn()
    c = conn.cursor()

    fake_container = "app-orphantest-nonexistent"
    c.execute(
        """
        INSERT INTO deployments
            (user_email, project_name, github_url, status, container_name, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW()::text)
        RETURNING id
        """,
        ("test@test.com", "orphantest", "https://github.com/test/test",
         "Running", fake_container)
    )
    deploy_id = c.fetchone()["id"]
    conn.commit()
    print(f"[1] Sahte Running deployment eklendi → id={deploy_id}, container={fake_container}")

    # 2. Deploy service'i restart et (startup hook'u tetiklemek için)
    print("[2] Deploy service yeniden başlatılıyor...")
    os.system("docker service update --force mini-paas_deploy-service > NUL 2>&1")
    print("[2] Restart komutu gönderildi, 20 saniye bekleniyor...")
    time.sleep(20)

    # 3. Deployment'ın durumunu kontrol et
    conn2 = pg_conn()
    c2 = conn2.cursor()
    c2.execute(
        "SELECT id, status, error_message FROM deployments WHERE id=%s",
        (deploy_id,)
    )
    row = c2.fetchone()
    conn2.close()

    if not row:
        print(f"[3] HATA: deployment id={deploy_id} bulunamadı!")
        return False

    print(f"[3] Deployment durumu: status={row['status']}, error_message={row['error_message']}")

    if row["status"] == "Failed":
        print("[✓] TEST BAŞARILI: Orphan container tespit edildi ve Failed yapıldı")
        success = True
    else:
        print(f"[✗] TEST BAŞARISIZ: Beklenen 'Failed', gelen '{row['status']}'")
        success = False

    # 4. Test kaydını temizle
    conn3 = pg_conn()
    c3 = conn3.cursor()
    c3.execute("DELETE FROM deployments WHERE id=%s", (deploy_id,))
    conn3.commit()
    conn3.close()
    print(f"[4] Test kaydı temizlendi (id={deploy_id})")

    return success


if __name__ == "__main__":
    result = test_orphan_check()
    print()
    if result:
        print("✓ Test geçti.")
    else:
        print("✗ Test başarısız.")
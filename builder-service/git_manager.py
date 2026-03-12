import subprocess
import os
import shutil
import stat
import re


def on_rm_error(func, path, exc_info):
    """Windows'ta salt-okunur dosyaları silerken izin hatası çıkar. Bunu çözer."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def build_authenticated_url(repo_url: str) -> str:
    # Koda gömdüğün token'ı buradan SİL, yerine sadece tırnak bırak
    # Token'ı artık sadece .env veya docker-compose üzerinden alacağız
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if not token:
        print("[git] ⚠️ GITHUB_TOKEN bulunamadı!")
        return repo_url

    base_url = repo_url.replace("https://", "")
    authenticated = f"https://oauth2:{token}@{base_url}"
    return authenticated


def clone_repo(repo_url: str, project_name: str):
    """
    GitHub reposunu ./workspace/<project_name> klasörüne klonlar.

    Adımlar:
    1. workspace klasörü yoksa oluştur
    2. Aynı isimde eski klasör varsa sil (temiz başlangıç)
    3. Token'lı URL ile git clone çalıştır
    4. Başarılı → klasör yolunu döndür | Hatalı → None döndür
    """
    base_dir = "./workspace"
    os.makedirs(base_dir, exist_ok=True)

    project_path = os.path.join(base_dir, project_name)

    # Eski klonu temizle — aynı proje tekrar deploy ediliyorsa
    if os.path.exists(project_path):
        print(f"[git] Eski '{project_name}' klasörü siliniyor...")
        shutil.rmtree(project_path, onexc=on_rm_error)

    # Token'ı URL'ye göm
    authenticated_url = build_authenticated_url(repo_url)

    print(f"[git] Repo klonlanıyor: {project_name}")

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", authenticated_url, project_path],
            # --depth 1: sadece son commit'i al, tüm geçmişi değil
            # Bu build'i çok daha hızlı yapar (büyük repolarda dakikalar kazanılır)
            capture_output=True,
            text=True,
            timeout=120  # 2 dakika içinde bitmezse iptal et
        )

        if result.returncode == 0:
            print(f"[git] ✅ Başarıyla klonlandı: {project_path}")
            return project_path
        else:
            # Hata mesajından token'ı temizle, sonra logla
            safe_err = result.stderr.replace(
                os.environ.get("GITHUB_TOKEN", ""), "***"
            )
            print(f"[git] ❌ Clone hatası:\n{safe_err}")
            return None

    except subprocess.TimeoutExpired:
        print(f"[git] ❌ Zaman aşımı — 2 dakika içinde tamamlanamadı")
        return None
    except Exception as e:
        print(f"[git] ❌ Beklenmeyen hata: {e}")
        return None

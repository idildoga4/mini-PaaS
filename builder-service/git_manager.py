import subprocess
import os
import shutil
import stat
import re


def on_rm_error(func, path, exc_info):
    """Windows'ta salt-okunur dosyaları silerken izin hatası çıkar. Bunu çözer."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def build_authenticated_url(repo_url: str, user_token: str = "") -> str:
    """
    Token önceliği:
      1. Kullanıcının OAuth token'ı
      2. Token yoksa URL'yi olduğu gibi bırak (public repo)
    """
    token = user_token.strip()

    if not token:
        print("[git] Token bulunamadı — public repo ise aynen deneniyor")
        return repo_url

    print("[git] Kullanıcı token'ı ile kimlik doğrulama aktif")
    base_url = repo_url.replace("https://", "")
    return f"https://oauth2:{token}@{base_url}"


def clone_repo(repo_url: str, project_name: str, user_token: str = ""):
    """
    GitHub reposunu ./workspace/<project_name> klasörüne klonlar.
    """
    base_dir = "./workspace"
    os.makedirs(base_dir, exist_ok=True)

    project_path = os.path.join(base_dir, project_name)
    log_path = os.path.join(base_dir, f"{project_name}.log")

    # Eski klonu temizle
    if os.path.exists(project_path):
        print(f"[git] Eski '{project_name}' klasörü siliniyor...")
        shutil.rmtree(project_path, onerror=on_rm_error)

    authenticated_url = build_authenticated_url(repo_url, user_token)

    print(f"[git] Repo klonlanıyor: {project_name}")

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", authenticated_url, project_path],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            print(f"[git] ✅ Başarıyla klonlandı: {project_path}")
            return project_path
        else:
            # Token'ı log'dan gizle
            safe_err = result.stderr
            if user_token:
                safe_err = safe_err.replace(user_token, "***")

            print(f"[git] ❌ Clone hatası:\n{safe_err}")

            # Hata mesajını log dosyasına yaz (dashboard drawer'da görünsün)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"[git] ❌ Repo klonlanamadı:\n")
                f.write(safe_err + "\n")
                f.write("[error occurred]\n")

            return None

    except subprocess.TimeoutExpired:
        msg = "[git] ❌ Zaman aşımı — 2 dakika içinde tamamlanamadı"
        print(msg)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.write("[error occurred]\n")
        return None

    except Exception as e:
        msg = f"[git] ❌ Beklenmeyen hata: {e}"
        print(msg)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.write("[error occurred]\n")
        return None
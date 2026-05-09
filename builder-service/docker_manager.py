from asyncio import log
import subprocess
import os
import yaml
import threading

# traefik_dynamic.yml'e aynı anda iki build yazmasın diye kilit
_traefik_lock = threading.Lock()

DYNAMIC_YML = "/etc/traefik/dynamic.yml"


def update_traefik(router_name: str, container_name: str):
    """
    Deploy edilen uygulamayı Traefik'e kayıt eder.

    NEDEN BUNU YAPIYORUZ?
    Docker provider (--providers.docker) Windows'ta sorunlu olduğu için
    kapattık. Bunun yerine traefik_dynamic.yml dosyasını elle güncelliyoruz.
    Traefik --providers.file.watch=true ile bu dosyayı izliyor,
    değişince otomatik yeniliyor — restart gerekmez.

    Sonuç: http://demo-app.localhost:8090 → app-demo-app container'ı
    """
    with _traefik_lock:  # iki build aynı anda dosyayı bozmasın
        try:
            with open(DYNAMIC_YML, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}

        # Temel yapı yoksa oluştur
        config.setdefault("http", {})
        config["http"].setdefault("routers", {})
        config["http"].setdefault("services", {})

        # Yeni router: hangi subdomain'den gelirse bu servise git
        config["http"]["routers"][router_name] = {
            "rule":        f"Host(`{router_name}.localhost`)",
            "service":     router_name,
            "entryPoints": ["web"]
        }

        # Yeni service: o subdomain'i bu container'a yönlendir
        # container_name = "app-demo-app" — Docker iç ağında (paas-net) erişilebilir
        config["http"]["services"][router_name] = {
            "loadBalancer": {
                "servers": [{"url": f"http://{container_name}:80"}]
            }
        }

        with open(DYNAMIC_YML, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        print(f"[traefik] ✅ Routing eklendi: {router_name}.localhost → {container_name}")


def build_and_deploy(project_path: str, project_name: str) -> bool:
    """
    3 adımda deploy:
      1. docker build  → klonlanan repodan image üret
      2. docker run    → container'ı paas-net ağında başlat
      3. traefik güncelle → subdomain yönlendirmesini ekle
    """
    image_name     = f"{project_name.lower()}-img"
    container_name = f"app-{project_name.lower()}"
    router_name    = project_name.lower().replace("_", "-").replace(" ", "-")
    log_path       = f"./workspace/{project_name.lower()}.log"

    os.makedirs("./workspace", exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as log_file:

        def log(msg):
            print(msg, flush=True)
            log_file.write(msg + "\n")
            log_file.flush()

        log(f"\n[*] '{project_name}' için build başlatıldı...")

        # Eski image varsa sil (yeni build öncesi temizlik)
        log(f"[*] Eski image temizleniyor: {image_name}")
        subprocess.run(
            ["docker", "rmi", "-f", image_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
)

        try:
            # ── 1. Docker image build ─────────────────────────────
            log(f"[*] Image build ediliyor: {image_name}")
            subprocess.run(
                ["docker", "build", "--progress=plain", "-t", image_name, project_path],
                check=True,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            log(f"[+] Image başarıyla oluşturuldu: {image_name}")

            # ── 2. Eski container varsa sil, yenisini başlat ──────
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            log(f"[*] Container başlatılıyor: {container_name}")
            subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name",    container_name,
                    "--network", "paas-net",
                    # NOT: -l (label) etiketleri artık işe yaramıyor çünkü
                    # Docker provider kapalı. Traefik routing'i update_traefik()
                    # fonksiyonu ile traefik_dynamic.yml üzerinden yapıyoruz.
                    image_name
                ],
                check=True,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            log(f"[+] Container başlatıldı: {container_name}")

            # ── 3. Traefik'e subdomain kaydını ekle ───────────────
            update_traefik(router_name, container_name)

            # Traefik'i restart et (Windows'ta file watch çalışmadığı için)
            try:
                subprocess.run(["docker", "restart", "traefik_proxy"],
                                capture_output=True, timeout=30)
                log(f"[+] Traefik yenilendi")
            except Exception as e:
                log(f"[!] Traefik restart başarısız: {e}")

            log(f"[+] SUCCESS! Uygulama yayında:")
            log(f"    http://{router_name}.localhost:8090")
            log("[SUCCESS!]")
            return True

        except subprocess.CalledProcessError as e:
            log(f"[-] Docker hatası: {e}")
            log("[error occurred]")
            return False
        except Exception as e:
            log(f"[-] Beklenmeyen hata: {e}")
            log("[error occurred]")
            return False


if __name__ == "__main__":
    build_and_deploy("./workspace/sample-app", "sample-app")
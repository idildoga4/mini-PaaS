from asyncio import log
import subprocess
import os
import yaml
import threading

# traefik_dynamic.yml'e aynı anda iki build yazmasın diye kilit
_traefik_lock = threading.Lock()

DYNAMIC_YML = "/etc/traefik/dynamic.yml"


def get_paas_network() -> str:
    """
    Swarm stack deploy edilince network adı "mini-paas_paas-net" olur.
    docker-compose ile çalışıyorsa "paas-net" kalır.
    PAAS_NETWORK env variable ile override edilebilir.
    """
    if os.getenv("PAAS_NETWORK"):
        return os.getenv("PAAS_NETWORK")
    result = subprocess.run(
        ["docker", "network", "ls", "--filter", "name=paas-net", "--format", "{{.Name}}"],
        capture_output=True, text=True
    )
    networks = [n.strip() for n in result.stdout.strip().splitlines() if "paas-net" in n]
    for n in networks:
        if "_paas-net" in n:
            return n
    return networks[0] if networks else "paas-net"


def get_traefik_container_id() -> str:
    """
    Swarm'da container adı 'mini-paas_traefik.1.xxxxx' formatında olur,
    sabit 'traefik_proxy' adı artık geçerli değil.
    """
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=mini-paas_traefik"],
        capture_output=True, text=True
    )
    container_id = result.stdout.strip()
    if not container_id:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=traefik_proxy"],
            capture_output=True, text=True
        )
        container_id = result.stdout.strip()
    return container_id


def update_traefik(router_name: str, container_name: str):
    """
    Deploy edilen uygulamayı Traefik'e kayıt eder.

    FAZ 4 A.2:
        router_name  = subdomain (örn. "omertank36-testapp")
        container_name = benzersiz container adı (örn. "omertank36_testapp")

    Önceki davranış:
        router_name = project_name (örn. "testapp")
        container_name = "app-testapp"
    """
    with _traefik_lock:
        try:
            with open(DYNAMIC_YML, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}

        config.setdefault("http", {})
        config["http"].setdefault("routers", {})
        config["http"].setdefault("services", {})

        # router_name artık subdomain değeri (örn. omertank36-testapp)
        config["http"]["routers"][router_name] = {
            "rule":        f"Host(`{router_name}.localhost`)",
            "service":     router_name,
            "entryPoints": ["web"]
        }

        # container_name artık kullanıcı bazlı benzersiz (örn. omertank36_testapp)
        config["http"]["services"][router_name] = {
            "loadBalancer": {
                "servers": [{"url": f"http://{container_name}:80"}]
            }
        }

        with open(DYNAMIC_YML, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        print(f"[traefik] ✅ Routing: {router_name}.localhost → {container_name}")


def restart_traefik():
    """
    Swarm modunda Traefik container'ını yeniden başlatır.
    Windows'ta --providers.file.watch=true çalışmadığı için gerekli.
    """
    container_id = get_traefik_container_id()
    if not container_id:
        print("[!] Traefik container bulunamadı, restart atlandı")
        return
    try:
        subprocess.run(
            ["docker", "restart", container_id],
            capture_output=True, timeout=30
        )
        print(f"[+] Traefik yenilendi (container: {container_id[:12]})")
    except Exception as e:
        print(f"[!] Traefik restart başarısız: {e}")


def build_and_deploy(project_path: str, project_name: str,
                     container_name: str = "", subdomain: str = "") -> bool:
    """
    3 adımda deploy:
      1. docker build  → klonlanan repodan image üret
      2. docker run    → container'ı paas-net ağında başlat
      3. traefik güncelle → subdomain yönlendirmesini ekle

    FAZ 4 A.2 parametreleri:
        container_name : Kullanıcı bazlı benzersiz container adı.
                         Örn: "omertank36_testapp"
                         Boş gelirse eski davranışa (app-{project_name}) geri düşer.
        subdomain      : Traefik router adı ve Host kuralı.
                         Örn: "omertank36-testapp"
                         Boş gelirse project_name kullanılır.
    """
    image_name = f"{project_name.lower()}-img"
    log_path   = f"./workspace/{project_name.lower()}.log"

    # FAZ 4 A.2: geriye uyumluluk — parametre gelmezse eski davranış
    if not container_name:
        container_name = f"app-{project_name.lower()}"
    if not subdomain:
        subdomain = project_name.lower().replace("_", "-").replace(" ", "-")

    os.makedirs("./workspace", exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as log_file:

        def _log(msg):
            print(msg, flush=True)
            log_file.write(msg + "\n")
            log_file.flush()

        _log(f"\n[*] '{project_name}' için build başlatıldı...")
        _log(f"[*] Container: {container_name} | Subdomain: {subdomain}.localhost")

        # Eski image varsa sil
        _log(f"[*] Eski image temizleniyor: {image_name}")
        subprocess.run(
            ["docker", "rmi", "-f", image_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        try:
            # ── 1. Docker image build ─────────────────────────────
            _log(f"[*] Image build ediliyor: {image_name}")
            subprocess.run(
                ["docker", "build", "--progress=plain", "-t", image_name, project_path],
                check=True,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            _log(f"[+] Image oluşturuldu: {image_name}")

            # ── 2. Eski container varsa sil, yenisini başlat ──────
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            paas_network = get_paas_network()
            _log(f"[*] Container başlatılıyor: {container_name} (network: {paas_network})")
            subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name",    container_name,   # FAZ 4 A.2: kullanıcı bazlı benzersiz
                    "--network", paas_network,
                    image_name
                ],
                check=True,
                stdout=log_file,
                stderr=subprocess.STDOUT
            )
            _log(f"[+] Container başlatıldı: {container_name}")

            # ── 3. Traefik'e subdomain kaydını ekle ───────────────
            # FAZ 4 A.2: router_name = subdomain (örn. omertank36-testapp)
            update_traefik(subdomain, container_name)
            restart_traefik()

            _log(f"[+] SUCCESS! Uygulama yayında:")
            _log(f"    http://{subdomain}.localhost:8090")
            _log("[SUCCESS!]")
            return True

        except subprocess.CalledProcessError as e:
            _log(f"[-] Docker hatası: {e}")
            _log("[error occurred]")
            return False
        except Exception as e:
            _log(f"[-] Beklenmeyen hata: {e}")
            _log("[error occurred]")
            return False


if __name__ == "__main__":
    build_and_deploy("./workspace/sample-app", "sample-app")
from fastapi import FastAPI, BackgroundTasks, WebSocket, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os
import requests
from git_manager import clone_repo
from docker_manager import build_and_deploy

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API sözleşmesi: Builder'ın beklediği 3 alan
class DeployRequest(BaseModel):
    deploy_id:    int   # Veritabanındaki kayıt ID'si — webhook geri bildirimi için
    repo_url:     str   # Klonlanacak GitHub URL'si
    project_name: str   # Docker image ve container'a verilecek isim
    github_token: str = ""  # Kullanıcının OAuth token'ı (opsiyonel)

    def validate_repo_url(self):
        """
        Geçerli bir URL mi kontrol et.
        Boş string, sadece kelime ("dca", "test") veya http/https olmayan
        girişleri reddeder. Böylece git clone hiç çalışmadan hata döner.
        """
        url = self.repo_url.strip()
        if not url:
            raise ValueError("repo_url boş olamaz")
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("git@")):
            raise ValueError(f"Geçersiz repo URL'si: '{url}'. http://, https:// veya git@ ile başlamalı")
        return url

# 2. PİPELINE İŞLEMİ BİTİNCE HABER VERME EKLENDİ
def run_pipeline(deploy_id: int, repo_url: str, project_name: str, github_token: str = ""):
    """
    CI/CD pipeline'ının ana fonksiyonu. Sırayla şunları yapar:
      1. git clone  → repoyu ./workspace/<proje> klasörüne çeker
      2. docker build + run  → image üretir, container başlatır
      3. webhook  → API Gateway'e "bitti, durum şu" diye haber verir

    Subdomain nasıl çalışır?
      docker_manager.py container'ı şu etiketle başlatıyor:
        traefik.http.routers.<proje>.rule = Host(`<proje>.localhost`)
      Traefik bu etiketi okuyup yönlendirmeyi otomatik yapıyor.
      Yani http://test.localhost:8090 → app-test container'ına gidiyor.
    """
    # Proje adını temizle — boşluk, büyük harf, özel karakter kabul etmez
    # "Test App" → "test-app", "My_Project" → "my-project"
    import re
    project_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')
    project_name = re.sub(r'-+', '-', project_name)  # çoklu tireyi tekleştir

    router_name = project_name
    subdomain   = f"{router_name}.localhost"

    project_path = clone_repo(repo_url, project_name, github_token)
    status = "Failed"  # varsayılan — build başarısız olursa bu kalır

    if project_path:
        success = build_and_deploy(project_path, project_name)
        if success:
            status = "Running"

    # Webhook: API Gateway'e sonucu bildir
    # port alanı artık subdomain'i taşıyor — "8321" gibi anlamsız sayı değil
    # API Gateway bunu veritabanına yazacak, dashboard "Open" linkini buradan üretecek
    webhook_url = "http://paas_api:8000/api/webhook"
    payload = {
        "deploy_id": deploy_id,
        "status":    status,
        "port":      8090,      # Traefik her zaman 8090'dan yayın yapıyor
        "subdomain": subdomain  # "test.localhost" — dashboard bu adresi açacak
    }

    try:
        requests.post(webhook_url, json=payload, timeout=10)
        print(f"[builder] Webhook gönderildi → ID:{deploy_id} | {status} | {subdomain}")
    except Exception as e:
        print(f"[builder] Webhook hatası: {e}")

@app.get("/health")
async def health():
    """
    Servis sağlık kontrolü.
    Docker Swarm ve Prometheus bu endpoint'i periyodik olarak çağırır.
    'status: ok' dönerse servis sağlıklı sayılır.
    """
    # Workspace klasörü erişilebilir mi?
    workspace_ok = os.path.exists("./workspace") or True  # ilk çalıştırmada henüz yoktur
    return {
        "status": "ok",
        "service": "builder-service",
        "workspace": workspace_ok
    }

@app.get("/logs/{project_name}")
async def get_logs(project_name: str):
    import re
    project_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')
    log_path = f"./workspace/{project_name}.log"
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log dosyası bulunamadı")
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return {"lines": [l.strip() for l in lines if l.strip()]}

@app.post("/deploy")
async def deploy_project(req: DeployRequest, background_tasks: BackgroundTasks):
    """
    API Gateway'den gelen deploy talebini alır.
    1. URL'yi doğrula — geçersizse hemen hata dön, git clone'u deneme
    2. Pipeline'ı arka planda başlat — endpoint hemen yanıt versin (non-blocking)
    3. Pipeline bitince builder kendi webhook'unu atar (run_pipeline içinde)
    """
    # Adım 1: URL geçerli mi?
    try:
        req.validate_repo_url()
    except ValueError as e:
        # 400 Bad Request: kullanıcıya neyin yanlış olduğunu söyle
        raise HTTPException(status_code=400, detail=str(e))

    # Adım 2: İşi arka plana al, hemen yanıt ver
    # BackgroundTasks: FastAPI'nin built-in async task sistemi
    # Bu sayede endpoint "200 OK" döndürür, build arkada devam eder
    background_tasks.add_task(run_pipeline, req.deploy_id, req.repo_url, req.project_name, req.github_token)

    return {
        "message":      f"{req.project_name} için deployment başlatıldı",
        "deploy_id":    req.deploy_id,
        "logs_websocket": f"/ws/{req.project_name}"  # Canlı log için WebSocket adresi
    }

@app.websocket("/ws/{project_name}")
async def websocket_endpoint(websocket: WebSocket, project_name: str):
    await websocket.accept()
    log_path = f"./workspace/{project_name.lower()}.log"

    # Log dosyası oluşana kadar bekle (max 60 saniye)
    waited = 0
    while not os.path.exists(log_path):
        await asyncio.sleep(0.5)
        waited += 0.5
        if waited >= 60:
            await websocket.send_text("[!] Log dosyası bulunamadı, zaman aşımı.")
            await websocket.close()
            return

    # Dosya açıkken satır satır oku, yeni satır gelince gönder
    with open(log_path, "r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                # Yeni satır yok — build devam ediyor mu kontrol et
                await asyncio.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            await websocket.send_text(line)
            # Build bitti sinyalleri
            if "[SUCCESS!]" in line or "[error occurred]" in line.lower():
                await asyncio.sleep(0.5)  # Son satırların iletilmesi için bekle
                break

    await websocket.close()
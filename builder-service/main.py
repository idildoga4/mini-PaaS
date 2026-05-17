from fastapi import FastAPI, BackgroundTasks, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio, os, re, subprocess, requests

from git_manager import clone_repo
from docker_manager import build_and_deploy

app = FastAPI(title="Builder Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEPLOY_SERVICE_URL = os.getenv("DEPLOY_SERVICE_URL", "http://deploy-service:8003")

active_builds = {}

# ─── Models ───────────────────────────────────────────────────
class DeployRequest(BaseModel):
    deploy_id:    int
    repo_url:     str
    project_name: str
    github_token: str    = ""
    # FAZ 4 A.2: kullanıcı bazlı benzersiz container adı ve subdomain
    # deploy-service tarafından hesaplanıp gönderilir.
    # Boş gelirse eski davranışa (app-{project}) geri düşer — geriye uyumluluk.
    container_name: Optional[str] = ""
    subdomain:      Optional[str] = ""

    def validate_repo_url(self):
        url = self.repo_url.strip()
        if not url:
            raise ValueError("repo_url boş olamaz")
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("git@")):
            raise ValueError(f"Geçersiz repo URL'si: '{url}'")
        return url

# ─── Pipeline ─────────────────────────────────────────────────
def run_pipeline(deploy_id: int, repo_url: str, project_name: str,
                 github_token: str = "", container_name: str = "", subdomain: str = ""):
    # project_name build için temizle (image adı olarak kullanılacak)
    clean_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')
    clean_name = re.sub(r'-+', '-', clean_name)

    # FAZ 4 A.2: container_name gelmemişse eski davranışa geri dön
    if not container_name:
        container_name = f"app-{clean_name}"
        print(f"[builder] container_name gelmedi, fallback: {container_name}")

    # FAZ 4 A.2: subdomain gelmemişse proje adından türet
    if not subdomain:
        subdomain = clean_name
        print(f"[builder] subdomain gelmedi, fallback: {subdomain}")

    project_path = clone_repo(repo_url, clean_name, github_token)
    status = "Failed"

    if project_path:
        # FAZ 4 A.2: container_name ve subdomain build_and_deploy'a iletiliyor
        success = build_and_deploy(project_path, clean_name, container_name, subdomain)
        if success:
            status = "Running"

    # Webhook: Deploy Service'e sonucu bildir
    try:
        requests.post(
            f"{DEPLOY_SERVICE_URL}/api/webhook",
            json={
                "deploy_id": deploy_id,
                "status":    status,
                "port":      8090,
                "subdomain": f"{subdomain}.localhost"
            },
            timeout=10
        )
        print(f"[builder] Webhook → ID:{deploy_id} | {status} | {subdomain}.localhost")
    except Exception as e:
        print(f"[builder] Webhook hatası: {e}")

# ─── Endpoints ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "builder-service"}

@app.get("/logs/{project_name}")
async def get_logs(project_name: str):
    project_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')
    log_path = f"./workspace/{project_name}.log"
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log dosyası bulunamadı")
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return {"lines": [l.strip() for l in lines if l.strip()]}

@app.post("/deploy")
async def deploy_project(req: DeployRequest, background_tasks: BackgroundTasks):
    try:
        req.validate_repo_url()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    normalized_project = req.project_name.lower().strip()

    if normalized_project in active_builds:
        raise HTTPException(
            status_code=409,
            detail="Build already in progress"
        )
    
    active_builds[normalized_project] = True

    async def pipeline_wrapper():
        try:
            run_pipeline(
                req.deploy_id,
                req.repo_url,
                req.project_name,
                req.github_token,
                req.container_name or "",
                req.subdomain or "",
            )
        finally:
            active_builds.pop(normalized_project, None)
            print(f"[builder] Build lock kaldırıldı: {normalized_project}")

    background_tasks.add_task(pipeline_wrapper)
    
    return {
        "message":        f"{req.project_name} için deployment başlatıldı",
        "deploy_id":      req.deploy_id,
        "logs_websocket": f"/ws/{req.project_name}"
    }

@app.websocket("/ws/{project_name}")
async def websocket_endpoint(websocket: WebSocket, project_name: str):
    await websocket.accept()
    log_path = f"./workspace/{project_name.lower()}.log"

    waited = 0
    while not os.path.exists(log_path):
        await asyncio.sleep(0.5)
        waited += 0.5
        if waited >= 60:
            await websocket.send_text("[!] Log dosyası bulunamadı, zaman aşımı.")
            await websocket.close()
            return

    with open(log_path, "r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            await websocket.send_text(line)
            if "[SUCCESS!]" in line or "[error occurred]" in line.lower():
                await asyncio.sleep(0.5)
                break

    await websocket.close()

@app.post("/cleanup")
async def cleanup(data: dict):
    container_name = data.get("container_name", "")
    image_name     = data.get("image_name", "")
    if container_name:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    if image_name:
        subprocess.run(["docker", "rmi", "-f", image_name], capture_output=True)
    print(f"[cleanup] {container_name} ve {image_name} silindi")
    return {"message": "Temizlendi"}

@app.post("/stop")
async def stop_container(data: dict):
    container_name = data.get("container_name", "")
    if not container_name:
        raise HTTPException(status_code=400, detail="container_name gerekli")
    result = subprocess.run(["docker", "stop", container_name], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[stop] {container_name} durduruldu")
        return {"message": "Durduruldu"}
    else:
        raise HTTPException(status_code=500, detail=f"Durdurulamadı: {result.stderr}")
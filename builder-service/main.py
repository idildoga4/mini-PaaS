from fastapi import FastAPI, BackgroundTasks, WebSocket 
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import asyncio 
import random
import os
import requests # WEBHOOK İÇİN EKLENDİ
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

# 1. API SÖZLEŞMESİ GÜNCELLENDİ (deploy_id Eklendi)
class DeployRequest(BaseModel):
    deploy_id: int 
    repo_url: str
    project_name: str

# 2. PİPELINE İŞLEMİ BİTİNCE HABER VERME EKLENDİ
def run_pipeline(deploy_id: int, repo_url: str, project_name: str):
    project_path = clone_repo(repo_url, project_name)
    status = "Failed" # Varsayılan olarak hata durumu
    port = random.randint(8100, 9000)
    
    if project_path:
        success = build_and_deploy(project_path, project_name)
        if success:
            status = "Running"
            
    # --- WEBHOOK (GERİ BİLDİRİM) KISMI ---
    # Arkadaşının API'sine kendi iç ağımızdan (paas-net) ulaşıyoruz
    webhook_url = "http://paas_api:8000/api/webhook"
    
    payload = {
        "deploy_id": deploy_id,
        "status": status,
        "port": port
    }
    
    try:
        # Arkadaşının Swagger'da beklediği formata uygun POST isteği atıyoruz
        requests.post(webhook_url, json=payload)
        print(f"[*] Webhook gönderildi. ID: {deploy_id}, Durum: {status}")
    except Exception as e:
        print(f"[-] Webhook gönderilirken hata oluştu: {e}")

@app.post("/deploy")
async def deploy_project(req: DeployRequest, background_tasks: BackgroundTasks):
    # deploy_id de artık fonksiyona aktarılıyor
    background_tasks.add_task(run_pipeline, req.deploy_id, req.repo_url, req.project_name)
    return {"message": f"Deployment started for {req.project_name}! You can watch the logs via WebSocket."}

@app.websocket("/ws/{project_name}")
async def websocket_endpoint(websocket: WebSocket, project_name: str):
    # (Bu kısım önceki mesajdaki İngilizce haliyle tamamen aynı kalacak, dokunmana gerek yok)
    await websocket.accept()
    log_path = f"./workspace/{project_name.lower()}.log"
    while not os.path.exists(log_path):
        await asyncio.sleep(0.5)
    with open(log_path, "r", encoding="utf-8") as f:
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.5)
                continue
            await websocket.send_text(line.strip())
            if "SUCCESS!" in line or "error occurred" in line.lower():
                break
    await websocket.close()
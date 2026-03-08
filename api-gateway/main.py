from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from datetime import datetime
import requests
import os

from database import init_db, get_connection

app = FastAPI(title="Mini PaaS API Gateway")

# Data klasörü garanti
os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class DeployRequest(BaseModel):
    project_name: str
    github_url: str

class WebhookRequest(BaseModel):
    deploy_id: int
    status:    str
    port:      int
    subdomain: str = ""   # "test.localhost" — opsiyonel, eski builder'larla uyumlu

def trigger_builder_service(deploy_id: int, github_url: str, project_name: str):
    """
    Builder Service'e iş talebi gönderir.

    Builder'ın beklediği 3 alan:
      - deploy_id    : Bu deploy'un veritabanındaki ID'si (geri bildirim için)
      - repo_url     : Klonlanacak GitHub adresi
      - project_name : Docker image ve container için kullanılacak isim

    Hata olursa veritabanındaki kaydı "Failed" olarak güncelle,
    sessizce geçme — böylece dashboard'da kullanıcı durumu görebilir.
    """
    try:
        response = requests.post(
            "http://builder-service:5000/deploy",
            json={
                "deploy_id":    deploy_id,
                "repo_url":     github_url,    # Builder "repo_url" istiyor
                "project_name": project_name   # Image ve container ismi için şart
            },
            timeout=10  # Build başlatmak biraz zaman alabilir
        )
        print(f"[API Gateway] Builder'a istek gönderildi. Yanıt: {response.status_code}")
    except Exception as e:
        # Builder'a ulaşılamazsa bunu veritabanına yaz
        print(f"[API Gateway] HATA - Builder servisine ulaşılamadı: {e}")
        conn = get_connection()
        conn.execute(
            "UPDATE deployments SET status='Failed' WHERE id=?",
            (deploy_id,)
        )
        conn.commit()
        conn.close()

@app.post("/api/deploy")
async def deploy(req: DeployRequest, background_tasks: BackgroundTasks):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO deployments (project_name, github_url, status, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        req.project_name,
        req.github_url,
        "Pending",
        datetime.utcnow().isoformat()
    ))

    deploy_id = cursor.lastrowid
    conn.commit()
    conn.close()

    background_tasks.add_task(trigger_builder_service, deploy_id, req.github_url, req.project_name)

    return {"deploy_id": deploy_id, "message": "Deployment başlatıldı"}

@app.post("/api/webhook")
async def webhook(req: WebhookRequest):
    conn = get_connection()
    cursor = conn.cursor()

    result = cursor.execute("""
        UPDATE deployments
        SET status=?, port=?, subdomain=?
        WHERE id=?
    """, (req.status, req.port, req.subdomain, req.deploy_id))

    print(f"[api] Deployment güncellendi → ID:{req.deploy_id} | {req.status} | {req.subdomain}")

    conn.commit()
    conn.close()

    return {"message": "Durum güncellendi"}

@app.get("/api/status/{deploy_id}")
async def status(deploy_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    deployment = cursor.execute(
        "SELECT * FROM deployments WHERE id=?",
        (deploy_id,)
    ).fetchone()

    conn.close()

    if not deployment:
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    return dict(deployment)

app.mount("/", StaticFiles(directory="static", html=True), name="static")

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
    status: str
    port: int

def trigger_builder_service(deploy_id: int, github_url: str):
    try:
        requests.post(
            "http://builder-service:5000/deploy",
            json={
                "deploy_id": deploy_id,
                "github_url": github_url
            },
            timeout=5
        )
    except:
        print("Builder servisine ulaşılamadı")

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

    background_tasks.add_task(trigger_builder_service, deploy_id, req.github_url)

    return {"deploy_id": deploy_id, "message": "Deployment başlatıldı"}

@app.post("/api/webhook")
async def webhook(req: WebhookRequest):
    conn = get_connection()
    cursor = conn.cursor()

    result = cursor.execute("""
        UPDATE deployments
        SET status=?, port=?
        WHERE id=?
    """, (req.status, req.port, req.deploy_id))

    print("UPDATED ROWS:", cursor.rowcount)

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

from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime
import requests as http_requests
import httpx, re, os
from circuit_breaker import verify_token_with_circuit_breaker

from database import init_db, get_connection

AUTH_SERVICE_URL    = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8001")
BUILDER_SERVICE_URL = os.getenv("BUILDER_SERVICE_URL", "http://builder-service:5000")
GITHUB_SERVICE_URL  = os.getenv("GITHUB_SERVICE_URL", "http://github-service:8002")

bearer = HTTPBearer()
app = FastAPI(title="Deploy Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Models ───────────────────────────────────────────────────
class ProjectRequest(BaseModel):
    project_name: str
    github_url:   str

class WebhookRequest(BaseModel):
    deploy_id: int
    status:    str
    port:      int
    subdomain: str = ""

# ─── Auth ─────────────────────────────────────────────────────
# ─── Auth ─────────────────────────────────────────────────────
async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    return await verify_token_with_circuit_breaker(
        credentials.credentials,
        AUTH_SERVICE_URL
    )

# ─── GitHub token al ──────────────────────────────────────────
async def get_github_token(email: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{GITHUB_SERVICE_URL}/api/github/token",
                params={"email": email},
                timeout=5
            )
            if r.status_code == 200:
                return r.json().get("token", "")
    except Exception:
        pass
    return ""

# ─── Builder tetikle ──────────────────────────────────────────
def trigger_builder(deploy_id: int, github_url: str, project_name: str, github_token: str = ""):
    try:
        r = http_requests.post(
            f"{BUILDER_SERVICE_URL}/deploy",
            json={"deploy_id": deploy_id, "repo_url": github_url,
                  "project_name": project_name, "github_token": github_token},
            timeout=10
        )
        print(f"[Deploy Service] Builder: {r.status_code}")
    except Exception as e:
        print(f"[Deploy Service] Builder ulaşılamadı: {e}")
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()

# ─── Yeni deployment başlat ───────────────────────────────────
def start_deployment(conn, email: str, project_name: str, github_url: str) -> int:
    conn.execute(
        "UPDATE deployments SET status='Stopped' WHERE user_email=? AND LOWER(project_name)=LOWER(?) AND status IN ('Running','Pending','Building')",
        (email, project_name)
    )
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO deployments (user_email, project_name, github_url, status, created_at) VALUES (?,?,?,?,?)",
        (email, project_name, github_url, "Pending", datetime.utcnow().isoformat())
    )
    deploy_id = cursor.lastrowid
    conn.commit()
    return deploy_id

# ─── Project endpoints ────────────────────────────────────────
@app.post("/api/projects")
async def create_project(req: ProjectRequest, bg: BackgroundTasks, email: str = Depends(verify_token)):
    """Yeni proje oluştur ve ilk deploy'u başlat."""
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, req.project_name)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="Bu proje adı zaten kullanılıyor")

    conn.execute(
        "INSERT INTO projects (user_email, project_name, github_url, created_at) VALUES (?,?,?,?)",
        (email, req.project_name, req.github_url, datetime.utcnow().isoformat())
    )
    deploy_id = start_deployment(conn, email, req.project_name, req.github_url)
    conn.close()

    github_token = await get_github_token(email)
    bg.add_task(trigger_builder, deploy_id, req.github_url, req.project_name, github_token)
    return {"deploy_id": deploy_id, "message": "Proje oluşturuldu, deployment başlatıldı"}

@app.get("/api/projects")
async def list_projects(email: str = Depends(verify_token)):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM projects WHERE user_email=? ORDER BY id DESC", (email,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/projects/{project_name}/redeploy")
async def redeploy(project_name: str, bg: BackgroundTasks, email: str = Depends(verify_token)):
    """Mevcut projeyi yeniden deploy et."""
    conn = get_connection()
    proj = conn.execute(
        "SELECT * FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    deploy_id = start_deployment(conn, email, proj["project_name"], proj["github_url"])
    conn.close()

    github_token = await get_github_token(email)
    bg.add_task(trigger_builder, deploy_id, proj["github_url"], proj["project_name"], github_token)
    return {"deploy_id": deploy_id, "message": "Redeploy başlatıldı"}

@app.delete("/api/projects/{project_name}")
async def delete_project(project_name: str, email: str = Depends(verify_token)):
    """Projeyi ve tüm deployment'larını sil."""
    conn = get_connection()
    proj = conn.execute(
        "SELECT * FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    clean_name     = re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')
    container_name = f"app-{clean_name}"
    image_name     = f"{clean_name}-img"

    conn.execute("DELETE FROM deployments WHERE user_email=? AND LOWER(project_name)=LOWER(?)", (email, project_name))
    conn.execute("DELETE FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)", (email, project_name))
    conn.commit()
    conn.close()

    try:
        http_requests.post(
            f"{BUILDER_SERVICE_URL}/cleanup",
            json={"container_name": container_name, "image_name": image_name},
            timeout=10
        )
    except Exception as e:
        print(f"[Deploy Service] Cleanup hatası: {e}")

    return {"message": "Proje silindi"}

# ─── Deployment endpoints ─────────────────────────────────────
@app.post("/api/webhook")
async def webhook(req: WebhookRequest):
    conn = get_connection()
    conn.execute(
        "UPDATE deployments SET status=?, port=?, subdomain=? WHERE id=?",
        (req.status, req.port, req.subdomain, req.deploy_id)
    )
    conn.commit()
    conn.close()
    print(f"[Deploy Service] Deploy #{req.deploy_id} → {req.status}")
    return {"message": "Güncellendi"}

@app.get("/api/status/{deploy_id}")
async def status(deploy_id: int, email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deployments WHERE id=? AND user_email=?", (deploy_id, email)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Bulunamadı")
    return dict(row)

@app.get("/api/deployments")
async def list_deployments(email: str = Depends(verify_token)):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM deployments WHERE user_email=? ORDER BY id DESC", (email,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/deployments/{deploy_id}")
async def delete_deployment(deploy_id: int, email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deployments WHERE id=? AND user_email=?", (deploy_id, email)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bulunamadı")

    clean_name     = re.sub(r'[^a-z0-9-]', '-', row["project_name"].lower().strip()).strip('-')
    container_name = f"app-{clean_name}"
    image_name     = f"{clean_name}-img"

    other = conn.execute(
        "SELECT id FROM deployments WHERE user_email=? AND project_name=? AND id!=? AND status='Running'",
        (email, row["project_name"], deploy_id)
    ).fetchone()

    conn.execute("DELETE FROM deployments WHERE id=?", (deploy_id,))
    conn.commit()
    conn.close()

    if not other:
        try:
            http_requests.post(
                f"{BUILDER_SERVICE_URL}/cleanup",
                json={"container_name": container_name, "image_name": image_name},
                timeout=10
            )
        except Exception as e:
            print(f"[Deploy Service] Cleanup hatası: {e}")

    return {"message": "Silindi"}

@app.post("/api/deployments/{deploy_id}/stop")
async def stop_deployment(deploy_id: int, email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deployments WHERE id=? AND user_email=?", (deploy_id, email)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bulunamadı")
    if row["status"] != "Running":
        conn.close()
        raise HTTPException(status_code=400, detail="Sadece çalışan deployment durdurulabilir")

    clean_name     = re.sub(r'[^a-z0-9-]', '-', row["project_name"].lower().strip()).strip('-')
    container_name = f"app-{clean_name}"

    try:
        http_requests.post(f"{BUILDER_SERVICE_URL}/stop", json={"container_name": container_name}, timeout=15)
    except Exception as e:
        print(f"[Deploy Service] Stop hatası: {e}")

    conn.execute("UPDATE deployments SET status='Stopped' WHERE id=?", (deploy_id,))
    conn.commit()
    conn.close()
    return {"message": "Durduruldu"}

# ─── Internal endpoints ───────────────────────────────────────
@app.get("/api/internal/latest-deployment")
async def latest_deployment(repo_name: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deployments WHERE github_url LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{repo_name}%",)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment bulunamadı")
    return dict(row)

@app.post("/api/internal/deploy")
async def internal_deploy(request: Request, bg: BackgroundTasks):
    data         = await request.json()
    user_email   = data.get("user_email", "")
    project_name = data.get("project_name", "")
    github_url   = data.get("github_url", "")
    github_token = data.get("github_token", "")

    conn = get_connection()
    deploy_id = start_deployment(conn, user_email, project_name, github_url)
    conn.close()

    bg.add_task(trigger_builder, deploy_id, github_url, project_name, github_token)
    return {"message": "Redeploy başlatıldı", "deploy_id": deploy_id}

# ─── Health ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "service": "deploy-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
# Deploy Service

import uuid
import contextvars
import logging
import time
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime
from jose import JWTError, jwt          # FAZ 4 A.3 — local doğrulama
import requests as http_requests
import httpx, re, os, asyncio
from secrets_helper import get_secret   # FAZ 4 A.3
from prometheus_client import Counter, Histogram, make_asgi_app
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

# --- LOGGING & TRACE ID KURULUMU ---
trace_id_var = contextvars.ContextVar("trace_id", default='no-trace')

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get()
        return True

logger = logging.getLogger()
logger.addFilter(TraceIdFilter())
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s %(service_name)s %(trace_id)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
service_logger = logging.LoggerAdapter(logger, extra={"service_name": "deploy-service"})
from database import init_db, get_connection, upsert_project  # FAZ 4 A.1

AUTH_SERVICE_URL    = os.getenv("AUTH_SERVICE_URL",    "http://auth-service:8001")
BUILDER_SERVICE_URL = os.getenv("BUILDER_SERVICE_URL", "http://builder-service:5000")
GITHUB_SERVICE_URL  = os.getenv("GITHUB_SERVICE_URL",  "http://github-service:8002")

SECRET_KEY = get_secret("jwt_secret", "JWT_SECRET")
ALGORITHM  = "HS256"

bearer = HTTPBearer()

# --- PROMETHEUS METRİKLERİ ---
deploy_total = Counter('deploy_total', 'Deploy sayisi', ['status'])

DEPLOY_DURATION = Histogram(
    'deploy_duration_seconds',
    'Time spent during the deployment trigger process',
    ['project_name', 'status']
)

HTTP_REQUEST_DURATION = Histogram(
    'http_request_duration_seconds',
    'HTTP request duration in seconds',
    ['method', 'endpoint', 'status_code']
)

deploy_error_total = Counter(
    'deploy_error_total',
    'Total number of deployment errors by type',
    ['error_type', 'project_name']
)
# -----------------------------

app = FastAPI(title="Deploy Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- ORTAK MİDDLEWARE (Trace ID ve HTTP Metrikleri) ---
@app.middleware("http")
async def main_middleware(request: Request, call_next):
    # 1. Trace ID Ayarla
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4())[:8])
    token = trace_id_var.set(trace_id)
    
    # 2. Metrik İçin Süreyi Başlat
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    
    # 3. URL'leri Temizle (ID'leri gruplayarak PromQL'in şişmesini engelleriz)
    path = request.url.path
    path = re.sub(r'/[0-9]+', '/{id}', path)
    path = re.sub(r'/projects/[^/]+(/redeploy)?', r'/projects/{project_name}\1', path)
    
    # 4. Metriği Kaydet
    HTTP_REQUEST_DURATION.labels(
        method=request.method,
        endpoint=path,
        status_code=response.status_code
    ).observe(duration)
    
    # 5. Temizlik ve Dönüş
    response.headers["X-Trace-Id"] = trace_id
    trace_id_var.reset(token)
    return response

# ─── FAZ 4 B.2 — Orphan container health check ─────────────────────────────
@app.on_event("startup")
async def orphan_container_check():
    await asyncio.sleep(3)

    print("[startup-check] Running deployment container kontrolü başlıyor")

    conn = get_connection()

    rows = conn.execute(
        """
        SELECT id, container_name
        FROM deployments
        WHERE status='Running'
        """
    ).fetchall()

    for row in rows:
        deployment_id  = row["id"]
        container_name = row["container_name"]

        if not container_name:
            print(f"[startup-check] container_name NULL, deployment {deployment_id} Failed yapiliyor")
            conn.execute(
                "UPDATE deployments SET status='Failed', error_message='container_name bilinmiyor (eski kayit)' WHERE id=?",
                (deployment_id,)
            )
            continue

        print(f"[startup-check] Kontrol ediliyor: {container_name}")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{BUILDER_SERVICE_URL}/container-status",
                    params={"container_name": container_name}
                )
            if resp.status_code == 200:
                container_running = resp.json().get("running", False)
            else:
                print(f"[startup-check] Builder-service hata dondu ({resp.status_code}), atliyorum")
                continue
        except Exception as e:
            print(f"[startup-check] Builder-service'e ulasilamadi: {e}, atliyorum")
            continue

        if not container_running:
            print(f"[startup-check] Container bulunamadi: {container_name}")
            conn.execute(
                "UPDATE deployments SET status='Failed', error_message='Container not found after restart' WHERE id=?",
                (deployment_id,)
            )

    conn.commit()
    conn.close()
    print("[startup-check] Tamamlandi")

# ─── Models ───────────────────────────────────────────────────
class ProjectRequest(BaseModel):
    project_name: str
    github_url:   str

class WebhookRequest(BaseModel):
    deploy_id: int
    status:    str
    port:      int
    subdomain: str = ""

# ─── FAZ 4 A.3: Local JWT doğrulama ──────────────────────────────────────────
async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Geçersiz token")
        return email
    except JWTError:
        raise HTTPException(status_code=401, detail="Token geçersiz veya süresi dolmuş")

# ─── FAZ 4 A.2: Container adı üretici ───────────────────────────────────────
def compute_container_name(user_email: str, project_name: str) -> str:
    email_prefix = user_email.split("@")[0].lower().replace(".", "_")
    email_prefix = re.sub(r"[^a-z0-9_]", "_", email_prefix)
    clean_project = re.sub(r"[^a-z0-9-]", "-", project_name.lower().strip()).strip("-")
    clean_project = re.sub(r"-+", "-", clean_project)
    return f"{email_prefix}_{clean_project}"

def compute_subdomain(user_email: str, project_name: str) -> str:
    email_prefix = user_email.split("@")[0].lower().replace(".", "-")
    email_prefix = re.sub(r"[^a-z0-9-]", "-", email_prefix).strip("-")
    clean_project = re.sub(r"[^a-z0-9-]", "-", project_name.lower().strip()).strip("-")
    clean_project = re.sub(r"-+", "-", clean_project)
    return f"{email_prefix}-{clean_project}"

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
def trigger_builder(deploy_id: int, github_url: str, project_name: str,
                    github_token: str = "", container_name: str = "", subdomain: str = ""):
    deploy_total.labels(status='running').inc()
    start_time = time.time()
    status_label = "success"
    
    try:
        r = http_requests.post(
            f"{BUILDER_SERVICE_URL}/deploy",
            json={
                "deploy_id":      deploy_id,
                "repo_url":       github_url,
                "project_name":   project_name,
                "github_token":   github_token,
                "container_name": container_name,
                "subdomain":      subdomain,
            },
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=10
        )
        print(f"[Deploy Service] Builder: {r.status_code}")

        if r.status_code == 409:
            conn = get_connection()
            conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
            conn.commit()
            conn.close()
            deploy_error_total.labels(error_type='parallel_build_conflict', project_name=project_name).inc()
            status_label = "failed"
            print("[Deploy Service] Paralel build engellendi")
            return
           
        service_logger.info(f"Builder tetiklendi, statüs: {r.status_code}")
        
    except http_requests.Timeout:
        service_logger.error("[Deploy Service] Builder zaman aşımı")
        deploy_error_total.labels(error_type='timeout', project_name=project_name).inc()
        status_label = "failed"
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()
    except http_requests.ConnectionError:
        service_logger.error("[Deploy Service] Builder bağlantı hatası")
        deploy_error_total.labels(error_type='connection_error', project_name=project_name).inc()
        status_label = "failed"
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        service_logger.error(f"[Deploy Service] Builder ulaşılamadı: {e}")
        deploy_error_total.labels(error_type='unknown', project_name=project_name).inc()
        status_label = "failed"
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()
    finally:
        duration = time.time() - start_time
        DEPLOY_DURATION.labels(project_name=project_name, status=status_label).observe(duration)

# ─── Yeni deployment başlat ───────────────────────────────────
def start_deployment(conn, email: str, project_name: str, github_url: str,
                     container_name: str = "") -> int:
    conn.execute(
        """
        UPDATE deployments SET status='Stopped'
        WHERE user_email=? AND LOWER(project_name)=LOWER(?)
          AND status IN ('Running','Pending','Building')
        """,
        (email, project_name)
    )
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO deployments
            (user_email, project_name, github_url, status, container_name, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (email, project_name, github_url, "Pending", container_name,
         datetime.utcnow().isoformat())
    )
    deploy_id = cursor.lastrowid
    conn.commit()
    return deploy_id

# ─── Project endpoints ────────────────────────────────────────
@app.post("/api/projects")
async def create_project(req: ProjectRequest, bg: BackgroundTasks,
                         email: str = Depends(verify_token)):
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

    container_name = compute_container_name(email, req.project_name)
    subdomain      = compute_subdomain(email, req.project_name)

    deploy_id = start_deployment(conn, email, req.project_name, req.github_url, container_name)
    conn.close()

    github_token = await get_github_token(email)
    bg.add_task(trigger_builder, deploy_id, req.github_url, req.project_name,
                github_token, container_name, subdomain)
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
async def redeploy(project_name: str, bg: BackgroundTasks,
                   email: str = Depends(verify_token)):
    conn = get_connection()
    proj = conn.execute(
        "SELECT * FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    container_name = compute_container_name(email, proj["project_name"])
    subdomain      = compute_subdomain(email, proj["project_name"])

    deploy_id = start_deployment(conn, email, proj["project_name"], proj["github_url"],
                                 container_name)
    conn.close()

    github_token = await get_github_token(email)
    bg.add_task(trigger_builder, deploy_id, proj["github_url"], proj["project_name"],
                github_token, container_name, subdomain)
    return {"deploy_id": deploy_id, "message": "Redeploy başlatıldı"}

@app.put("/api/projects/{project_name}")
async def update_project(project_name: str, req: dict, email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadi")
    new_name   = req.get("project_name", project_name).strip()
    new_url    = req.get("github_url", "").strip()
    if not new_name or not new_url:
        conn.close()
        raise HTTPException(status_code=422, detail="project_name ve github_url zorunlu")
    conn.execute(
        "UPDATE projects SET project_name=?, github_url=? WHERE id=?",
        (new_name, new_url, row["id"])
    )
    conn.commit()
    conn.close()
    service_logger.info(f"[Deploy Service] Proje guncellendi: {project_name} -> {new_name}")
    return {"message": "Proje guncellendi", "project_name": new_name, "github_url": new_url}

@app.delete("/api/projects/{project_name}")
async def delete_project(project_name: str, email: str = Depends(verify_token)):
    conn = get_connection()
    proj = conn.execute(
        "SELECT * FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    running = conn.execute(
        """
        SELECT container_name FROM deployments
        WHERE user_email=? AND LOWER(project_name)=LOWER(?) AND status='Running'
        ORDER BY id DESC LIMIT 1
        """,
        (email, project_name)
    ).fetchone()

    container_name = (running["container_name"] if running and running["container_name"]
                      else compute_container_name(email, project_name))
    image_name     = f"{re.sub(r'[^a-z0-9-]', '-', project_name.lower().strip()).strip('-')}-img"

    conn.execute(
        "DELETE FROM deployments WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    )
    conn.execute(
        "DELETE FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    )
    conn.commit()
    conn.close()

    try:
        http_requests.post(
            f"{BUILDER_SERVICE_URL}/cleanup",
            json={"container_name": container_name, "image_name": image_name},
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=10
        )
    except Exception as e:
        service_logger.error(f"Builder ulaşılamadı: {e}")

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
    
    if req.status == "Running":
        deploy_total.labels(status='success').inc()
    else:
        deploy_total.labels(status='failed').inc()
        
        # --- EKLENEN YENİ KISIM BAŞLANGICI ---
        # Subdomain "projem.localhost" formatında geldiği için ilk kısmını alıp proje adını buluyoruz
        project_name = req.subdomain.split('.')[0] if req.subdomain else "bilinmeyen"
        
        # Grafana'daki "Hata Kırılımı (Deploy Error)" panelini dolduracak asıl tetikleyici
        deploy_error_total.labels(error_type="healthcheck_failed", project_name=project_name).inc()
        # --- EKLENEN YENİ KISIM BİTİŞİ ---
        
    service_logger.info(f"[Deploy Service] Deploy #{req.deploy_id} → {req.status}")
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

    container_name = (row["container_name"] if row["container_name"]
                      else compute_container_name(email, row["project_name"]))
    image_name     = f"{re.sub(r'[^a-z0-9-]', '-', row['project_name'].lower().strip()).strip('-')}-img"

    other = conn.execute(
        """
        SELECT id FROM deployments
        WHERE user_email=? AND project_name=? AND id!=? AND status='Running'
        """,
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
                headers={"X-Trace-Id": trace_id_var.get()},
                timeout=10
            )
        except Exception as e:
            service_logger.error(f"[Deploy Service] Cleanup hatası: {e}")

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

    container_name = (row["container_name"] if row["container_name"]
                      else compute_container_name(email, row["project_name"]))

    try:
        http_requests.post(
            f"{BUILDER_SERVICE_URL}/stop",
            json={"container_name": container_name},
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=15
        )
    except Exception as e:
        service_logger.error(f"[Deploy Service] Stop hatası: {e}")

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

    if not project_name:
        raise HTTPException(status_code=422, detail="project_name zorunlu")

    conn = get_connection()

    if github_url and user_email:
        upsert_project(conn, user_email, project_name, github_url)
    else:
        service_logger.info(f"[Deploy Service] internal/deploy: upsert atlandı "
                            f"(user_email={user_email!r}, github_url={github_url!r})")

    container_name = compute_container_name(user_email or "anon", project_name)
    subdomain      = compute_subdomain(user_email or "anon", project_name)

    deploy_id = start_deployment(conn, user_email, project_name, github_url, container_name)
    conn.close()

    bg.add_task(trigger_builder, deploy_id, github_url, project_name,
                github_token, container_name, subdomain)
    return {"message": "Redeploy başlatıldı", "deploy_id": deploy_id}

# ─── Health & Metrics ──────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "service": "deploy-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/metrics")
async def metrics():
    """Prometheus kazıma işlemlerinin yönlendirmeye takılmadan direkt 200 OK dönmesini sağlar."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
import uuid
import contextvars
import logging
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime
from jose import JWTError, jwt          # FAZ 4 A.3 — local doğrulama
import requests as http_requests
import httpx, re, os
from secrets_helper import get_secret   # FAZ 4 A.3

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
app = FastAPI(title="Deploy Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4())[:8])
    token = trace_id_var.set(trace_id)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    trace_id_var.reset(token)
    return response

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
# Auth Service down olsa bile tüm API endpoint'leri çalışmaya devam eder.
# circuit_breaker.py'ye olan bağımlılık kaldırıldı.
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
    """
    Kullanıcı bazlı benzersiz container adı üretir.

    Örnekler:
        omertank36@gmail.com + testapp   → omertank36_testapp
        ali.veli@company.com + api       → ali_veli_api

    Böylece farklı kullanıcılar aynı proje adını kullanabilir,
    container çakışması olmaz.
    """
    email_prefix = user_email.split("@")[0].lower().replace(".", "_")
    # Docker isimlendirme kuralı: [a-zA-Z0-9][a-zA-Z0-9_.-]
    email_prefix = re.sub(r"[^a-z0-9_]", "_", email_prefix)
    clean_project = re.sub(r"[^a-z0-9-]", "-", project_name.lower().strip()).strip("-")
    clean_project = re.sub(r"-+", "-", clean_project)
    return f"{email_prefix}_{clean_project}"

def compute_subdomain(user_email: str, project_name: str) -> str:
    """
    Traefik subdomain adı üretir.

    Örnekler:
        omertank36@gmail.com + testapp  → omertank36-testapp
    """
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
# FAZ 4 A.2: container_name parametresi eklendi
def trigger_builder(deploy_id: int, github_url: str, project_name: str,
                    github_token: str = "", container_name: str = "", subdomain: str = ""):
    try:
        r = http_requests.post(
            f"{BUILDER_SERVICE_URL}/deploy",
            json={
                "deploy_id":      deploy_id,
                "repo_url":       github_url,
                "project_name":   project_name,
                "github_token":   github_token,
                "container_name": container_name,   # FAZ 4 A.2
                "subdomain":      subdomain,        # FAZ 4 A.2
            },
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=10
        )
        service_logger.info(f"Builder tetiklendi, statüs: {r.status_code}")
    except Exception as e:
        service_logger.error(f"[Deploy Service] Builder ulaşılamadı: {e}")
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()

# ─── Yeni deployment başlat ───────────────────────────────────
# FAZ 4 A.2: container_name parametresi eklendi, veritabanına yazılıyor
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

    # FAZ 4 A.2: kullanıcı-proje bazlı container adı
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
    """Mevcut projeyi yeniden deploy et."""
    conn = get_connection()
    proj = conn.execute(
        "SELECT * FROM projects WHERE user_email=? AND LOWER(project_name)=LOWER(?)",
        (email, project_name)
    ).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(status_code=404, detail="Proje bulunamadı")

    # FAZ 4 A.2: container adını yeniden hesapla (tutarlılık için)
    container_name = compute_container_name(email, proj["project_name"])
    subdomain      = compute_subdomain(email, proj["project_name"])

    deploy_id = start_deployment(conn, email, proj["project_name"], proj["github_url"],
                                 container_name)
    conn.close()

    github_token = await get_github_token(email)
    bg.add_task(trigger_builder, deploy_id, proj["github_url"], proj["project_name"],
                github_token, container_name, subdomain)
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

    # FAZ 4 A.2: container adını veritabanından al, yoksa hesapla
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

    # FAZ 4 A.2: veritabanındaki container_name'i kullan
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

    # FAZ 4 A.2: veritabanındaki container_name'i kullan
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

# ─── FAZ 4 A.1: internal/deploy — upsert fix ─────────────────────────────────
@app.post("/api/internal/deploy")
async def internal_deploy(request: Request, bg: BackgroundTasks):
    """
    GitHub push webhook'undan tetiklenen otomatik deploy endpoint'i.

    FAZ 4 A.1 değişikliği:
        Endpoint başında projects tablosuna upsert yapılır.
        Bu sayede push-to-deploy ile gelen deploy'lar Projects sayfasında görünür.

    FAZ 4 A.2 değişikliği:
        container_name ve subdomain artık user_email + project_name kombinasyonundan
        hesaplanır; builder'a payload ile iletilir.
    """
    data         = await request.json()
    user_email   = data.get("user_email", "")
    project_name = data.get("project_name", "")
    github_url   = data.get("github_url", "")
    github_token = data.get("github_token", "")

    if not project_name:
        raise HTTPException(status_code=422, detail="project_name zorunlu")

    conn = get_connection()

    # ── A.1: Projects tablosuna upsert ───────────────────────────────────────
    # Push-to-deploy ile gelen deploy'lar artık Projects sayfasında görünür.
    if github_url and user_email:
        upsert_project(conn, user_email, project_name, github_url)
    else:
        service_logger.info(f"[Deploy Service] internal/deploy: upsert atlandı "
                            f"(user_email={user_email!r}, github_url={github_url!r})")

    # ── A.2: Kullanıcı bazlı container adı hesapla ───────────────────────────
    container_name = compute_container_name(user_email or "anon", project_name)
    subdomain      = compute_subdomain(user_email or "anon", project_name)

    deploy_id = start_deployment(conn, user_email, project_name, github_url, container_name)
    conn.close()

    bg.add_task(trigger_builder, deploy_id, github_url, project_name,
                github_token, container_name, subdomain)
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
# GitHub Service
# FAZ 7: SQLite → PostgreSQL geçişi
#   - conn.execute() → c = conn.cursor(); c.execute() pattern'ine geçildi
#   - ? → %s placeholder
#   - ON CONFLICT(email) DO UPDATE SET → PostgreSQL syntax'ına uyarlandı
#   - ON CONFLICT DO NOTHING → PostgreSQL'de aynı, korundu

import uuid
import contextvars
import logging
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt           # FAZ 4 A.3 — local doğrulama
import hashlib, hmac, httpx, os
from secrets_helper import get_secret
from database import init_db, get_connection
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

trace_id_var = contextvars.ContextVar("trace_id", default='no-trace')

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get()
        return True

logger = logging.getLogger()
logger.addFilter(TraceIdFilter())
from pythonjsonlogger import jsonlogger
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s %(service_name)s %(trace_id)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
service_logger = logging.LoggerAdapter(logger, extra={"service_name": "github-service"})

SECRET_KEY           = get_secret("jwt_secret",            "JWT_SECRET")
ALGORITHM            = "HS256"
GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = get_secret("github_client_secret",  "GITHUB_CLIENT_SECRET")
WEBHOOK_SECRET       = get_secret("webhook_secret",        "WEBHOOK_SECRET")
DEPLOY_SERVICE_URL   = os.getenv("DEPLOY_SERVICE_URL", "http://deploy-service:8003")

bearer = HTTPBearer()
app = FastAPI(title="GitHub Service")

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

# ─── Endpoints ────────────────────────────────────────────────
@app.get("/api/github/login")
async def github_login(email: str = Depends(verify_token)):
    state = jwt.encode(
        {"sub": email, "exp": datetime.utcnow() + timedelta(minutes=10)},
        SECRET_KEY, algorithm=ALGORITHM
    )
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&scope=repo"
        f"&state={state}"
    )
    return {"redirect_url": url}

@app.get("/api/github/callback")
async def github_callback(code: str, state: str):
    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=400, detail="Geçersiz state")
    except JWTError:
        raise HTTPException(status_code=400, detail="State süresi dolmuş veya geçersiz.")

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code}
        )
        token_data = res.json()

    github_token = token_data.get("access_token")
    if not github_token:
        raise HTTPException(status_code=400, detail="GitHub token alınamadı")

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO github_tokens (email, github_token, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (email) DO UPDATE
        SET github_token = EXCLUDED.github_token,
            updated_at   = EXCLUDED.updated_at
        """,
        (email, github_token, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    service_logger.info(f"[GitHub Service] OAuth bağlandı → {email}")
    return RedirectResponse(url="http://localhost:8090/?github=connected")

@app.get("/api/github/status")
async def github_status(email: str = Depends(verify_token)):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT github_token FROM github_tokens WHERE email=%s", (email,))
    row = c.fetchone()
    conn.close()
    return {"connected": bool(row and row["github_token"])}

@app.delete("/api/github/disconnect")
async def github_disconnect(email: str = Depends(verify_token)):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE github_tokens SET github_token=NULL WHERE email=%s", (email,))
    conn.commit()
    conn.close()
    service_logger.info(f"[GitHub Service] GitHub bağlantısı kesildi → {email}")
    return {"message": "GitHub bağlantısı kesildi"}

@app.get("/api/github/token")
async def get_github_token(email: str):
    """Deploy Service'in kullanıcı token'ını sorgulaması için internal endpoint."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT github_token FROM github_tokens WHERE email=%s", (email,))
    row = c.fetchone()
    conn.close()
    token = (row["github_token"] or "") if row else ""
    return {"token": token}

@app.post("/api/github/webhook")
async def github_webhook(request: Request):
    signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    mac = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Geçersiz webhook signature")

    payload = await request.json()
    if "commits" not in payload:
        return {"message": "Push değil, atlandı"}

    repo_url  = payload["repository"]["clone_url"]
    repo_name = payload["repository"]["name"]
    pusher    = payload["pusher"]["name"]
    service_logger.info(f"[GitHub Service] Push alındı → {pusher} → {repo_url}",
                        extra={"trace_id": trace_id_var.get()})

    import requests as http_requests
    try:
        r = http_requests.get(
            f"{DEPLOY_SERVICE_URL}/api/internal/latest-deployment",
            params={"repo_name": repo_name},
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=5
        )
        if r.status_code != 200:
            return {"message": "Bu repo için deployment bulunamadı"}
        data = r.json()
        user_email   = data["user_email"]
        project_name = data["project_name"]
    except Exception as e:
        service_logger.error(f"[GitHub Service] Deploy Service sorgu hatası: {e}")
        return {"message": "Deploy Service ulaşılamadı"}

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT github_token FROM github_tokens WHERE email=%s", (user_email,)
    )
    token_row = c.fetchone()
    github_token = (token_row["github_token"] or "") if token_row else ""
    conn.close()

    try:
        r = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/api/internal/deploy",
            json={
                "user_email":   user_email,
                "project_name": project_name,
                "github_url":   repo_url,
                "github_token": github_token
            },
            headers={"X-Trace-Id": trace_id_var.get()},
            timeout=10
        )
        return {"message": "Redeploy başlatıldı", "status": r.status_code}
    except Exception as e:
        service_logger.error(f"[GitHub Service] Deploy Service hatası: {e}")
        return {"message": "Deploy Service ulaşılamadı"}

@app.post("/api/github/register-repo")
async def register_repo(request: Request, email: str = Depends(verify_token)):
    """Deploy sonrası repo → kullanıcı eşlemesini kaydet."""
    data = await request.json()
    repo_name    = data.get("repo_name", "")
    project_name = data.get("project_name", "")
    if not repo_name or not project_name:
        raise HTTPException(status_code=400, detail="repo_name ve project_name gerekli")
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO repo_mappings (repo_name, user_email, project_name, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (repo_name, email, project_name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return {"message": "Repo mapping kaydedildi"}

# FAZ 5 B: Prometheus metrics endpoint
@app.get('/metrics', include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
async def health():
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "service": "github-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
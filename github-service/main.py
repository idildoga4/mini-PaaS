from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
import hashlib, hmac, httpx, os

from database import init_db, get_connection

SECRET_KEY            = os.getenv("JWT_SECRET", "mini-paas-secret-2025-xK9")
ALGORITHM             = "HS256"
GITHUB_CLIENT_ID      = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET  = os.getenv("GITHUB_CLIENT_SECRET")
WEBHOOK_SECRET        = os.getenv("WEBHOOK_SECRET", "minipaas2025secret")
DEPLOY_SERVICE_URL    = os.getenv("DEPLOY_SERVICE_URL", "http://deploy-service:8003")
AUTH_SERVICE_URL      = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8001")

bearer = HTTPBearer()
app = FastAPI(title="GitHub Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Auth (Auth Service'e sor) ────────────────────────────────
async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{AUTH_SERVICE_URL}/api/auth/verify",
                headers={"Authorization": f"Bearer {credentials.credentials}"},
                timeout=5
            )
            if r.status_code == 200:
                return r.json()["email"]
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="Token geçersiz")

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
    conn.execute("""
        INSERT INTO github_tokens (email, github_token, updated_at)
        VALUES (?,?,?)
        ON CONFLICT(email) DO UPDATE SET github_token=excluded.github_token, updated_at=excluded.updated_at
    """, (email, github_token, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    print(f"[GitHub Service] OAuth bağlandı → {email}")
    return RedirectResponse(url="http://localhost:8090/?github=connected")

@app.get("/api/github/status")
async def github_status(email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute("SELECT github_token FROM github_tokens WHERE email=?", (email,)).fetchone()
    conn.close()
    return {"connected": bool(row and row["github_token"])}

@app.delete("/api/github/disconnect")
async def github_disconnect(email: str = Depends(verify_token)):
    conn = get_connection()
    conn.execute("UPDATE github_tokens SET github_token=NULL WHERE email=?", (email,))
    conn.commit()
    conn.close()
    return {"message": "GitHub bağlantısı kesildi"}

@app.get("/api/github/token")
async def get_github_token(email: str):
    """Deploy Service'in kullanıcı token'ını sorgulaması için internal endpoint."""
    conn = get_connection()
    row = conn.execute("SELECT github_token FROM github_tokens WHERE email=?", (email,)).fetchone()
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
    print(f"[GitHub Service] Push alındı → {pusher} → {repo_url}")

    # Deploy Service'ten bu repo'ya ait son deployment'ı bul
    import requests as http_requests
    try:
        r = http_requests.get(
            f"{DEPLOY_SERVICE_URL}/api/internal/latest-deployment",
            params={"repo_name": repo_name},
            timeout=5
        )
        if r.status_code != 200:
            return {"message": "Bu repo için deployment bulunamadı"}
        data = r.json()
        user_email   = data["user_email"]
        project_name = data["project_name"]
    except Exception as e:
        print(f"[GitHub Service] Deploy Service sorgu hatası: {e}")
        return {"message": "Deploy Service ulaşılamadı"}

    # GitHub token'ı al
    conn = get_connection()
    token_row = conn.execute(
        "SELECT github_token FROM github_tokens WHERE email=?", (user_email,)
    ).fetchone()
    github_token = (token_row["github_token"] or "") if token_row else ""
    conn.close()

    # Deploy Service'i tetikle
    try:
        r = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/api/internal/deploy",
            json={
                "user_email":   user_email,
                "project_name": project_name,
                "github_url":   repo_url,
                "github_token": github_token
            },
            timeout=10
        )
        return {"message": "Redeploy başlatıldı", "status": r.status_code}
    except Exception as e:
        print(f"[GitHub Service] Deploy Service hatası: {e}")
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
    conn.execute("""
        INSERT INTO repo_mappings (repo_name, user_email, project_name, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT DO NOTHING
    """, (repo_name, email, project_name, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"message": "Repo mapping kaydedildi"}

@app.get("/health")
async def health():
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "service": "github-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

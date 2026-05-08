import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
import hashlib, hmac, secrets, base64, re, os, smtplib
from email.mime.text import MIMEText
import requests as http_requests
from typing import Optional

from database import init_db, get_connection


# ─── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY         = os.getenv("JWT_SECRET", "mini-paas-secret-2025-xK9")
ALGORITHM          = "HS256"
TOKEN_EXPIRE_HOURS = 24
CODE_EXPIRE_MINUTES = 15

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@minipaas.local")

bearer = HTTPBearer()

app = FastAPI(title="Mini PaaS API Gateway")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Models ────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email:    str
    password: str

class VerifyRequest(BaseModel):
    email: str
    code:  str

class LoginRequest(BaseModel):
    email:    str
    password: str

class DeployRequest(BaseModel):
    project_name: str
    github_url:   str

class WebhookRequest(BaseModel):
    deploy_id: int
    status:    str
    port:      int
    subdomain: str = ""

# ─── Password ─────────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h    = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return salt + ":" + base64.b64encode(h).decode()

def check_password(password: str, stored: str) -> bool:
    try:
        salt, b64 = stored.split(":", 1)
        h_new = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
        return hmac.compare_digest(base64.b64decode(b64), h_new)
    except Exception:
        return False

# ─── Validation ───────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')

def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))

def validate_password(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Şifre en az 8 karakter olmalı"
    if not re.search(r'[A-Z]', password):
        return "En az bir büyük harf içermeli (A-Z)"
    if not re.search(r'[a-z]', password):
        return "En az bir küçük harf içermeli (a-z)"
    if not re.search(r'[0-9]', password):
        return "En az bir rakam içermeli (0-9)"
    return None

# ─── Email ─────────────────────────────────────────────────────────────────────
def send_verification_email(to_email: str, code: str):
    subject = f"Mini PaaS — Doğrulama Kodunuz: {code}"
    body    = f"Doğrulama kodunuz: {code}\nBu kod {CODE_EXPIRE_MINUTES} dakika geçerlidir."
    if SMTP_HOST and SMTP_USER:
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"]    = SMTP_FROM
            msg["To"]      = to_email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to_email], msg.as_string())
        except Exception as e:
            print(f"[Email] SMTP hatası: {e}")
    else:
        print(f"\n{'='*40}\n[DEV MODE] Email: {to_email} | Kod: {code}\n{'='*40}\n")

# ─── JWT ───────────────────────────────────────────────────────────────────────
def create_token(email: str) -> str:
    return jwt.encode(
        {"sub": email, "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)},
        SECRET_KEY, algorithm=ALGORITHM
    )

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email   = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Geçersiz token")
        return email
    except JWTError:
        raise HTTPException(status_code=401, detail="Token geçersiz veya süresi dolmuş")

# ─── Auth endpoints ────────────────────────────────────────────────────────────
@app.post("/api/register")
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if not validate_email(email):
        raise HTTPException(status_code=400, detail="Geçerli bir e-posta adresi girin")
    pw_err = validate_password(req.password)
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)
    conn = get_connection()
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Bu e-posta zaten kayıtlı")
    hashed = hash_password(req.password)
    now = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)", (email, hashed, now))
    conn.commit()
    conn.close()
    token = create_token(email)
    return {"token": token, "email": email, "message": "Kayıt başarılı"}

@app.post("/api/verify")
async def verify(req: VerifyRequest):
    email = req.email.lower().strip()
    conn  = get_connection()
    row   = conn.execute("SELECT * FROM pending_verifications WHERE email=?", (email,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bekleyen doğrulama bulunamadı.")
    if datetime.utcnow() > datetime.fromisoformat(row["expires_at"]):
        conn.execute("DELETE FROM pending_verifications WHERE email=?", (email,))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=410, detail="Kodun süresi dolmuş.")
    if row["code"] != req.code.strip():
        conn.close()
        raise HTTPException(status_code=400, detail="Doğrulama kodu hatalı")
    try:
        conn.execute("INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)",
                     (email, row["password"], datetime.utcnow().isoformat()))
    except Exception:
        pass
    conn.execute("DELETE FROM pending_verifications WHERE email=?", (email,))
    conn.commit()
    conn.close()
    token = create_token(email)
    return {"token": token, "email": email, "message": "Hesap doğrulandı!"}

@app.post("/api/login")
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    conn  = get_connection()
    row   = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not row or not check_password(req.password, row["password"]):
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    token = create_token(email)
    return {"token": token, "email": email, "message": "Giriş başarılı"}

@app.get("/api/me")
async def me(email: str = Depends(verify_token)):
    return {"email": email}

# ─── GitHub OAuth ──────────────────────────────────────────────────────────────
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
        error = token_data.get("error_description", "Bilinmeyen hata")
        raise HTTPException(status_code=400, detail=f"GitHub token alınamadı: {error}")
    conn = get_connection()
    conn.execute("UPDATE users SET github_token=? WHERE email=?", (github_token, email))
    conn.commit()
    conn.close()
    print(f"[OAuth] GitHub bağlandı → {email}")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="http://localhost:8090/?github=connected")

@app.get("/api/github/status")
async def github_status(email: str = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute("SELECT github_token FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    connected = bool(row and row["github_token"])
    return {"connected": connected}

@app.delete("/api/github/disconnect")
async def github_disconnect(email: str = Depends(verify_token)):
    conn = get_connection()
    conn.execute("UPDATE users SET github_token=NULL WHERE email=?", (email,))
    conn.commit()
    conn.close()
    return {"message": "GitHub bağlantısı kesildi"}

# ─── Deploy endpoints ──────────────────────────────────────────────────────────
def trigger_builder(deploy_id: int, github_url: str, project_name: str, github_token: str = ""):
    try:
        r = http_requests.post(
            "http://builder-service:5000/deploy",
            json={
                "deploy_id":    deploy_id,
                "repo_url":     github_url,
                "project_name": project_name,
                "github_token": github_token
            },
            timeout=10
        )
        print(f"[API Gateway] Builder: {r.status_code}")
    except Exception as e:
        print(f"[API Gateway] Builder ulaşılamadı: {e}")
        conn = get_connection()
        conn.execute("UPDATE deployments SET status='Failed' WHERE id=?", (deploy_id,))
        conn.commit()
        conn.close()

@app.post("/api/deploy")
async def deploy(req: DeployRequest, bg: BackgroundTasks, email: str = Depends(verify_token)):
    conn   = get_connection()
    user   = conn.execute("SELECT github_token FROM users WHERE email=?", (email,)).fetchone()
    github_token = (user["github_token"] or "") if user else ""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO deployments (user_email, project_name, github_url, status, created_at) VALUES (?,?,?,?,?)",
        (email, req.project_name, req.github_url, "Pending", datetime.utcnow().isoformat())
    )
    deploy_id = cursor.lastrowid
    conn.commit()
    conn.close()
    bg.add_task(trigger_builder, deploy_id, req.github_url, req.project_name, github_token)
    return {"deploy_id": deploy_id, "message": "Deployment başlatıldı"}

# ─── Builder webhook (builder → api-gateway durum bildirimi) ───────────────────
@app.post("/api/webhook")
async def webhook(req: WebhookRequest):
    conn = get_connection()
    conn.execute(
        "UPDATE deployments SET status=?, port=?, subdomain=? WHERE id=?",
        (req.status, req.port, req.subdomain, req.deploy_id)
    )
    conn.commit()
    conn.close()
    print(f"[webhook] Deploy #{req.deploy_id} güncellendi → {req.status}")
    return {"message": "Güncellendi"}

# ─── GitHub push webhook (github → api-gateway push bildirimi) ────────────────
@app.post("/api/github/webhook")
async def github_webhook(request: Request):
    secret = os.getenv("WEBHOOK_SECRET", "minipaas2025secret")
    signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()

    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Geçersiz webhook signature")

    payload = await request.json()

    if "commits" not in payload:
        return {"message": "Push değil, atlandı"}

    repo_url  = payload["repository"]["clone_url"]
    repo_name = payload["repository"]["name"]
    pusher    = payload["pusher"]["name"]

    print(f"[webhook] Push alındı → {pusher} → {repo_url}")

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deployments WHERE github_url LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{repo_name}%",)
    ).fetchone()
    conn.close()

    if not row:
        return {"message": "Bu repo için deployment bulunamadı"}

    conn = get_connection()
    user = conn.execute("SELECT github_token FROM users WHERE email=?", (row["user_email"],)).fetchone()
    github_token = (user["github_token"] or "") if user else ""
    conn.close()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO deployments (user_email, project_name, github_url, status, created_at) VALUES (?,?,?,?,?)",
        (row["user_email"], row["project_name"], repo_url, "Pending", datetime.utcnow().isoformat())
    )
    deploy_id = cursor.lastrowid
    conn.commit()
    conn.close()

    trigger_builder(deploy_id, repo_url, row["project_name"], github_token)

    return {"message": "Redeploy başlatıldı", "deploy_id": deploy_id}

# ─── Status & listing ──────────────────────────────────────────────────────────
@app.get("/api/status/{deploy_id}")
async def status(deploy_id: int, email: str = Depends(verify_token)):
    conn = get_connection()
    row  = conn.execute(
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
    conn.execute("DELETE FROM deployments WHERE id=?", (deploy_id,))
    conn.commit()
    conn.close()
    return {"message": "Silindi"}

# ─── Health & metrics ──────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)}"
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "api-gateway",
        "database": db_status
    }

@app.get("/metrics/summary")
async def metrics_summary(email: str = Depends(verify_token)):
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM deployments WHERE user_email=? GROUP BY status",
        (email,)
    ).fetchall()
    conn.close()
    summary = {r["status"].lower(): r["cnt"] for r in rows}
    return {
        "total":   sum(summary.values()),
        "running": summary.get("running", 0),
        "failed":  summary.get("failed", 0),
        "pending": summary.get("pending", 0) + summary.get("building", 0),
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
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

# SMTP (opsiyonel — boş bırakılırsa konsola yazar)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@minipaas.local")

bearer = HTTPBearer()

app = FastAPI(title="Mini PaaS API Gateway")
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

# ─── Password (stdlib only — no bcrypt/passlib needed) ────────────────────────
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
    """None = geçerli. str = hata mesajı."""
    if len(password) < 8:
        return "Şifre en az 8 karakter olmalı"
    if not re.search(r'[A-Z]', password):
        return "En az bir büyük harf içermeli (A-Z)"
    if not re.search(r'[a-z]', password):
        return "En az bir küçük harf içermeli (a-z)"
    if not re.search(r'[0-9]', password):
        return "En az bir rakam içermeli (0-9)"
    return None

# ─── Email sending ─────────────────────────────────────────────────────────────
def send_verification_email(to_email: str, code: str):
    subject = f"Mini PaaS — Doğrulama Kodunuz: {code}"
    body    = f"""Merhaba,

Mini PaaS hesabınızı doğrulamak için aşağıdaki kodu kullanın:

  ┌─────────────┐
  │   {code}   │
  └─────────────┘

Bu kod {CODE_EXPIRE_MINUTES} dakika geçerlidir.

E er bu isteği siz yapmadıysanız bu e-postayı görmezden gelin.

— Mini PaaS Ekibi
"""
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
            print(f"[Email] Doğrulama kodu gönderildi → {to_email}")
        except Exception as e:
            print(f"[Email] SMTP hatası: {e}")
    else:
        # Geliştirme modunda konsola yaz
        print(f"\n{'='*40}")
        print(f"[DEV MODE] Doğrulama kodu")
        print(f"  Email : {to_email}")
        print(f"  Kod   : {code}")
        print(f"{'='*40}\n")

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
    # Aktif kullanıcı mı?
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Bu e-posta zaten kayıtlı")
    conn.close()

    hashed = hash_password(req.password)
    code   = str(secrets.randbelow(900000) + 100000)  # 6 haneli
    expires = (datetime.utcnow() + timedelta(minutes=CODE_EXPIRE_MINUTES)).isoformat()

    conn = get_connection()
    conn.execute("""
        INSERT INTO pending_verifications (email, password, code, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET password=excluded.password, code=excluded.code, expires_at=excluded.expires_at
    """, (email, hashed, code, expires))
    conn.commit()
    conn.close()

    send_verification_email(email, code)
    return {"message": "Doğrulama kodu gönderildi", "email": email}

@app.post("/api/verify")
async def verify(req: VerifyRequest):
    email = req.email.lower().strip()
    conn  = get_connection()
    row   = conn.execute(
        "SELECT * FROM pending_verifications WHERE email=?", (email,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Bekleyen doğrulama bulunamadı. Tekrar kayıt olun.")

    if datetime.utcnow() > datetime.fromisoformat(row["expires_at"]):
        conn.execute("DELETE FROM pending_verifications WHERE email=?", (email,))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=410, detail="Kodun süresi dolmuş. Tekrar kayıt olun.")

    if row["code"] != req.code.strip():
        conn.close()
        raise HTTPException(status_code=400, detail="Doğrulama kodu hatalı")

    # Kodu doğru — kullanıcıyı aktif tabloya al
    try:
        conn.execute(
            "INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)",
            (email, row["password"], datetime.utcnow().isoformat())
        )
    except Exception:
        pass  # Zaten kayıtlıysa geç

    conn.execute("DELETE FROM pending_verifications WHERE email=?", (email,))
    conn.commit()
    conn.close()

    token = create_token(email)
    return {"token": token, "email": email, "message": "Hesap doğrulandı, hoş geldiniz!"}

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

# ─── Deploy endpoints ──────────────────────────────────────────────────────────
def trigger_builder(deploy_id: int, github_url: str, project_name: str):
    try:
        r = http_requests.post(
            "http://builder-service:5000/deploy",
            json={"deploy_id": deploy_id, "repo_url": github_url, "project_name": project_name},
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
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO deployments (project_name, github_url, status, created_at) VALUES (?,?,?,?)",
        (req.project_name, req.github_url, "Pending", datetime.utcnow().isoformat())
    )
    deploy_id = cursor.lastrowid
    conn.commit()
    conn.close()
    bg.add_task(trigger_builder, deploy_id, req.github_url, req.project_name)
    return {"deploy_id": deploy_id, "message": "Deployment başlatıldı"}

@app.post("/api/webhook")
async def webhook(req: WebhookRequest):
    conn = get_connection()
    conn.execute(
        "UPDATE deployments SET status=?, port=?, subdomain=? WHERE id=?",
        (req.status, req.port, req.subdomain, req.deploy_id)
    )
    conn.commit()
    conn.close()
    return {"message": "Güncellendi"}

@app.get("/api/status/{deploy_id}")
async def status(deploy_id: int, email: str = Depends(verify_token)):
    conn = get_connection()
    row  = conn.execute("SELECT * FROM deployments WHERE id=?", (deploy_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Bulunamadı")
    return dict(row)

@app.get("/api/deployments")
async def list_deployments(email: str = Depends(verify_token)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM deployments ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-gateway"}

app.mount("/", StaticFiles(directory="static", html=True), name="static")

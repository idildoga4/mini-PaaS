from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
import hashlib, hmac, secrets, base64, re, os
from typing import Optional

from database import init_db, get_connection

SECRET_KEY         = os.getenv("JWT_SECRET", "mini-paas-secret-2025-xK9")
ALGORITHM          = "HS256"
TOKEN_EXPIRE_HOURS = 24

bearer = HTTPBearer()
app = FastAPI(title="Auth Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Models ───────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email:    str
    password: str

class LoginRequest(BaseModel):
    email:    str
    password: str

# ─── Password ─────────────────────────────────────────────────
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

# ─── Validation ───────────────────────────────────────────────
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

# ─── JWT ──────────────────────────────────────────────────────
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

# ─── Endpoints ────────────────────────────────────────────────
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
    conn.execute("INSERT INTO users (email, password, created_at) VALUES (?,?,?)",
                 (email, hashed, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"token": create_token(email), "email": email, "message": "Kayıt başarılı"}

@app.post("/api/login")
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    conn  = get_connection()
    row   = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not row or not check_password(req.password, row["password"]):
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    return {"token": create_token(email), "email": email, "message": "Giriş başarılı"}

@app.get("/api/me")
async def me(email: str = Depends(verify_token)):
    return {"email": email}

@app.get("/api/auth/verify")
async def verify_token_endpoint(email: str = Depends(verify_token)):
    """Diğer servisler token doğrulamak için bu endpoint'i çağırır."""
    return {"email": email, "valid": True}

@app.get("/health")
async def health():
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "service": "auth-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# Static files en sona mount edilmeli
app.mount("/", StaticFiles(directory="static", html=True), name="static")
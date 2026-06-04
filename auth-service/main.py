# Auth Service
# FAZ 6: StaticFiles mount kaldırıldı — dashboard artık dashboard-service tarafından serve ediliyor.
# FAZ 7: SQLite → PostgreSQL geçişi — ? placeholder'ları %s'e, cursor pattern'e geçildi.
# FAZ 8: circuit_breaker_state Gauge label'lı versiyona güncellendi.
#         circuit_breaker.py (deploy/github-service) ile aynı metric adı ve label yapısı kullanılıyor.
#         Prometheus'ta circuit_breaker_state{service="auth-service"} olarak ayrışır.

import uuid
import contextvars
import logging
from pythonjsonlogger import jsonlogger
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
import hashlib, hmac, secrets, base64, re, os
from typing import Optional
from secrets_helper import get_secret
from database import init_db, get_connection
from prometheus_client import Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

trace_id_var = contextvars.ContextVar("trace_id", default='no-trace')

class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id_var.get()
        return True

logger = logging.getLogger()
logger.addFilter(TraceIdFilter())
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    '%(asctime)s %(levelname)s %(name)s %(message)s %(service_name)s %(trace_id)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

service_logger = logging.LoggerAdapter(logger, extra={"service_name": "auth-service"})

SECRET_KEY         = get_secret("jwt_secret", "JWT_SECRET")
ALGORITHM          = "HS256"
TOKEN_EXPIRE_HOURS = 24

bearer = HTTPBearer()

# Prometheus metrikleri
auth_verify_duration = Histogram('auth_verify_duration_seconds', 'Auth dogrulama suresi')

# FAZ 8: Label'lı Gauge — circuit_breaker.py ile aynı yapı.
# deploy-service ve github-service'de circuit_breaker.py aynı metric adını
# service="deploy-service" / service="github-service" label'ıyla kaydediyor.
# Auth-service burada service="auth-service" ile kaydediyor.
# Grafana'da tek panelde üç servisin circuit durumu görünür.
circuit_state = Gauge(
    'circuit_breaker_state',
    'Circuit breaker durumu (0=CLOSED, 1=OPEN)',
    ['service']
)
circuit_state.labels(service='auth-service').set(0)  # Auth-service'in circuit'i her zaman CLOSED

app = FastAPI(title="Auth Service")

os.makedirs("data", exist_ok=True)
init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4())[:8])
    token = trace_id_var.set(trace_id)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    trace_id_var.reset(token)
    return response

# --- Models ---
class RegisterRequest(BaseModel):
    email:    str
    password: str

class LoginRequest(BaseModel):
    email:    str
    password: str

# --- Password ---
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

# --- Validation ---
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')

def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))

def validate_password(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Sifre en az 8 karakter olmali"
    if not re.search(r'[A-Z]', password):
        return "En az bir buyuk harf icermeli (A-Z)"
    if not re.search(r'[a-z]', password):
        return "En az bir kucuk harf icermeli (a-z)"
    if not re.search(r'[0-9]', password):
        return "En az bir rakam icermeli (0-9)"
    return None

# --- JWT ---
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
            raise HTTPException(status_code=401, detail="Gecersiz token")
        return email
    except JWTError:
        raise HTTPException(status_code=401, detail="Token gecersiz veya suresi dolmus")

# --- Endpoints ---
@app.post("/api/register")
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if not validate_email(email):
        raise HTTPException(status_code=400, detail="Gecerli bir e-posta adresi girin")
    pw_err = validate_password(req.password)
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)

    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email=%s", (email,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Bu e-posta zaten kayitli")

    hashed = hash_password(req.password)
    c.execute(
        "INSERT INTO users (email, password, created_at) VALUES (%s,%s,%s)",
        (email, hashed, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    service_logger.info(f"Kullanici kayit oldu: {email}")
    return {"token": create_token(email), "email": email, "message": "Kayit basarili"}

@app.post("/api/login")
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    conn  = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=%s", (email,))
    row = c.fetchone()
    conn.close()
    if not row or not check_password(req.password, row["password"]):
        raise HTTPException(status_code=401, detail="E-posta veya sifre hatali")
    service_logger.info(f"Kullanici giris yapti: {email}")
    return {"token": create_token(email), "email": email, "message": "Giris basarili"}

@app.get("/api/me")
async def me(email: str = Depends(verify_token)):
    return {"email": email}

@app.get("/api/auth/verify")
@auth_verify_duration.time()
async def verify_token_endpoint(email: str = Depends(verify_token)):
    """Diger servisler token dogrulamak icin bu endpoint'i cagirir."""
    return {"email": email, "valid": True}

@app.get("/health")
async def health():
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "service": "auth-service"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get('/metrics', include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

# FAZ 6: app.mount("/", StaticFiles(...)) KALDIRILDI.
# Dashboard artık dashboard-service (Nginx) tarafından serve ediliyor.
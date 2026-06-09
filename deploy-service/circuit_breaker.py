# circuit_breaker.py
# deploy-service ve github-service klasörlerine kopyala.
# FAZ 8: circuit_state Gauge eklendi — circuit OPEN/CLOSED olunca Prometheus'a yansır.

import hashlib
import os
import httpx
import redis as redis_lib

from fastapi import HTTPException
from prometheus_client import Gauge

REDIS_URL        = os.getenv("REDIS_URL", "redis://redis:6379")
CIRCUIT_KEY      = "circuit:auth_service"
FAIL_COUNT_KEY   = "circuit:auth_fail_count"
TOKEN_TTL        = 60    # saniye
CIRCUIT_OPEN_TTL = 30    # saniye
FAIL_THRESHOLD   = 3

# FAZ 8: Her servis kendi Gauge'unu tutar.
# SERVICE_NAME deploy-service ya da github-service olarak gelir,
# Prometheus'ta circuit_breaker_state{service="deploy-service"} şeklinde ayrışır.
_SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown-service")
circuit_state_gauge = Gauge(
    'circuit_breaker_state',
    'Circuit breaker durumu (0=CLOSED, 1=OPEN)',
    ['service']
)
circuit_state_gauge.labels(service=_SERVICE_NAME).set(0)  # Başlangıçta CLOSED

# Modül yüklenince bir kez bağlantı kur, tekrar kullan
_redis = None

def get_redis():
    global _redis
    if _redis is not None:
        try:
            _redis.ping()
            return _redis
        except Exception:
            _redis = None
    try:
        r = redis_lib.from_url(
            REDIS_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True
        )
        r.ping()
        _redis = r
        return _redis
    except Exception:
        return None


def _token_key(token: str) -> str:
    return "token:" + hashlib.sha256(token.encode()).hexdigest()[:32]


async def verify_token_with_circuit_breaker(token: str, auth_service_url: str) -> str:
    """
    Token doğrulama — Redis cache + circuit breaker.

    Akış:
    1. Redis cache hit → direkt dön (Auth Service'e gitme)
    2. Circuit 'open' → 503 dön
    3. Auth Service'e istek at (timeout: 3sn)
       - Başarılı → cache'e yaz, fail sıfırla, Gauge=0
       - Hata → fail artır, eşik aşıldıysa circuit aç, Gauge=1
    4. Redis yoksa → Auth Service'e direkt git
    """
    r = get_redis()

    # ── FALLBACK: Redis erişilemiyorsa direkt Auth Service ────────────────────
    if r is None:
        return await _direct_auth(token, auth_service_url)

    key = _token_key(token)

    # ── 1. Cache kontrolü ─────────────────────────────────────────────────────
    try:
        cached = r.get(key)
        if cached:
            print(f"[circuit-breaker] Cache HIT → {cached}")
            return cached
    except Exception as e:
        print(f"[circuit-breaker] Cache okuma hatası: {e}")

    # ── 2. Circuit breaker kontrolü ───────────────────────────────────────────
    try:
        state = r.get(CIRCUIT_KEY)
        if state == "open":
            print("[circuit-breaker] Circuit OPEN → 503")
            circuit_state_gauge.labels(service=_SERVICE_NAME).set(1)  # Gauge güncelle
            raise HTTPException(
                status_code=503,
                detail="Auth Service geçici olarak kullanılamıyor. Lütfen kısa süre sonra tekrar deneyin."
            )
        else:
            # Redis'teki state closed/yok → Gauge'u sıfırla
            circuit_state_gauge.labels(service=_SERVICE_NAME).set(0)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[circuit-breaker] Circuit state okuma hatası: {e}")

    # ── 3. Auth Service'e istek ───────────────────────────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{auth_service_url}/api/auth/verify",
                headers={"Authorization": f"Bearer {token}"},
                timeout=3  # 5sn → 3sn, Traefik timeout'undan önce bitsin
            )

        if resp.status_code == 200:
            email = resp.json()["email"]
            try:
                r.setex(key, TOKEN_TTL, email)
                r.delete(FAIL_COUNT_KEY)
                r.delete(CIRCUIT_KEY)
                circuit_state_gauge.labels(service=_SERVICE_NAME).set(0)  # CLOSED
            except Exception:
                pass
            return email

        # 401/403: token geçersiz, circuit açma
        raise HTTPException(status_code=401, detail="Token geçersiz")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[circuit-breaker] Auth Service hatası: {e}")
        try:
            count = r.incr(FAIL_COUNT_KEY)
            r.expire(FAIL_COUNT_KEY, CIRCUIT_OPEN_TTL * 2)
            print(f"[circuit-breaker] Fail count: {count}/{FAIL_THRESHOLD}")

            if count >= FAIL_THRESHOLD:
                r.setex(CIRCUIT_KEY, CIRCUIT_OPEN_TTL, "open")
                r.delete(FAIL_COUNT_KEY)
                circuit_state_gauge.labels(service=_SERVICE_NAME).set(1)  # OPEN
                print("[circuit-breaker] Circuit AÇILDI — 30sn sonra half-open")
        except Exception:
            pass

        raise HTTPException(status_code=503, detail="Auth Service'e ulaşılamıyor")


async def _direct_auth(token: str, auth_service_url: str) -> str:
    """Redis yokken doğrudan Auth Service çağrısı."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{auth_service_url}/api/auth/verify",
                headers={"Authorization": f"Bearer {token}"},
                timeout=3
            )
            if resp.status_code == 200:
                return resp.json()["email"]
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="Token geçersiz")
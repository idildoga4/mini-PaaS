"""
circuit_breaker.py — github-service
Faz 6 değişikliği: State geçişlerinde circuit_breaker_state Gauge güncelleniyor.
  CLOSED → gauge.set(0)
  OPEN   → gauge.set(1)
"""

import hashlib
import httpx
import redis
from prometheus_client import Gauge

# ─── Prometheus Gauge ───────────────────────────────────────────────────────
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0=CLOSED, 1=OPEN",
    ["service"],
)

circuit_breaker_state.labels(service="github-service").set(0)

# ─── Redis bağlantısı ────────────────────────────────────────────────────────
_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)
    return _redis


# ─── Sabitler ────────────────────────────────────────────────────────────────
CACHE_TTL         = 60
CIRCUIT_OPEN_TTL  = 30
FAILURE_THRESHOLD = 3
AUTH_SERVICE_URL  = "http://auth-service:8001"
HTTP_TIMEOUT      = 3.0

def _cache_key(token: str) -> str:
    h = hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"token_cache:{h}"

CB_STATE_KEY   = "cb:github-service:state"
CB_FAILURE_KEY = "cb:github-service:failures"


# ─── Circuit state yardımcıları ──────────────────────────────────────────────
def _is_open(r: redis.Redis) -> bool:
    return r.exists(CB_STATE_KEY) == 1


def _set_open(r: redis.Redis) -> None:
    r.setex(CB_STATE_KEY, CIRCUIT_OPEN_TTL, "open")
    circuit_breaker_state.labels(service="github-service").set(1)


def _set_closed(r: redis.Redis) -> None:
    r.delete(CB_STATE_KEY)
    r.delete(CB_FAILURE_KEY)
    circuit_breaker_state.labels(service="github-service").set(0)


def _increment_failure(r: redis.Redis) -> int:
    count = r.incr(CB_FAILURE_KEY)
    r.expire(CB_FAILURE_KEY, CIRCUIT_OPEN_TTL * 2)
    return count


# ─── Ana doğrulama fonksiyonu ─────────────────────────────────────────────────
async def verify_token_with_circuit_breaker(token: str) -> str | None:
    """
    Token doğrulama — cache → circuit breaker → auth-service sırası.
    Redis erişilemezse fallback olarak doğrudan Auth Service'e git.
    """
    try:
        r = get_redis()

        cache_key = _cache_key(token)
        cached = r.get(cache_key)
        if cached:
            return cached

        if _is_open(r):
            return None

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(
                    f"{AUTH_SERVICE_URL}/api/auth/verify",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200:
                email = resp.json().get("email")
                if email:
                    r.setex(cache_key, CACHE_TTL, email)
                    _set_closed(r)
                    return email
            count = _increment_failure(r)
            if count >= FAILURE_THRESHOLD:
                _set_open(r)
            return None

        except (httpx.RequestError, httpx.TimeoutException):
            count = _increment_failure(r)
            if count >= FAILURE_THRESHOLD:
                _set_open(r)
            return None

    except redis.RedisError:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(
                    f"{AUTH_SERVICE_URL}/api/auth/verify",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200:
                return resp.json().get("email")
        except Exception:
            pass
        return None
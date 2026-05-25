"""
circuit_breaker.py — deploy-service
Faz 6 değişikliği: State geçişlerinde circuit_breaker_state Gauge güncelleniyor.
  CLOSED → gauge.set(0)
  OPEN   → gauge.set(1)
"""

import hashlib
import time
import httpx
import redis
from prometheus_client import Gauge

# ─── Prometheus Gauge ───────────────────────────────────────────────────────
# auth-service'deki circuit_state ile aynı isim kullanılırsa Prometheus çift
# kayıt hatası verir; servis adını label olarak ayırt ediyoruz.
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0=CLOSED, 1=OPEN",
    ["service"],          # label: hangi servisin circuit'i
)

# Başlangıçta CLOSED
circuit_breaker_state.labels(service="deploy-service").set(0)

# ─── Redis bağlantısı ────────────────────────────────────────────────────────
_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)
    return _redis


# ─── Sabitler ────────────────────────────────────────────────────────────────
CACHE_TTL         = 60    # Token cache süresi (saniye)
CIRCUIT_OPEN_TTL  = 30    # Circuit OPEN kalma süresi (saniye)
FAILURE_THRESHOLD = 3     # Kaç ardışık hata sonrası OPEN?
AUTH_SERVICE_URL  = "http://auth-service:8001"
HTTP_TIMEOUT      = 3.0   # Saniye

# Redis key şemaları
def _cache_key(token: str) -> str:
    h = hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"token_cache:{h}"

CB_STATE_KEY    = "cb:deploy-service:state"       # "open" | yok → closed
CB_FAILURE_KEY  = "cb:deploy-service:failures"    # ardışık hata sayacı


# ─── Circuit state yardımcıları ──────────────────────────────────────────────
def _is_open(r: redis.Redis) -> bool:
    return r.exists(CB_STATE_KEY) == 1


def _set_open(r: redis.Redis) -> None:
    """Circuit'i OPEN yap, TTL başlat, Gauge'u güncelle."""
    r.setex(CB_STATE_KEY, CIRCUIT_OPEN_TTL, "open")
    circuit_breaker_state.labels(service="deploy-service").set(1)


def _set_closed(r: redis.Redis) -> None:
    """Circuit'i CLOSED yap, hata sayacını sıfırla, Gauge'u güncelle."""
    r.delete(CB_STATE_KEY)
    r.delete(CB_FAILURE_KEY)
    circuit_breaker_state.labels(service="deploy-service").set(0)


def _increment_failure(r: redis.Redis) -> int:
    """Hata sayacını artır, mevcut değeri döndür."""
    count = r.incr(CB_FAILURE_KEY)
    # Sayacın TTL'i yoksa OPEN süresiyle hizala
    r.expire(CB_FAILURE_KEY, CIRCUIT_OPEN_TTL * 2)
    return count


# ─── Ana doğrulama fonksiyonu ─────────────────────────────────────────────────
async def verify_token_with_circuit_breaker(token: str) -> str | None:
    """
    Token doğrulama — cache → circuit breaker → auth-service sırası:
    1. Cache HIT → doğrudan email döndür.
    2. Circuit OPEN → 503 anlamına gelir, None döndür.
    3. Auth Service'e istek at; başarılı → cache'e yaz, circuit kapat.
       Başarısız → hata say, eşik aşıldıysa circuit aç.
    Redis erişilemezse doğrudan Auth Service'e git (fallback).
    """
    try:
        r = get_redis()

        # 1. Cache kontrolü
        cache_key = _cache_key(token)
        cached = r.get(cache_key)
        if cached:
            return cached  # Cache HIT

        # 2. Circuit OPEN mu?
        if _is_open(r):
            return None   # 503 dönecek

        # 3. Auth Service isteği
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
                    _set_closed(r)          # Başarılı → circuit kapat
                    return email
            # HTTP hata (401, 500 vb.) → hata say
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
        # Redis erişilemez → direkt Auth Service'e git
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
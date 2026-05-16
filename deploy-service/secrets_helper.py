# secrets_helper.py
# Her servisin klasorune bu dosyayi kopyala.
# main.py basina: from secrets_helper import get_secret

import os

def get_secret(name: str, env_fallback: str = None) -> str:
    """
    Once /run/secrets/<name> dosyasini okur (Docker Secrets).
    Dosya yoksa env_fallback environment variable'ina bakar.
    Ikisi de yoksa hata firlatir.

    Kullanim:
        JWT_SECRET    = get_secret("jwt_secret",          "JWT_SECRET")
        WEBHOOK_SEC   = get_secret("webhook_secret",      "WEBHOOK_SECRET")
        GH_SECRET     = get_secret("github_client_secret","GITHUB_CLIENT_SECRET")
        NGROK_TOKEN   = get_secret("ngrok_authtoken",     "NGROK_AUTHTOKEN")
    """
    secret_path = f"/run/secrets/{name}"
    if os.path.exists(secret_path):
        with open(secret_path) as f:
            return f.read().strip()

    fallback_key = env_fallback or name.upper()
    value = os.getenv(fallback_key)
    if value:
        return value

    raise RuntimeError(
        f"Secret '{name}' bulunamadi. "
        f"Ne /run/secrets/{name} dosyasi ne de {fallback_key} env var mevcut."
    )

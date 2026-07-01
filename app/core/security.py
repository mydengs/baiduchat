import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional

from app.core.config import get_settings


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$", 2)
    except ValueError:
        return False
    candidate = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(candidate, expected)


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_admin_cookie() -> str:
    settings = get_settings()
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    body = f"{ts}.{nonce}"
    sig = hmac.new(settings.app_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_admin_cookie(value: str, max_age_seconds: int = 86400) -> bool:
    settings = get_settings()
    try:
        ts, nonce, sig = value.split(".", 2)
        body = f"{ts}.{nonce}"
        expected = hmac.new(settings.app_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return int(time.time()) - int(ts) <= max_age_seconds
    except Exception:
        return False

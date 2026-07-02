from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import sha256_token
from app.db.init_db import get_setting
from app.db.models import ApiKey, RequestLog
from app.db.session import get_db


def get_api_key(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiKey:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    raw = authorization.split(" ", 1)[1].strip()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == sha256_token(raw), ApiKey.enabled.is_(True)))
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    now = datetime.utcnow()
    if key.validity_days > 0 and not key.activated_at:
        key.activated_at = now
        key.expires_at = now + timedelta(days=key.validity_days)
    if key.expires_at and key.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")
    key.use_count_total = (key.use_count_total or 0) + 1
    key.last_used_at = now
    db.commit()
    db.refresh(key)
    return key


def ensure_model_allowed(api_key: ApiKey, model: str) -> None:
    allowed = [item.strip() for item in api_key.allowed_models.split(",") if item.strip()]
    if "*" in allowed or model in allowed:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Model not allowed: {model}")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _csv_values(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def enforce_access_policy(api_key: ApiKey, model: str, request: Request, db: Session) -> None:
    ensure_model_allowed(api_key, model)
    ip = client_ip(request)

    whitelist = _csv_values(get_setting(db, "ip_whitelist", ""))
    blacklist = _csv_values(get_setting(db, "ip_blacklist", ""))
    key_whitelist = _csv_values(api_key.ip_whitelist)
    key_blacklist = _csv_values(api_key.ip_blacklist)
    if blacklist and ip in blacklist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP is blocked")
    if whitelist and ip not in whitelist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP is not allowed")
    if key_blacklist and ip in key_blacklist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP is blocked for this API key")
    if key_whitelist and ip not in key_whitelist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="IP is not allowed for this API key")

    per_minute = int(get_setting(db, "rate_limit_per_minute", "0") or "0")
    if per_minute > 0:
        since = datetime.utcnow() - timedelta(minutes=1)
        count = db.scalar(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.api_key_name == api_key.name, RequestLog.created_at >= since)
        ) or 0
        if count >= per_minute:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    if api_key.request_limit_per_day > 0:
        since = datetime.utcnow() - timedelta(days=1)
        count = db.scalar(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.api_key_name == api_key.name, RequestLog.created_at >= since)
        ) or 0
        if count >= api_key.request_limit_per_day:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Daily quota exceeded")

    if api_key.request_limit_total > 0:
        total = db.scalar(
            select(func.count())
            .select_from(RequestLog)
            .where(RequestLog.api_key_name == api_key.name)
        ) or 0
        if total >= api_key.request_limit_total:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Total request quota exceeded")

    if api_key.token_limit_per_day > 0:
        since = datetime.utcnow() - timedelta(days=1)
        used = db.scalar(
            select(func.coalesce(func.sum(RequestLog.prompt_chars + RequestLog.completion_chars), 0))
            .where(RequestLog.api_key_name == api_key.name, RequestLog.created_at >= since)
        ) or 0
        if used >= api_key.token_limit_per_day:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Daily token quota exceeded")

    if api_key.token_limit_total > 0:
        used = db.scalar(
            select(func.coalesce(func.sum(RequestLog.prompt_chars + RequestLog.completion_chars), 0))
            .where(RequestLog.api_key_name == api_key.name)
        ) or 0
        if used >= api_key.token_limit_total:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Total token quota exceeded")

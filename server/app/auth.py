from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from hmac import compare_digest

from fastapi import Cookie, Header, HTTPException, Request, Response

from .settings import settings

LOCAL_SESSION_COOKIE = "poy_dty_local_session"
_DEV_SESSION_SECRET = secrets.token_hex(32)


def _session_secret() -> str:
    return settings.local_session_secret or settings.internal_api_token or _DEV_SESSION_SECRET


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    host = value.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_is_loopback(request: Request) -> bool:
    """Return whether the transport peer is local.

    Host, Origin, and Referer are caller-controlled and therefore must never
    grant loopback-only privileges.
    """
    client_host = request.client.host if request.client else ""
    return _is_loopback_host(client_host)


def create_local_session_token(now: int | None = None) -> str:
    issued_at = str(now or int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued_at}:{nonce}"
    signature = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def validate_local_session_token(token: str | None, now: int | None = None) -> bool:
    if not settings.enable_local_session_auth or not token:
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    issued_at, nonce, signature = parts
    if not issued_at.isdigit() or not nonce:
        return False
    timestamp = int(issued_at)
    current = now or int(time.time())
    if timestamp > current + 60:
        return False
    if current - timestamp > settings.local_session_ttl_seconds:
        return False
    payload = f"{issued_at}:{nonce}"
    expected = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return compare_digest(signature, expected)


def set_local_session_cookie(response: Response) -> int:
    token = create_local_session_token()
    response.set_cookie(
        LOCAL_SESSION_COOKIE,
        token,
        max_age=settings.local_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.production_like,
        path="/",
    )
    return settings.local_session_ttl_seconds


def clear_local_session_cookie(response: Response) -> None:
    response.delete_cookie(
        LOCAL_SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.production_like,
    )


def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None),
    local_session: str | None = Cookie(default=None, alias=LOCAL_SESSION_COOKIE),
) -> None:
    if not settings.enforce_internal_token:
        return
    if not settings.internal_api_token:
        raise HTTPException(status_code=503, detail="internal token is not configured")
    if x_internal_token and compare_digest(x_internal_token, settings.internal_api_token):
        return
    if request_is_loopback(request) and validate_local_session_token(local_session):
        return
    raise HTTPException(status_code=401, detail="invalid internal token")

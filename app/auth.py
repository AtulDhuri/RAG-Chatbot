"""Short-lived HS256 widget sessions and admin authentication."""
from __future__ import annotations
import base64, hashlib, hmac, json, time, uuid
from fastapi import HTTPException, Request
from .config import get_settings
from . import db


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _secret() -> bytes:
    secret = get_settings().jwt_secret
    if not secret or len(secret) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters")
    return secret.encode()


def issue_session(chatbot_id: str) -> str:
    now = int(time.time()); settings = get_settings()
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64(json.dumps({"sub": chatbot_id, "jti": str(uuid.uuid4()), "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now, "exp": now + settings.widget_session_ttl_seconds}, separators=(",", ":")).encode())
    body = f"{header}.{payload}"; sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_session(token: str) -> dict:
    try:
        head, body, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected): raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        settings = get_settings(); now = int(time.time())
        if data.get("iss") != settings.jwt_issuer or data.get("aud") != settings.jwt_audience or int(data["exp"]) <= now: raise ValueError("claims")
        return data
    except Exception as exc:
        raise HTTPException(status_code=403, detail={"success": False, "message": "Chatbot unavailable"}) from exc


def session_claims(request: Request) -> dict:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        db.log_security_event(None, "session_abuse", None, request.client.host if request.client else None, "missing bearer token")
        raise HTTPException(status_code=403, detail={"success": False, "message": "Chatbot unavailable"})
    try:
        return decode_session(value[7:])
    except HTTPException:
        db.log_security_event(None, "session_abuse", None, request.client.host if request.client else None, "invalid session token")
        raise


def _issue(payload: dict, ttl: int) -> str:
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    body_payload = dict(payload, iss=get_settings().jwt_issuer, iat=int(time.time()), exp=int(time.time()) + ttl, jti=str(uuid.uuid4()))
    body = f"{header}.{_b64(json.dumps(body_payload, separators=(',', ':')).encode())}"
    return f"{body}.{_b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())}"


def issue_admin_session() -> str:
    return _issue({"role": "admin", "aud": "rag-admin"}, 3600)


def decode_admin_session(token: str) -> dict:
    try:
        head, body, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected): raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("iss") != get_settings().jwt_issuer or data.get("aud") != "rag-admin" or data.get("role") != "admin" or int(data["exp"]) <= int(time.time()): raise ValueError("claims")
        return data
    except Exception as exc:
        raise HTTPException(status_code=401, detail={"success": False, "message": "Admin authentication required"}) from exc


def require_admin(request: Request) -> None:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"success": False, "message": "Admin authentication required"})
    decode_admin_session(value[7:])

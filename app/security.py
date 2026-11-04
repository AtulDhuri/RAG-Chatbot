"""Origin, referer, subscription, and failure-response policy."""
from __future__ import annotations
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import HTTPException, Request
from . import db

GENERIC = {"success": False, "message": "Chatbot unavailable"}


def _origin(value: str | None) -> str | None:
    if not value: return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname: return None
    host = parsed.hostname.lower().removeprefix("www.")
    port = parsed.port
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443): port = None
    suffix = f":{port}" if port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _registered_origin(domain: str, value: str | None) -> bool:
    origin = _origin(value)
    if not origin: return False
    parsed = urlparse(origin); expected = domain.lower().removeprefix("www.")
    host = parsed.hostname or ""
    return host == expected or host.removesuffix(".") == expected


def reject(request: Request, chatbot_id: str | None, event: str, reason: str, domain: str | None = None):
    db.log_security_event(chatbot_id, event, domain, request.client.host if request.client else None, reason)
    raise HTTPException(status_code=403, detail=GENERIC)


def validate_chatbot(request: Request, chatbot_id: str):
    bot = db.get_chatbot(chatbot_id)
    if bot is None: reject(request, chatbot_id, "invalid_chatbot_id", "not found")
    now = datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(bot["subscription_start_date"]); end = datetime.fromisoformat(bot["subscription_end_date"])
        if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    except ValueError: reject(request, chatbot_id, "expired_subscription", "invalid dates")
    if bot["status"] != "active": reject(request, chatbot_id, "suspended_account", bot["status"], bot["allowed_domain"])
    if not (start <= now <= end): reject(request, chatbot_id, "expired_subscription", "outside subscription", bot["allowed_domain"])
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if not _registered_origin(bot["allowed_domain"], origin): reject(request, chatbot_id, "invalid_domain", "origin mismatch", bot["allowed_domain"])
    if referer and not _registered_origin(bot["allowed_domain"], referer): reject(request, chatbot_id, "invalid_referer", "referer mismatch", bot["allowed_domain"])
    return bot

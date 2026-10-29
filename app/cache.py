"""Tenant-scoped response cache with Redis and safe local fallback."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

from .config import get_settings

_settings = get_settings()
_lock = threading.Lock()
_memory: dict[str, tuple[float, dict[str, Any]]] = {}
_redis = None

try:
    if _settings.redis_url:
        from redis import Redis
        _redis = Redis.from_url(_settings.redis_url, decode_responses=True, socket_timeout=0.2)
except Exception:
    _redis = None


def normalize_question(question: str) -> str:
    value = question.casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    for old, new in (("check in", "checkin"), ("check-out", "checkout"), ("check out", "checkout"), ("timing", "time")):
        value = value.replace(old, new)
    # Question framing should not create separate cache entries for the same
    # intent, while the substantive terms remain part of the key.
    value = re.sub(r"^(what is|what are|when is|when can i|can i|please)\s+", "", value)
    return value.strip()


def key(tenant_id: str, website_id: str, domain: str, language: str, question: str) -> str:
    digest = hashlib.sha256(normalize_question(question).encode()).hexdigest()
    return f"rag:answer:v1:{tenant_id}:{website_id}:{domain}:{language or 'auto'}:{digest}"


def get(cache_key: str) -> dict[str, Any] | None:
    try:
        if _redis is not None:
            raw = _redis.get(cache_key)
            return json.loads(raw) if raw else None
    except Exception:
        pass
    with _lock:
        item = _memory.get(cache_key)
        if not item:
            return None
        expires, value = item
        if expires <= time.time():
            _memory.pop(cache_key, None)
            return None
        return dict(value)


def set(cache_key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
    ttl = max(1, ttl_seconds or _settings.cache_ttl_seconds)
    payload = dict(value)
    payload.setdefault("created_at", time.time())
    payload["last_accessed_at"] = time.time()
    try:
        if _redis is not None:
            _redis.setex(cache_key, ttl, json.dumps(payload))
            return
    except Exception:
        pass
    with _lock:
        _memory[cache_key] = (time.time() + ttl, payload)


def invalidate_domain(domain: str) -> None:
    pattern = f"rag:answer:v1:*:*:{domain}:*"
    try:
        if _redis is not None:
            for item in _redis.scan_iter(match=pattern, count=100):
                _redis.delete(item)
    except Exception:
        pass
    with _lock:
        for item in list(_memory):
            if f":{domain}:" in item:
                _memory.pop(item, None)

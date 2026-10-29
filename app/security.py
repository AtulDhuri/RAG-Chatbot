"""Early unauthorized-source protection for tenant endpoints."""
from __future__ import annotations

import hashlib
import time
import threading
from collections import defaultdict, deque
from fastapi import HTTPException, Request
from .config import get_settings

_settings = get_settings()
_lock = threading.Lock()
_failures: dict[str, deque[float]] = defaultdict(deque)
_blocks: dict[str, float] = {}
_redis = None
try:
    if _settings.redis_url:
        from redis import Redis
        _redis = Redis.from_url(_settings.redis_url, decode_responses=True, socket_timeout=0.2)
except Exception:
    _redis = None


def _source_key(request: Request, domain: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    return hashlib.sha256(f"{ip}:{domain}".encode()).hexdigest()


def _counter(key: str, limit: int, window: int) -> bool:
    if _redis is None:
        return True
    try:
        value = int(_redis.incr(key))
        if value == 1:
            _redis.expire(key, window)
        return value <= limit
    except Exception as exc:
        raise HTTPException(503, "Distributed protection backend unavailable") from exc


def enforce_distributed_rate_limit(request: Request, domain: str, *, tenant_id: str = "", website_id: str = "") -> None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "unknown")
    keys = [f"rag:rate:ip:{ip}", f"rag:rate:domain:{domain}"]
    if tenant_id:
        keys.append(f"rag:rate:tenant:{tenant_id}")
    if website_id:
        keys.append(f"rag:rate:website:{website_id}")
    if not all(_counter(key, _settings.rate_limit_per_minute, 60) for key in keys):
        raise HTTPException(429, "Rate limit exceeded")


def reject_if_blocked(request: Request, domain: str) -> None:
    source = _source_key(request, domain)
    if _redis is not None:
        try:
            if _redis.exists(f"rag:block:{source}"):
                raise HTTPException(401, "Unauthorized source")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Distributed protection backend unavailable") from exc
        return
    with _lock:
        until = _blocks.get(source, 0)
        if until > time.time():
            raise HTTPException(401, "Unauthorized source")
        if until:
            _blocks.pop(source, None)


def record_unauthorized(request: Request, domain: str) -> None:
    source = _source_key(request, domain)
    if _redis is not None:
        try:
            key = f"rag:unauthorized:{source}"
            count = int(_redis.incr(key))
            if count == 1:
                _redis.expire(key, _settings.unauthorized_window_seconds)
            if count >= _settings.unauthorized_threshold:
                _redis.setex(f"rag:block:{source}", _settings.unauthorized_block_seconds, "1")
            return
        except Exception as exc:
            raise HTTPException(503, "Distributed protection backend unavailable") from exc
    now = time.time()
    with _lock:
        failures = _failures[source]
        cutoff = now - _settings.unauthorized_window_seconds
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(now)
        if len(failures) >= _settings.unauthorized_threshold:
            _blocks[source] = now + _settings.unauthorized_block_seconds
            failures.clear()

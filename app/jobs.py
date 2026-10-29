"""Durable Redis crawl job queue used by web replicas and workers."""
from __future__ import annotations
import json
from .config import get_settings

_settings = get_settings()
_QUEUE = "rag:jobs:crawl"
_LOCK_PREFIX = "rag:jobs:crawl:lock:"


def _client():
    if not _settings.redis_url:
        return None
    from redis import Redis
    return Redis.from_url(_settings.redis_url, decode_responses=True, socket_timeout=0.5)


def enqueue_reindex(domain: str, force: bool = False) -> bool:
    client = _client()
    if client is None:
        return False
    lock = _LOCK_PREFIX + domain
    if not client.set(lock, "1", nx=True, ex=max(300, _settings.crawl_timeout * _settings.crawl_max_pages)):
        return True
    try:
        client.rpush(_QUEUE, json.dumps({"domain": domain, "force": force}))
        return True
    except Exception:
        client.delete(lock)
        return False


def release(domain: str) -> None:
    client = _client()
    if client is not None:
        client.delete(_LOCK_PREFIX + domain)


def next_job(timeout: int = 5) -> dict | None:
    client = _client()
    if client is None:
        raise RuntimeError("REDIS_URL is required for the crawl worker")
    item = client.blpop(_QUEUE, timeout=timeout)
    return json.loads(item[1]) if item else None


def is_queued(domain: str) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        return bool(client.exists(_LOCK_PREFIX + domain))
    except Exception:
        return False

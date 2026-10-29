"""Shared PostgreSQL connection pool for request-scoped database work."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from .config import get_settings

_settings = get_settings()


@lru_cache(maxsize=1)
def pool():
    from psycopg_pool import ConnectionPool
    if not _settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return ConnectionPool(
        conninfo=_settings.database_url,
        min_size=1,
        max_size=8,
        kwargs={"autocommit": True, "prepare_threshold": None},
        open=True,
    )


@contextmanager
def connection() -> Iterator:
    """Borrow a pooled connection and return it without closing the socket."""
    with pool().connection() as conn:
        yield conn

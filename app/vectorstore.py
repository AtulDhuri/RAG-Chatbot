"""Vector-store facade: one stable interface, two interchangeable backends.

The rest of the app (``indexer``, ``rag``, ``main``) imports only this module
and never a specific backend. That keeps the storage choice swappable without
touching the pipeline - the same way ``crawler.crawl_domain`` hides the crawl
backend.

Backends
--------
- ``pgvector`` (``app/vectorstore_pg.py``): Postgres + pgvector on Supabase.
  Durable, multi-tenant, survives redeploys and multiple replicas. Use in
  production.
- ``chroma`` (``app/vectorstore_chroma.py``): local on-disk ChromaDB. Zero
  infra, great for local dev, but ephemeral in containers.

Selection (``settings.vector_backend``)
    "pgvector" -> force Postgres (errors if DATABASE_URL is missing)
    "chroma"   -> force local ChromaDB
    "auto"     -> pgvector when DATABASE_URL is set, else chroma

Public interface (unchanged across backends):
    is_indexed(domain) -> bool
    last_indexed_at(domain) -> float
    is_fresh(domain, ttl_seconds) -> bool
    index_documents(domain, records, *, chunk_fn) -> int
    query(domain, question, *, top_k) -> list[dict]
"""
from __future__ import annotations

import logging

from .config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

_backend = None


def _select_backend():
    """Resolve and cache the active backend module per the configuration."""
    global _backend
    if _backend is not None:
        return _backend

    choice = (_settings.vector_backend or "auto").lower()

    if choice == "auto":
        choice = "pgvector" if _settings.database_url else "chroma"

    if choice == "pgvector":
        from . import vectorstore_pg as backend
        if not backend.is_available():
            raise RuntimeError(
                "vector_backend is 'pgvector' but DATABASE_URL is not set."
            )
    elif choice == "chroma":
        from . import vectorstore_chroma as backend
    else:
        raise RuntimeError(f"Unknown vector_backend: {_settings.vector_backend!r}")

    logger.info("Vector store backend: %s", choice)
    _backend = backend
    return _backend


# --- Facade: delegate the public interface to the selected backend. ---

def is_indexed(domain: str) -> bool:
    return _select_backend().is_indexed(domain)


def last_indexed_at(domain: str) -> float:
    return _select_backend().last_indexed_at(domain)


def is_fresh(domain: str, ttl_seconds: int) -> bool:
    return _select_backend().is_fresh(domain, ttl_seconds)


def index_documents(domain: str, records: list[dict], *, chunk_fn) -> int:
    return _select_backend().index_documents(domain, records, chunk_fn=chunk_fn)


def query(domain: str, question: str, *, top_k: int) -> list[dict]:
    return _select_backend().query(domain, question, top_k=top_k)

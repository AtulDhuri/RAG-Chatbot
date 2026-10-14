"""Orchestrates crawl -> chunk -> embed for a domain, with in-flight tracking."""
from __future__ import annotations

import logging
import threading

from .config import get_settings
from .crawler import crawl_domain, normalize_domain
from .chunking import chunk_text
from . import vectorstore

logger = logging.getLogger(__name__)
_settings = get_settings()

# Domains currently being crawled, so we don't start a second crawl in parallel.
_in_progress: set[str] = set()
_lock = threading.Lock()


def is_indexing(domain: str) -> bool:
    with _lock:
        return normalize_domain(domain) in _in_progress


def _mark_start(domain: str) -> bool:
    """Return True if we acquired the slot, False if a crawl is already running."""
    with _lock:
        if domain in _in_progress:
            return False
        _in_progress.add(domain)
        return True


def _mark_done(domain: str) -> None:
    with _lock:
        _in_progress.discard(domain)


def reindex(domain: str) -> int:
    """Crawl and (re)index a domain synchronously. Returns chunk count."""
    domain = normalize_domain(domain)
    if not _mark_start(domain):
        logger.info("Crawl already in progress for %s", domain)
        return -1
    try:
        pages = crawl_domain(
            domain,
            max_pages=_settings.crawl_max_pages,
            timeout=_settings.crawl_timeout,
            respect_robots=_settings.crawl_respect_robots,
        )
        records = [{"url": p.url, "title": p.title, "text": p.text} for p in pages]

        def _chunk(text: str):
            return chunk_text(
                text,
                chunk_size=_settings.chunk_size,
                overlap=_settings.chunk_overlap,
            )

        count = vectorstore.index_documents(domain, records, chunk_fn=_chunk)
        return count
    except Exception:
        logger.exception("Indexing failed for %s", domain)
        return 0
    finally:
        _mark_done(domain)


def ensure_indexed(domain: str, *, force: bool = False) -> str:
    """Ensure a domain has fresh content.

    Returns a status string: "ready", "indexing", or "started".
    Non-blocking: if a crawl is needed it runs in a background thread.
    """
    domain = normalize_domain(domain)

    if not force and vectorstore.is_fresh(domain, _settings.crawl_ttl_seconds):
        return "ready"

    if is_indexing(domain):
        return "indexing"

    # Fire-and-forget background crawl.
    threading.Thread(target=reindex, args=(domain,), daemon=True).start()

    # If we already have (stale) content, answers can still be served meanwhile.
    return "ready" if vectorstore.is_indexed(domain) else "started"

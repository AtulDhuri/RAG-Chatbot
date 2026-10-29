"""Orchestrates crawl -> chunk -> embed for a domain, with in-flight tracking."""
from __future__ import annotations

import logging
import threading

from .config import get_settings
from .crawler import crawl_domain, normalize_domain
from .chunking import chunk_text
from . import cache, vectorstore

logger = logging.getLogger(__name__)
_settings = get_settings()

# Domains currently being crawled, so we don't start a second crawl in parallel.
_in_progress: set[str] = set()
_lock = threading.Lock()

# Per-domain progress snapshots, so the API can report crawl status to clients.
# Shape: {domain: {"phase", "pages_done", "pages_known", "chunks", "last_url"}}.
# "phase" is one of: "crawling", "embedding", "ready", "error".
_progress: dict[str, dict] = {}


def is_indexing(domain: str) -> bool:
    domain = normalize_domain(domain)
    with _lock:
        return domain in _in_progress


def _set_progress(domain: str, **fields) -> None:
    with _lock:
        snap = _progress.setdefault(
            domain,
            {
                "phase": "crawling",
                "pages_done": 0,
                "pages_known": 0,
                "chunks": 0,
                "last_url": "",
            },
        )
        snap.update(fields)


def get_progress(domain: str) -> dict:
    """Return a copy of the latest progress snapshot for a domain.

    Falls back to a "ready"/"idle" snapshot when nothing is tracked yet, using
    the vector store to tell whether content already exists.
    """
    domain = normalize_domain(domain)
    with _lock:
        snap = _progress.get(domain)
        if snap is not None:
            out = dict(snap)
        else:
            out = None
    if out is not None:
        out["indexing"] = is_indexing(domain)
        return out
    # No active/finished crawl recorded this process lifetime.
    indexed = vectorstore.is_indexed(domain)
    return {
        "phase": "ready" if indexed else "idle",
        "pages_done": 0,
        "pages_known": 0,
        "chunks": 0,
        "last_url": "",
        "indexing": False,
    }


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


def reindex(domain: str, max_pages: int | None = None) -> int:
    """Crawl and (re)index a domain synchronously. Returns chunk count."""
    domain = normalize_domain(domain)
    crawl_limit = max(1, max_pages or _settings.crawl_max_pages)
    if not _mark_start(domain):
        logger.info("Crawl already in progress for %s", domain)
        return -1

    # Reset the snapshot for this run.
    _set_progress(
        domain,
        phase="crawling",
        pages_done=0,
        pages_known=0,
        chunks=0,
        last_url="",
    )

    def _on_progress(done: int, known: int, last_url: str) -> None:
        _set_progress(
            domain, pages_done=done, pages_known=known, last_url=last_url
        )

    try:
        pages = crawl_domain(
            domain,
            max_pages=crawl_limit,
            timeout=_settings.crawl_timeout,
            respect_robots=_settings.crawl_respect_robots,
            progress_cb=_on_progress,
        )
        records = [{"url": p.url, "title": p.title, "text": p.text} for p in pages]

        def _chunk(text: str):
            return chunk_text(
                text,
                chunk_size=_settings.chunk_size,
                overlap=_settings.chunk_overlap,
            )

        # Crawl finished; now embedding/storing.
        _set_progress(domain, phase="embedding", pages_done=len(pages))
        count = vectorstore.index_documents(domain, records, chunk_fn=_chunk)
        cache.invalidate_domain(domain)
        _set_progress(domain, phase="ready", chunks=count)
        return count
    except Exception:
        logger.exception("Indexing failed for %s", domain)
        _set_progress(domain, phase="error")
        return 0
    finally:
        _mark_done(domain)


def ensure_indexed(domain: str, *, force: bool = False, max_pages: int | None = None) -> str:
    """Ensure a domain has fresh content.

    Returns a status string: "ready", "indexing", or "started".
    Non-blocking: if a crawl is needed it runs in a background thread.
    """
    domain = normalize_domain(domain)

    if not force and vectorstore.is_fresh(domain, _settings.crawl_ttl_seconds):
        return "ready"

    if is_indexing(domain):
        return "indexing"

    # Run crawling in this API process. This keeps the setup simple and works
    # without Redis or a separate worker process.
    threading.Thread(target=reindex, args=(domain, max_pages), daemon=True).start()

    # If we already have (stale) content, answers can still be served meanwhile.
    return "ready" if vectorstore.is_indexed(domain) else "started"

"""Durable, resumable website ingestion with bounded embedding batches."""
from __future__ import annotations
import logging, threading
from .config import get_settings
from .extractor import get_website_extractor
from .chunking import chunk_text
from . import db, vectorstore

logger = logging.getLogger(__name__)
_settings = get_settings()
_in_progress: set[str] = set()
_lock = threading.Lock()
_progress: dict[str, dict] = {}
_PAGE_BATCH_SIZE = max(1, _settings.ingestion_page_batch_size)


def is_indexing(chatbot_id: str) -> bool:
    with _lock:
        return chatbot_id in _in_progress


def _set(chatbot_id, **fields):
    with _lock:
        _progress.setdefault(chatbot_id, {"phase": "idle", "pages_done": 0, "pages_known": 0, "chunks": 0, "last_url": "", "crawl_metrics": {}}).update(fields)


def get_progress(chatbot_id):
    with _lock:
        value = dict(_progress.get(chatbot_id, {"phase": "idle", "pages_done": 0, "pages_known": 0, "chunks": 0, "last_url": "", "crawl_metrics": {}}))
        value["indexing"] = chatbot_id in _in_progress
    try:
        run = db.latest_ingestion_run(chatbot_id)
        if run:
            value.update({"phase": run["status"], "pages_done": run["pages_done"], "pages_known": run["pages_total"], "chunks": run["chunks_done"], "last_url": run["last_url"], "indexing": True})
    except Exception:
        logger.debug("Could not load durable ingestion progress", exc_info=True)
    return value


def _records(pages):
    return [{"url": p["url"], "title": p["title"], "text": p["text"]} for p in pages]


def _run(chatbot_id: str, domain: str, force: bool = False):
    with _lock:
        if chatbot_id in _in_progress:
            return
        _in_progress.add(chatbot_id)
    started_at = db.now()
    run_id = None
    try:
        old = db.knowledge(chatbot_id)
        existing = None if force else db.latest_ingestion_run(chatbot_id)
        if existing:
            run_id = existing["id"]
            target_version = existing["target_version"]
            previous_version = existing["previous_version"]
            provider = existing["source_provider"]
        else:
            target_version = (old["knowledge_base_version"] if old else 0) + 1
            previous_version = old["knowledge_base_version"] if old else 0
            provider = ""
            run_id = db.create_ingestion_run(chatbot_id, target_version, previous_version)

        db.update_knowledge(chatbot_id, knowledge_status="processing", crawl_status="EXTRACTING", crawl_started_at=started_at, error_details="")
        _set(chatbot_id, phase="crawling", pages_done=0, pages_known=0, chunks=0, last_url="")

        stored_pages = db.ingestion_pages(run_id)
        if not stored_pages:
            extraction = get_website_extractor(force_recrawl=force).extract_knowledge(domain)
            provider = extraction.provider
            db.save_ingestion_pages(run_id, extraction.pages)
            db.update_ingestion_run(run_id, source_provider=provider)
            stored_pages = db.ingestion_pages(run_id)
            _set(chatbot_id, crawl_metrics={"source_provider": provider, "raw_pages": extraction.raw_pages, "total_pages": len(stored_pages)}, pages_known=len(stored_pages))
        else:
            provider = provider or "RESUMED"
            _set(chatbot_id, pages_known=len(stored_pages), crawl_metrics={"source_provider": provider, "total_pages": len(stored_pages), "resumed": True})

        if not stored_pages:
            raise RuntimeError(f"No usable pages extracted for {domain}")

        all_records = _records(stored_pages)
        total_tokens = sum(max(1, len(record["text"]) // 4) for record in all_records)
        digest = db.content_hash(all_records)
        old = db.knowledge(chatbot_id)
        if old and old["content_hash"] == digest and vectorstore.is_indexed(chatbot_id):
            db.update_knowledge(chatbot_id, knowledge_status="ready", crawl_status="READY", pages_crawled=len(stored_pages), total_tokens=total_tokens, total_chunks=vectorstore.count(chatbot_id), last_crawled_at=db.now(), crawl_completed_at=db.now(), embedding_provider=_settings.openai_embedding_model, source_provider=provider)
            db.finish_ingestion_run(run_id)
            _set(chatbot_id, phase="ready", pages_done=len(stored_pages), pages_known=len(stored_pages), chunks=vectorstore.count(chatbot_id))
            return

        db.update_ingestion_run(run_id, status="embedding", pages_total=len(stored_pages))
        db.update_knowledge(chatbot_id, crawl_status="GENERATING_EMBEDDINGS")
        completed = int((db.latest_ingestion_run(chatbot_id) or {}).get("pages_done") or 0)
        chunks = int((db.latest_ingestion_run(chatbot_id) or {}).get("chunks_done") or 0)
        pending = [page for page in stored_pages if page["status"] != "done"]
        _set(chatbot_id, phase="embedding", pages_done=completed, pages_known=len(stored_pages), chunks=chunks)

        for start in range(0, len(pending), _PAGE_BATCH_SIZE):
            batch = pending[start:start + _PAGE_BATCH_SIZE]
            count = vectorstore.index_documents(
                chatbot_id,
                _records(batch),
                chunk_fn=lambda text: chunk_text(text, chunk_size=_settings.chunk_size, overlap=_settings.chunk_overlap),
                version=target_version,
            )
            ordinals = [page["ordinal"] for page in batch]
            db.mark_ingestion_pages_done(run_id, ordinals)
            completed += len(batch)
            chunks += count
            last_url = batch[-1]["url"]
            db.update_ingestion_run(run_id, pages_done=completed, chunks_done=chunks, last_url=last_url)
            _set(chatbot_id, phase="embedding", pages_done=completed, pages_known=len(stored_pages), chunks=chunks, last_url=last_url)

        if chunks == 0:
            raise RuntimeError(f"No content chunks generated for {domain}")

        # Publish only after all target-version batches are present. Marking the
        # metadata ready first could expose a version before its vectors exist.
        vectorstore.get_vector_store().publish_version(chatbot_id, target_version, previous_version)
        db.update_knowledge(chatbot_id, knowledge_base_version=target_version, knowledge_status="ready", crawl_status="READY", pages_crawled=len(stored_pages), total_chunks=chunks, total_tokens=total_tokens, content_hash=digest, last_crawled_at=db.now(), crawl_completed_at=db.now(), embedding_provider=_settings.openai_embedding_model, source_provider=provider)
        db.finish_ingestion_run(run_id)
        _set(chatbot_id, phase="ready", pages_done=len(stored_pages), pages_known=len(stored_pages), chunks=chunks)
    except Exception as exc:
        logger.exception("Indexing failed for %s", chatbot_id)
        if run_id:
            db.finish_ingestion_run(run_id, status="failed", error_details=str(exc)[:1000])
        db.update_knowledge(chatbot_id, knowledge_status="failed", crawl_status="FAILED", crawl_completed_at=db.now(), error_details=str(exc)[:1000])
        _set(chatbot_id, phase="error")
    finally:
        with _lock:
            _in_progress.discard(chatbot_id)


def ensure_indexed(chatbot_id: str, domain: str, *, force=False) -> str:
    kb = db.knowledge(chatbot_id)
    if not force and kb and kb["knowledge_status"] == "ready" and vectorstore.is_indexed(chatbot_id):
        return "ready"
    if is_indexing(chatbot_id):
        return "indexing"
    threading.Thread(target=_run, args=(chatbot_id, domain, force), daemon=True).start()
    return "started"

"""Explicit, cached indexing. Chat never calls this module."""
from __future__ import annotations
import hashlib, logging, threading
from .config import get_settings
from .extractor import get_website_extractor
from .chunking import chunk_text
from . import db, vectorstore
logger = logging.getLogger(__name__); _settings = get_settings(); _in_progress: set[str] = set(); _lock = threading.Lock(); _progress: dict[str, dict] = {}

def is_indexing(chatbot_id: str) -> bool:
    with _lock: return chatbot_id in _in_progress

def _set(chatbot_id, **fields):
    with _lock: _progress.setdefault(chatbot_id, {"phase":"idle","pages_done":0,"pages_known":0,"chunks":0,"last_url":"","crawl_metrics":{}}).update(fields)

def get_progress(chatbot_id):
    with _lock:
        value = dict(_progress.get(chatbot_id, {"phase":"idle","pages_done":0,"pages_known":0,"chunks":0,"last_url":"","crawl_metrics":{}})); value["indexing"] = chatbot_id in _in_progress; return value

def _run(chatbot_id: str, domain: str, force: bool = False):
    with _lock:
        if chatbot_id in _in_progress: return
        _in_progress.add(chatbot_id)
    started_at = db.now()
    db.update_knowledge(chatbot_id, knowledge_status="processing", crawl_status="EXTRACTING", crawl_started_at=started_at, error_details="", source_provider="CONTEXT_DEV")
    _set(chatbot_id, phase="crawling", pages_done=0, pages_known=0, chunks=0, last_url="")
    try:
        extraction = get_website_extractor(force_recrawl=force).extract_knowledge(domain)
        pages = extraction.pages
        _set(chatbot_id, crawl_metrics={"source_provider": extraction.provider, "raw_pages": extraction.raw_pages, "total_pages": len(pages)}, pages_known=len(pages))
        if not pages:
            raise RuntimeError(f"No usable pages extracted for {domain}")
        records = [{"url": p.url, "title": p.title, "text": p.text} for p in pages]
        total_tokens = sum(max(1, len(record["text"]) // 4) for record in records)
        digest = db.content_hash(records); old = db.knowledge(chatbot_id)
        if old and old["content_hash"] == digest and vectorstore.is_indexed(chatbot_id):
            db.update_knowledge(chatbot_id, knowledge_status="ready", crawl_status="READY", pages_crawled=len(pages), total_tokens=total_tokens, total_chunks=vectorstore.count(chatbot_id), last_crawled_at=db.now(), crawl_completed_at=db.now(), embedding_provider=_settings.openai_embedding_model, source_provider=extraction.provider)
            _set(chatbot_id, phase="ready", chunks=vectorstore.count(chatbot_id)); return
        db.update_knowledge(chatbot_id, crawl_status="GENERATING_EMBEDDINGS")
        _set(chatbot_id, phase="embedding", pages_done=len(pages))
        version = (old["knowledge_base_version"] if old else 0) + 1
        count = vectorstore.index_documents(chatbot_id, records, chunk_fn=lambda text: chunk_text(text, chunk_size=_settings.chunk_size, overlap=_settings.chunk_overlap), version=version)
        if count == 0:
            raise RuntimeError(f"No content chunks generated for {domain}")
        db.update_knowledge(chatbot_id, knowledge_status="ready", crawl_status="READY", knowledge_base_version=version, pages_crawled=len(pages), total_chunks=count, total_tokens=total_tokens, content_hash=digest, last_crawled_at=db.now(), crawl_completed_at=db.now(), embedding_provider=_settings.openai_embedding_model, source_provider=extraction.provider)
        _set(chatbot_id, phase="ready", chunks=count)
    except Exception as exc:
        logger.exception("Indexing failed for %s", chatbot_id); db.update_knowledge(chatbot_id, knowledge_status="failed", crawl_status="FAILED", crawl_completed_at=db.now(), error_details=str(exc)[:1000]); _set(chatbot_id, phase="error")
    finally:
        with _lock: _in_progress.discard(chatbot_id)

def ensure_indexed(chatbot_id: str, domain: str, *, force=False) -> str:
    kb = db.knowledge(chatbot_id)
    if not force and kb and kb["knowledge_status"] == "ready" and vectorstore.is_indexed(chatbot_id): return "ready"
    if is_indexing(chatbot_id): return "indexing"
    threading.Thread(target=_run, args=(chatbot_id, domain, force), daemon=True).start(); return "started"

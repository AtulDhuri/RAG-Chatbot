"""ChromaDB-backed vector store with per-domain collections.

Each domain's chunks live in their own collection so retrieval is isolated
between tenants. Embeddings are produced by OpenAI.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time

# Disable ChromaDB's PostHog telemetry *before* importing chromadb. Otherwise an
# incompatible posthog version spams "capture() takes 1 positional argument but
# 3 were given" errors on every collection op. Must be set prior to import.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import chromadb
from chromadb.config import Settings as ChromaSettings
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
_client = chromadb.PersistentClient(
    path=_settings.chroma_persist_dir,
    settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
)
_openai = OpenAI(api_key=_settings.openai_api_key)

# Metadata key, on a per-collection basis, recording the last crawl time.
_META_KEY = "last_indexed_at"


def _collection_name(domain: str) -> str:
    """Chroma collection names must be 3-63 chars, alnum/._- and start/end alnum."""
    safe = re.sub(r"[^a-zA-Z0-9]", "-", domain).strip("-")
    digest = hashlib.sha1(domain.encode()).hexdigest()[:8]
    base = f"d-{safe}-{digest}"
    return base[:63]


def _get_collection(domain: str):
    return _client.get_or_create_collection(
        name=_collection_name(domain),
        metadata={"hnsw:space": "cosine", "domain": domain},
    )


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
def _embed(texts: list[str]) -> list[list[float]]:
    resp = _openai.embeddings.create(
        model=_settings.openai_embedding_model, input=texts
    )
    return [d.embedding for d in resp.data]


def _embed_batched(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        out.extend(_embed(texts[i : i + batch_size]))
    return out


def is_indexed(domain: str) -> bool:
    return _get_collection(domain).count() > 0


def last_indexed_at(domain: str) -> float:
    """Unix timestamp of the last successful index, or 0.0 if never."""
    coll = _get_collection(domain)
    meta = coll.metadata or {}
    try:
        return float(meta.get(_META_KEY, 0.0))
    except (TypeError, ValueError):
        return 0.0


def is_fresh(domain: str, ttl_seconds: int) -> bool:
    return is_indexed(domain) and (time.time() - last_indexed_at(domain)) < ttl_seconds


def index_documents(
    domain: str, records: list[dict], *, chunk_fn
) -> int:
    """Replace a domain's content with freshly chunked + embedded records.

    ``records`` is a list of {"url", "title", "text"}. ``chunk_fn(text)`` returns
    a list of chunk strings. Returns the number of chunks stored.
    """
    name = _collection_name(domain)
    # Fresh index: drop any prior collection so stale content doesn't linger.
    try:
        _client.delete_collection(name)
    except Exception:
        pass

    coll = _client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "domain": domain, _META_KEY: time.time()},
    )

    ids: list[str] = []
    docs: list[str] = []
    metadatas: list[dict] = []

    for rec in records:
        chunks = chunk_fn(rec["text"])
        for i, chunk in enumerate(chunks):
            uid = hashlib.sha1(f"{rec['url']}#{i}".encode()).hexdigest()
            ids.append(uid)
            docs.append(chunk)
            metadatas.append({"url": rec["url"], "title": rec.get("title", "")})

    if not docs:
        return 0

    embeddings = _embed_batched(docs)
    # Chroma caps add() batch size; insert in slices.
    batch = 256
    for i in range(0, len(docs), batch):
        coll.add(
            ids=ids[i : i + batch],
            documents=docs[i : i + batch],
            embeddings=embeddings[i : i + batch],
            metadatas=metadatas[i : i + batch],
        )

    logger.info("Indexed %d chunks for %s", len(docs), domain)
    return len(docs)


def query(domain: str, question: str, *, top_k: int) -> list[dict]:
    """Return the top-k relevant chunks for a question as
    [{"text", "url", "title", "score"}]."""
    coll = _get_collection(domain)
    if coll.count() == 0:
        return []

    q_emb = _embed([question])[0]
    res = coll.query(
        query_embeddings=[q_emb],
        n_results=min(top_k, coll.count()),
        include=["documents", "metadatas", "distances"],
    )

    hits: list[dict] = []
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        hits.append(
            {
                "text": doc,
                "url": (meta or {}).get("url", ""),
                "title": (meta or {}).get("title", ""),
                "score": 1.0 - float(dist),  # cosine distance -> similarity
            }
        )
    return hits

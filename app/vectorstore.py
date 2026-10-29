"""Tenant-isolated vector storage with pgvector production and Chroma local backends."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
_settings = get_settings()
logger = logging.getLogger(__name__)
_openai = OpenAI(api_key=_settings.openai_api_key)
_USE_PGVECTOR = _settings.vector_backend.lower() == "pgvector" or (
    _settings.vector_backend.lower() == "auto" and bool(_settings.database_url)
)

if not _USE_PGVECTOR:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _client = chromadb.PersistentClient(
        path=_settings.chroma_persist_dir,
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )

_META_KEY = "last_indexed_at"


def _collection_name(domain: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "-", domain).strip("-")
    digest = hashlib.sha1(domain.encode()).hexdigest()[:8]
    return f"d-{safe}-{digest}"[:63]


def _get_collection(domain: str):
    return _client.get_or_create_collection(
        name=_collection_name(domain), metadata={"hnsw:space": "cosine", "domain": domain}
    )


def _pg_conn():
    import psycopg
    from pgvector.psycopg import register_vector
    conn = psycopg.connect(_settings.database_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)
    _ensure_pg_schema(conn)
    return conn


def _ensure_pg_schema(conn) -> None:
    dim = int(_settings.embedding_dim)
    if dim < 1 or dim > 8192:
        raise ValueError("EMBEDDING_DIM must be between 1 and 8192")
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("CREATE TABLE IF NOT EXISTS vector_indexes (domain TEXT PRIMARY KEY, active_version UUID, indexed_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute(f"CREATE TABLE IF NOT EXISTS vector_documents (id BIGSERIAL PRIMARY KEY, domain TEXT NOT NULL, version UUID NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, embedding vector({dim}) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS vector_documents_domain_version_idx ON vector_documents(domain, version)")
        cur.execute("CREATE INDEX IF NOT EXISTS vector_documents_embedding_idx ON vector_documents USING hnsw (embedding vector_cosine_ops)")
    conn.commit()


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
def _embed(texts: list[str]) -> list[list[float]]:
    resp = _openai.embeddings.create(model=_settings.openai_embedding_model, input=texts)
    embeddings = [d.embedding for d in resp.data]
    if any(len(item) != _settings.embedding_dim for item in embeddings):
        raise ValueError("Embedding provider dimension does not match EMBEDDING_DIM")
    return embeddings


def _embed_batched(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    output: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        output.extend(_embed(texts[i:i + batch_size]))
    return output


def _pg_is_indexed(domain: str) -> bool:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM vector_indexes i JOIN vector_documents d ON d.domain=i.domain AND d.version=i.active_version WHERE i.domain=%s)", (domain,))
        return bool(cur.fetchone()[0])


def is_indexed(domain: str) -> bool:
    if _USE_PGVECTOR:
        return _pg_is_indexed(domain)
    return _get_collection(domain).count() > 0


def last_indexed_at(domain: str) -> float:
    if _USE_PGVECTOR:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM indexed_at) FROM vector_indexes WHERE domain=%s", (domain,))
            row = cur.fetchone()
        return float(row[0]) if row and row[0] else 0.0
    meta = _get_collection(domain).metadata or {}
    try:
        return float(meta.get(_META_KEY, 0.0))
    except (TypeError, ValueError):
        return 0.0


def is_fresh(domain: str, ttl_seconds: int) -> bool:
    return is_indexed(domain) and (time.time() - last_indexed_at(domain)) < ttl_seconds


def _records(records: list[dict], chunk_fn) -> tuple[list[str], list[dict]]:
    docs: list[str] = []
    metadata: list[dict] = []
    for rec in records:
        for chunk in chunk_fn(rec["text"]):
            docs.append(chunk)
            metadata.append({"url": rec["url"], "title": rec.get("title", "")})
    return docs, metadata


def _index_pg(domain: str, docs: list[str], metadata: list[dict]) -> int:
    if not docs:
        return 0
    embeddings = _embed_batched(docs)
    version = uuid.uuid4()
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO vector_documents(domain,version,url,title,content,embedding) VALUES (%s,%s,%s,%s,%s,%s)", [(domain, version, m["url"], m["title"], doc, emb) for doc, m, emb in zip(docs, metadata, embeddings)])
        cur.execute("SELECT active_version FROM vector_indexes WHERE domain=%s FOR UPDATE", (domain,))
        old = cur.fetchone()
        cur.execute("INSERT INTO vector_indexes(domain,active_version,indexed_at) VALUES (%s,%s,now()) ON CONFLICT(domain) DO UPDATE SET active_version=EXCLUDED.active_version,indexed_at=EXCLUDED.indexed_at", (domain, version))
        if old and old[0]:
            cur.execute("DELETE FROM vector_documents WHERE domain=%s AND version=%s", (domain, old[0]))
    logger.info("Indexed %d chunks for %s with pgvector", len(docs), domain)
    return len(docs)


def index_documents(domain: str, records: list[dict], *, chunk_fn) -> int:
    docs, metadata = _records(records, chunk_fn)
    if _USE_PGVECTOR:
        return _index_pg(domain, docs, metadata)
    if not docs:
        return 0
    name = _collection_name(domain)
    # Build the replacement before deleting the active collection where possible.
    embeddings = _embed_batched(docs)
    try:
        _client.delete_collection(name)
    except Exception:
        pass
    coll = _client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine", "domain": domain, _META_KEY: time.time()})
    for i in range(0, len(docs), 256):
        coll.add(ids=[hashlib.sha1(f"{m['url']}#{i+j}".encode()).hexdigest() for j, m in enumerate(metadata[i:i+256])], documents=docs[i:i+256], embeddings=embeddings[i:i+256], metadatas=metadata[i:i+256])
    return len(docs)


def query(domain: str, question: str, *, top_k: int) -> list[dict]:
    if _USE_PGVECTOR:
        q_emb = _embed([question])[0]
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT d.content,d.url,d.title,1-(d.embedding <=> %s::vector) AS score FROM vector_documents d JOIN vector_indexes i ON i.domain=d.domain AND i.active_version=d.version WHERE d.domain=%s ORDER BY d.embedding <=> %s::vector LIMIT %s", (q_emb, domain, q_emb, max(1, top_k)))
            return [{"text": r[0], "url": r[1], "title": r[2], "score": float(r[3])} for r in cur.fetchall()]
    coll = _get_collection(domain)
    count = coll.count()
    if count == 0:
        return []
    res = coll.query(query_embeddings=[_embed([question])[0]], n_results=min(top_k, count), include=["documents", "metadatas", "distances"])
    return [{"text": doc, "url": (meta or {}).get("url", ""), "title": (meta or {}).get("title", ""), "score": 1.0 - float(dist)} for doc, meta, dist in zip(res.get("documents", [[]])[0], res.get("metadatas", [[]])[0], res.get("distances", [[]])[0])]

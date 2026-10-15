"""Postgres + pgvector vector-store backend (Supabase).

This is the durable, multi-tenant backend. Unlike the local ChromaDB backend,
it survives redeploys and is safe to share across multiple app replicas, which
is what a plugin product with many tenant sites needs.

Multi-tenancy
-------------
Every row carries a ``tenant`` column (the normalized domain, e.g.
``example.com``). All reads and writes are scoped by it, so one tenant's content
is isolated from another's and a single tenant can be reindexed or deleted
without touching the rest. The schema lives in ``app/sql/schema.sql`` and is
applied automatically on first use (idempotent ``CREATE ... IF NOT EXISTS``).

Schema
------
- ``rag_documents(tenant, url, title, content_hash, last_indexed_at)`` - one row
  per crawled page, keyed by (tenant, url).
- ``rag_chunks(id, tenant, url, chunk_index, text, embedding vector(N), title)``
  - the embedded chunks, with an HNSW cosine index for fast ANN search and a
  btree index on ``tenant`` for scoping.

Content-hash dedup
------------------
On reindex we compute a SHA-256 over each page's ``chunk_fn`` output. If a
page's hash is unchanged since the last crawl, we keep its existing chunks and
embeddings instead of re-embedding identical text - a direct saving on OpenAI
embedding spend for every re-crawl. Pages whose hash changed (or are new) are
re-embedded; pages that disappeared from the crawl are deleted.

Public interface (matches app/vectorstore.py)
---------------------------------------------
    is_indexed(domain) -> bool
    last_indexed_at(domain) -> float
    is_fresh(domain, ttl_seconds) -> bool
    index_documents(domain, records, *, chunk_fn) -> int
    query(domain, question, *, top_k) -> list[dict]
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from .config import get_settings
from . import embeddings

logger = logging.getLogger(__name__)
_settings = get_settings()

_SCHEMA_PATH = Path(__file__).parent / "sql" / "schema.sql"

# One shared connection guarded by a lock. The indexer runs crawls in worker
# threads, and psycopg3 connections are not thread-safe for concurrent use, so
# we serialize access. For higher throughput this can later become a pool.
_conn: psycopg.Connection | None = None
_conn_lock = threading.Lock()
_schema_ready = False


class PgVectorError(RuntimeError):
    """Raised when the pgvector backend cannot complete an operation."""


def is_available() -> bool:
    """True when a Postgres connection string is configured."""
    return bool(_settings.database_url)


def _connect() -> psycopg.Connection:
    global _conn, _schema_ready
    if _conn is not None and not _conn.closed:
        return _conn

    if not is_available():
        raise PgVectorError(
            "DATABASE_URL is not set; cannot use the pgvector backend."
        )

    # autocommit keeps each statement simple; we wrap reindex in explicit
    # transactions where atomicity matters.
    #
    # prepare_threshold=None disables server-side prepared statements. Supabase's
    # transaction pooler (port 6543) multiplexes connections and does not support
    # prepared statements; leaving them on raises
    # "prepared statement does not exist". Disabling is also harmless on the
    # session pooler (5432) and direct connections.
    _conn = psycopg.connect(
        _settings.database_url, autocommit=True, prepare_threshold=None
    )
    _ensure_schema(_conn)
    register_vector(_conn)
    _schema_ready = True
    return _conn


def _ensure_schema(conn: psycopg.Connection) -> None:
    """Create the extension, tables, and indexes if they don't exist yet.

    The embedding dimension is injected from settings so the vector column
    matches the embedding model.
    """
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    sql = sql.replace("{{EMBEDDING_DIM}}", str(_settings.embedding_dim))
    with conn.cursor() as cur:
        cur.execute(sql)


def _hash_chunks(chunks: list[str]) -> str:
    """Stable hash of a page's chunk list, for change detection."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.encode("utf-8"))
        h.update(b"\x00")  # separator so [ab,c] != [a,bc]
    return h.hexdigest()


def is_indexed(domain: str) -> bool:
    with _conn_lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM rag_chunks WHERE tenant = %s LIMIT 1", (domain,)
            )
            return cur.fetchone() is not None


def last_indexed_at(domain: str) -> float:
    """Most recent page index time for a tenant, as a unix timestamp (0 if none)."""
    with _conn_lock:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM MAX(last_indexed_at)) "
                "FROM rag_documents WHERE tenant = %s",
                (domain,),
            )
            row = cur.fetchone()
    if not row or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return 0.0


def is_fresh(domain: str, ttl_seconds: int) -> bool:
    return is_indexed(domain) and (time.time() - last_indexed_at(domain)) < ttl_seconds


def index_documents(domain: str, records: list[dict], *, chunk_fn) -> int:
    """(Re)index a tenant's pages, re-embedding only changed content.

    ``records`` is a list of {"url", "title", "text"}. ``chunk_fn(text)`` returns
    a list of chunk strings. Returns the total chunk count stored for the tenant.
    """
    with _conn_lock:
        conn = _connect()

        # Existing per-page hashes, so we can skip unchanged pages.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, content_hash FROM rag_documents WHERE tenant = %s",
                (domain,),
            )
            existing_hashes = {url: h for url, h in cur.fetchall()}

        seen_urls: set[str] = set()
        reused_pages = 0
        embedded_pages = 0
        now = time.time()

        for rec in records:
            url = rec["url"]
            title = rec.get("title", "")
            seen_urls.add(url)

            chunks = chunk_fn(rec["text"])
            if not chunks:
                continue
            new_hash = _hash_chunks(chunks)

            # Unchanged page: refresh its timestamp/title, keep its chunks.
            if existing_hashes.get(url) == new_hash:
                reused_pages += 1
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE rag_documents SET last_indexed_at = to_timestamp(%s), "
                        "title = %s WHERE tenant = %s AND url = %s",
                        (now, title, domain, url),
                    )
                continue

            # Changed or new page: embed and replace its chunks atomically.
            vectors = embeddings.embed_batched(chunks)
            embedded_pages += 1
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM rag_chunks WHERE tenant = %s AND url = %s",
                        (domain, url),
                    )
                    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                        cur.execute(
                            "INSERT INTO rag_chunks "
                            "(tenant, url, chunk_index, text, title, embedding) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (domain, url, i, chunk, title, vec),
                        )
                    cur.execute(
                        "INSERT INTO rag_documents "
                        "(tenant, url, title, content_hash, last_indexed_at) "
                        "VALUES (%s, %s, %s, %s, to_timestamp(%s)) "
                        "ON CONFLICT (tenant, url) DO UPDATE SET "
                        "title = EXCLUDED.title, "
                        "content_hash = EXCLUDED.content_hash, "
                        "last_indexed_at = EXCLUDED.last_indexed_at",
                        (domain, url, title, new_hash, now),
                    )

        # Drop pages that vanished from this crawl so stale content doesn't linger.
        stale = set(existing_hashes) - seen_urls
        if stale:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM rag_chunks WHERE tenant = %s AND url = ANY(%s)",
                        (domain, list(stale)),
                    )
                    cur.execute(
                        "DELETE FROM rag_documents WHERE tenant = %s AND url = ANY(%s)",
                        (domain, list(stale)),
                    )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM rag_chunks WHERE tenant = %s", (domain,)
            )
            total = cur.fetchone()[0]

    logger.info(
        "pgvector index for %s: %d chunks total (%d pages embedded, %d reused, "
        "%d stale pages removed)",
        domain, total, embedded_pages, reused_pages, len(stale),
    )
    return total


def query(domain: str, question: str, *, top_k: int) -> list[dict]:
    """Return top-k relevant chunks for a tenant as
    [{"text", "url", "title", "score"}], ordered by cosine similarity."""
    q_emb = embeddings.embed([question])[0]
    with _conn_lock:
        conn = _connect()
        with conn.cursor() as cur:
            # `<=>` is pgvector's cosine distance; similarity = 1 - distance.
            # Cast the query parameter to `vector` explicitly: a bare bound list
            # arrives as double precision[], which has no `<=>` operator. We pass
            # the embedding as a string so the ::vector cast parses it.
            q_vec = "[" + ",".join(repr(float(x)) for x in q_emb) + "]"
            cur.execute(
                "SELECT text, url, title, 1 - (embedding <=> %s::vector) AS score "
                "FROM rag_chunks WHERE tenant = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (q_vec, domain, q_vec, top_k),
            )
            rows = cur.fetchall()

    return [
        {"text": text, "url": url or "", "title": title or "", "score": float(score)}
        for (text, url, title, score) in rows
    ]

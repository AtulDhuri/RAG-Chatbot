-- Multi-tenant RAG vector store on Postgres + pgvector (Supabase).
-- Applied automatically on first use by app/vectorstore_pg.py. Idempotent:
-- safe to run repeatedly. {{EMBEDDING_DIM}} is substituted at runtime from
-- settings.embedding_dim (1536 for text-embedding-3-small).

-- pgvector extension (preinstalled/available on Supabase).
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per crawled page, per tenant. content_hash drives re-embedding dedup.
CREATE TABLE IF NOT EXISTS rag_documents (
    tenant          TEXT        NOT NULL,
    url             TEXT        NOT NULL,
    title           TEXT        NOT NULL DEFAULT '',
    content_hash    TEXT        NOT NULL,
    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, url)
);

-- Embedded chunks. One page expands into many chunks.
CREATE TABLE IF NOT EXISTS rag_chunks (
    id          BIGSERIAL PRIMARY KEY,
    tenant      TEXT      NOT NULL,
    url         TEXT      NOT NULL,
    chunk_index INTEGER   NOT NULL,
    text        TEXT      NOT NULL,
    title       TEXT      NOT NULL DEFAULT '',
    embedding   vector({{EMBEDDING_DIM}}) NOT NULL
);

-- Scope every query by tenant cheaply.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant ON rag_chunks (tenant);

-- Fast per-page replace during reindex.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant_url ON rag_chunks (tenant, url);

-- Approximate-nearest-neighbor index for cosine similarity search.
-- HNSW gives good recall/latency without a training step. vector_cosine_ops
-- matches the `<=>` cosine-distance operator used in queries.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedding_hnsw
    ON rag_chunks USING hnsw (embedding vector_cosine_ops);

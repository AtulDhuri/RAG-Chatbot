# Agent conventions

Guidance for AI agents and developers working in this repo. Keep this in sync
with `AGENTS.md` and `.cursor/rules/context-dev.mdc` (same content).

## Project

A plugin RAG chatbot. A site owner drops `widget.js` on their page; the backend
scans their site, embeds the content, and answers visitor questions using that
content as the retrieval source.

- Language / framework: Python 3 + FastAPI (`app/`)
- Package manager: `pip` (`requirements.txt`), virtualenv in `.venv`
- Config / secrets: `.env`, loaded via `pydantic-settings` in `app/config.py`.
  `.env` is gitignored; `.env.example` documents every variable.
- Ingest pipeline: `indexer.reindex` → `crawler.crawl_domain` →
  `chunking.chunk_text` → `vectorstore.index_documents` (OpenAI embeddings;
  pluggable vector backend — Supabase/pgvector in prod, ChromaDB for local dev).

## Vector storage (multi-tenant)

Chunks + embeddings persist behind one facade, `app/vectorstore.py`, which
delegates to a selectable backend. The pipeline (`indexer`, `rag`, `main`) only
ever imports `vectorstore`, never a specific backend — so storage is swappable
without touching the app, the same pattern as the crawl backend.

- `pgvector` — `app/vectorstore_pg.py`, Postgres + pgvector on **Supabase**.
  Durable, multi-tenant, survives redeploys and multiple replicas. **Use in
  production.**
- `chroma` — `app/vectorstore_chroma.py`, local on-disk ChromaDB. Zero infra,
  good for local dev, ephemeral in containers.

Selection via `VECTOR_BACKEND`: `pgvector` | `chroma` | `auto` (pgvector when
`DATABASE_URL` is set, else chroma).

### Rules

1. **Keep the facade stable.** The public interface is `is_indexed`,
   `last_indexed_at`, `is_fresh`, `index_documents(domain, records, *, chunk_fn)`,
   `query(domain, question, *, top_k)`. Any new backend must implement exactly
   these. Don't import a backend module directly from the pipeline.
2. **Embeddings live in `app/embeddings.py`** so every backend produces
   identical vectors. `EMBEDDING_DIM` (1536 for `text-embedding-3-small`) must
   match the model and the pgvector column.
3. **Multi-tenancy = the `tenant` column** (normalized domain). Every read/write
   is scoped by it; never query across tenants.
4. **`DATABASE_URL` is a server-side secret** (Supabase). Prefer the connection
   pooler URL (port 6543). Never ship it to the browser.
5. **Dedup is intentional.** `index_documents` skips re-embedding pages whose
   content hash is unchanged. Preserve this when editing the pgvector backend.

### Schema — `app/sql/schema.sql`

Applied automatically on first use (idempotent). `rag_documents(tenant, url,
title, content_hash, last_indexed_at)` and `rag_chunks(id, tenant, url,
chunk_index, text, title, embedding vector(EMBEDDING_DIM))`, with a btree index
on `tenant` and an HNSW `vector_cosine_ops` index on `embedding`.

### Environment

```env
VECTOR_BACKEND=auto            # pgvector | chroma | auto
DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres
EMBEDDING_DIM=1536
```

## Context.dev integration

[Context.dev](https://docs.context.dev) is the web-data provider used to **scan
sites** (the RAG source). One API key covers scraping, crawling, search, and
more. Base URL: `https://api.context.dev/v1`. Auth: `Authorization: Bearer
$CONTEXT_DEV_API_KEY` on every request.

### Rules — read before touching Context.dev code

1. **One wrapper, one import.** All Context.dev calls go through
   **`app/context_dev.py`**. Do not import the `context.dev` SDK anywhere else.
   Add any new endpoint as a function in that module.
2. **Secret stays server-side.** The key is read from the environment
   (`CONTEXT_DEV_API_KEY`, surfaced as `settings.context_dev_api_key`). Never
   hardcode it and never let it reach the browser bundle (`app/static/*`).
3. **Read the docs page before coding** against any endpoint. Append `.md` to a
   docs URL for plain markdown (e.g. `curl -s https://docs.context.dev/quickstart.md`).
4. **Errors:** honor `Retry-After` on `429`; retry `5xx` with bounded backoff;
   never retry validation (`4xx`) errors. The wrapper already does this.
5. **Caching:** most endpoints serve cached results by default. Pass
   `max_age_ms` explicitly (`0` = fresh) when the goal needs current data.
6. **Credits cost money.** Keep automated tests off the live API — mock
   `app.context_dev`. For a manual check, make one targeted call, never a loop.
   Past a few hundred URLs use the Batch API instead of looping crawls/scrapes.

### Environment

```env
# server-side secret; rotate at https://www.context.dev/dashboard/api-keys
CONTEXT_DEV_API_KEY=ctxt_secret_...
# crawl backend: auto | context_dev | playwright
CRAWL_BACKEND=auto
```

### Wrapper module — `app/context_dev.py`

- `is_available() -> bool` — true when a key is configured.
- `crawl_site(start_url, *, max_pages, max_age_ms=None, use_main_content_only=True, follow_subdomains=True, progress_cb=None) -> list[Page]`
  — scans a site and returns `Page(url, title, text)` objects (one per page,
  content as Markdown). This is the shape `vectorstore.index_documents` consumes.
- `scrape_urls(urls, *, max_age_ms=None, progress_cb=None) -> list[Page]`
  — scrape an explicit list of URLs to Markdown (used after discovery).
- `ContextDevError` — raised on non-retryable failures / exhausted retries.

### Discovery cascade (how `crawl_site` finds pages)

Site scanning is NOT a plain crawl. Many real sites (JS-rendered nav, WAF/bot
protection, sitemap-only inner pages) strand a link-following crawl on the
homepage. `crawl_site` tries, in order, and stops at the first that yields
multiple pages:

1. **WordPress REST API** — `/wp-json/wp/v2/pages` + `/posts` via Context.dev
   scrape; parse JSON, collect each `link`.
2. **Sitemap** — `/sitemap_index.xml`, `/sitemap.xml`, `/wp-sitemap.xml`;
   follow one level of sitemap-index nesting, extract `<loc>` URLs.
3. **Link-following crawl** — `POST /web/crawl` (`_crawl_link_following`), the
   general fallback for sites with crawlable navigation.

For (1) and (2), the discovered URLs are then scraped individually
(`scrape_urls`, "Option B") for faithful, JS-rendered Markdown. All discovery
fetches go through Context.dev's browser, so **WAF 403s that block plain HTTP
clients are bypassed**. Results are capped at `max_pages` (`CRAWL_MAX_PAGES`).

Error handling: 429 honors `Retry-After`; 5xx and transport errors (connection
drops / timeouts, which carry no HTTP status) retry with bounded backoff;
validation (4xx) errors never retry.

### Backend dispatch — `app/crawler.py`

`crawl_domain(...)` dispatches by `CRAWL_BACKEND`:
- `auto` (default): use Context.dev when a key is present, fall back to the
  local Playwright crawler on error or empty result.
- `context_dev`: force the hosted crawler.
- `playwright`: force the local headless-Chromium crawler.

The indexer calls `crawl_domain` unchanged; it does not know which backend ran.

### Endpoints in use

| Endpoint | Method | Why | Docs |
| - | - | - | - |
| Crawl Website | `POST /web/crawl` | Link-following scan; Markdown per page | https://docs.context.dev/api-reference/web-scraping/crawl |
| Scrape | `POST /web/scrape` | Fetch one URL (discovery reads + per-URL content in Option B) | https://docs.context.dev/api-reference/web-scraping/scrape |

Endpoints to reach for next, through the same wrapper, if the goal grows:
- **Map URLs** `GET /web/urls` — hosted URL discovery (alternative to our
  sitemap parsing). https://docs.context.dev/api-reference/web-scraping/map
- **Monitors** `POST /monitors` — re-scan on a schedule and re-embed on change.
  https://docs.context.dev/monitors/overview
- **Batch API** `POST /batch/submit` — crawl/scrape beyond a few hundred URLs
  (use when a site exceeds ~100–500 pages). https://docs.context.dev/batches/overview

### Verified calls (proof)

- `crawl_site("https://example.com", max_pages=2, max_age_ms=0)` → 1 usable page
  of Markdown; chunks embedded via the pipeline.
- WAF-protected WordPress site (`drkrinitamotwani.com`): link-following crawl
  reached only the homepage (1 page, 17 skipped). The discovery cascade's
  sitemap step found **171 URLs**; a capped 30-page run scraped all 30 and
  indexed **661 chunks** into Supabase. Sitemap discovery (via Context.dev's
  browser) was the working path; raw HTTP to the same URLs returned 403.

### Troubleshooting & feedback

- Empty/failed call: https://docs.context.dev/optimization/troubleshooting
- Endpoint behaves differently than documented: report it via
  `POST /feedback` (0 credits) with the `request_id`.
  https://docs.context.dev/optimization/agent-feedback

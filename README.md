# RAG Website Chatbot

A multi-tenant Retrieval-Augmented Generation (RAG) chatbot. A site owner drops a single
`<script>` tag on their page. The script detects its own domain, the backend crawls that
website, embeds the content into a per-domain vector store, and answers visitor questions
using the site as the source of truth.

## How it works

```
Website page
  └─ <script src="https://your-host/widget.js"> ──┐
                                                  │ auto-detects its domain
                                                  ▼
                            POST /api/register  (crawl + embed the domain, once per TTL)
                            POST /api/chat       (retrieve top-k chunks + LLM answer)
                                                  ▼
      FastAPI backend ── Playwright crawler ── chunker ── OpenAI embeddings ── ChromaDB (one collection per domain)
                                                  │
                                   OpenAI chat model answers from retrieved context
```

Each domain gets its own ChromaDB collection, so one site's content never leaks into another's answers.

## Project layout

```
CHATBOT_RAG/
├─ app/
│  ├─ main.py          FastAPI app: /api/register, /api/chat, /widget.js
│  ├─ config.py        Env-based settings
│  ├─ crawler.py       Headless-browser crawler (Playwright): same-domain, robots-aware, page-capped
│  ├─ chunking.py      Splits page text into overlapping chunks
│  ├─ vectorstore.py   ChromaDB wrapper (per-domain collections) + OpenAI embeddings
│  ├─ rag.py           Retrieve + LLM answer pipeline
│  └─ static/
│     └─ widget.js     Embeddable floating chat bubble
├─ requirements.txt
├─ .env.example
└─ README.md
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # one-time: downloads the headless browser the crawler drives
Copy-Item .env.example .env   # then edit .env and add your OPENAI_API_KEY
```

> The crawler uses a headless Chromium (via Playwright) rather than a plain HTTP
> client. This is required for JavaScript-rendered sites and for sites behind a
> WAF that rejects non-browser TLS fingerprints. The `playwright install chromium`
> step is mandatory.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

## Embed on a website

Add this to the site's main page:

```html
<script src="https://your-host/widget.js" data-installation-token="YOUR_SITE_TOKEN" defer></script>
```

The widget auto-detects the page's domain, registers it (triggering a crawl on first use),
and renders a chat bubble in the bottom-right corner.

## API

### `POST /api/register`
Crawl and index a domain. Safe to call repeatedly; it re-crawls only after the TTL expires.

```json
{ "domain": "example.com" }
```

### `POST /api/chat`
Ask a question scoped to a domain.

```json
{ "domain": "example.com", "message": "What services do you offer?" }
```

Response:

```json
{ "answer": "...", "sources": ["https://example.com/services"], "status": "ready" }
```

If the domain hasn't been crawled yet, `status` is `"indexing"` and a crawl is kicked off.


## Subscriptions and installation tokens

The production flow uses Postgres when `DATABASE_URL` is configured. Create an account with `POST /api/auth/signup`, sign in with `POST /api/auth/login`, then create a website with authenticated `POST /api/sites`:

```json
{"domain":"example.com"}
```

Put the returned `installation_token` in the widget script as `data-installation-token`. The server resolves the website from the hashed token and never trusts a browser-supplied domain as authorization. Chat usage is reserved atomically per website and billing period. The seeded Starter plan allows 500 chats. An inactive subscription returns the exact message `Subscription ended, Please connect {domain}`. Razorpay subscriptions and signed, replay-safe subscription webhooks are enabled when the Razorpay settings are present.

Without `DATABASE_URL`, local development keeps the original domain-only demo flow and uses process-local auth/subscription state. That fallback is not durable or safe for multiple replicas; use Supabase/Postgres for production.

## Production deployment

Run the ordered database migrations before starting the API:

```powershell
$env:DATABASE_URL="postgresql://..."
python -m app.migrations
```

Set `VECTOR_BACKEND=pgvector` (or `auto` with `DATABASE_URL`) and `EMBEDDING_DIM` to match the configured embedding model. The pgvector adapter stages a new version and switches the active version only after all chunks and embeddings are inserted, so a failed re-index keeps the previous knowledge base available.

Configure Redis for shared cache, distributed rate limits, unauthorized-source blocking, and durable crawl jobs:

```text
REDIS_URL=redis://...
CACHE_TTL_SECONDS=86400
UNAUTHORIZED_THRESHOLD=50
UNAUTHORIZED_WINDOW_SECONDS=900
UNAUTHORIZED_BLOCK_SECONDS=3600
RATE_LIMIT_PER_MINUTE=60
```

Run the API and a worker as separate processes in production:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
python worker.py
```

When Redis is configured, crawl requests are deduplicated into the `rag:jobs:crawl` queue and the worker performs crawling, chunking, embedding, and version activation. Without Redis, local development uses the existing in-process thread fallback.

The project regression tests are under `tests/` and run with:

```powershell
python -m pytest -q
```

Do not commit `.env`. Rotate provider, database, billing, webhook, and administrator credentials if they have been exposed outside the deployment secret manager.

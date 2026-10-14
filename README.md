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
<script src="http://localhost:8000/widget.js" defer></script>
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

## Notes

- Prototype auth: open CORS + rate limiting. Add per-domain API keys before production.
- Provider is OpenAI, read from env; the vector store and LLM calls are isolated in
  `vectorstore.py` / `rag.py` so you can swap in Ollama or another provider later.

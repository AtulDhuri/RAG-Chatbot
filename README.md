# Secure SaaS RAG Chatbot

This service provides tenant-isolated website chatbots. Widget initialization validates the registered domain from browser `Origin` and optional `Referer` headers, confirms the chatbot and subscription are active, and issues a ten-minute in-memory widget session token. Chat and status require that token and repeat the subscription checks.

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
# Set OPENAI_API_KEY, JWT_SECRET, and ADMIN_API_KEY in .env
uvicorn app.main:app --port 8000
```

Use PostgreSQL for all application metadata. `DATABASE_URL` is required and must point to the PostgreSQL instance used by the deployment. The application fails startup if it is missing or not a PostgreSQL URL. Embeddings and knowledge chunks are stored in Supabase PostgreSQL using `pgvector`. The application does not depend on ChromaDB or local vector files.

The portal is available at `/admin`. Sign in with the configured `ADMIN_API_KEY`; the portal exchanges it for a short-lived admin JWT kept in session storage.
Admin calls require `X-Admin-Key`. Create a chatbot and start its one-time initial crawl:

```http
POST /api/admin/chatbots
X-Admin-Key: <admin secret>
Content-Type: application/json

{"chatbot_name":"Acme","customer_id":"customer-1","allowed_domain":"acme.example","subscription_start_date":"2026-01-01T00:00:00+00:00","subscription_end_date":"2027-01-01T00:00:00+00:00"}
```

Manual recrawling is explicit and authenticated:

```http
POST /api/admin/chatbots/{chatbot_id}/recrawl
X-Admin-Key: <admin secret>
```

The indexer stores a content hash. An unchanged recrawl skips embedding generation. Chat requests never crawl or re-embed. Knowledge is served only after a successful `ready` build.

## Widget installation

```html
<script src="https://yourdomain.com/widget.js" data-chatbot-id="CHATBOT_ID" defer></script>
```

The chatbot ID is public configuration, not an API key. No permanent credential is embedded. The backend trusts neither frontend subscription checks nor a client-supplied domain. Do not use the old domain-based `/api/register` flow; it has been removed.

## Security and operations

- All initialization failures return the same `403 {"success":false,"message":"Chatbot unavailable"}` response while writing an internal security log.
- The token is HS256-signed, bound to the chatbot ID, and expires after ten minutes.
- Subscriptions support arbitrary start/end dates, so monthly, quarterly, yearly, manual, and future auto-renewal updates require only metadata changes, not widget changes.
- Crawler targets reject private, loopback, link-local, multicast, reserved, and unresolved addresses.
- For multi-worker scale, migrate the metadata repository to PostgreSQL, move vectors to shared storage/pgvector, and replace process-local crawl threads with a durable queue while retaining the same API contract.

## Knowledge extraction behavior

The MVP uses Context.dev as the primary website knowledge extraction provider. Context.dev handles browser rendering, retries, proxies, and multi-page crawling for WordPress, Shopify, WooCommerce, Wix, Webflow, Squarespace, React, Next.js, Angular, Vue, Laravel, Django, and custom websites.

The application depends on the `WebsiteKnowledgeExtractor` interface in `app/extractor.py`. `ContextDevExtractor` adapts Context.dev results into normalized page content. The indexer then cleans and deduplicates content, creates chunks, generates embeddings, and stores source URLs and titles with every chunk. A future sitemap, Playwright, or hybrid extractor can be added without changing chatbot logic.

The database stores crawl status, start and completion times, page/chunk/token counts, knowledge version, embedding provider, source provider, and extraction errors. The status endpoint exposes these values while a crawl is running or after it completes.

Extraction is cached through the existing knowledge status and content hash. A ready chatbot is not recrawled when it loads. Initial crawling, customer recrawling, and administrator recrawling are explicit. Forced recrawls set Context.dev freshness to zero. Unchanged content skips embedding generation.

Context.dev configuration:

```text
CONTEXT_DEV_API_KEY=replace-with-context-dev-key
CONTEXT_DEV_BASE_URL=https://api.context.dev
CONTEXT_DEV_MAX_PAGES=100
CONTEXT_DEV_TIMEOUT=300
```

Context.dev returns page content as Markdown and page metadata. Its scrape API supports JavaScript rendering and managed retries/proxies. See the [Context.dev scrape documentation](https://branddev.mintlify.app/guides/scrape-websites-to-markdown) and [authentication guide](https://branddev.mintlify.app/guides/get-started/authentication).

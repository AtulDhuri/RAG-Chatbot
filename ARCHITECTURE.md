# Production SaaS architecture

This repository keeps the existing FastAPI service and adds the production boundaries needed for a small multi-tenant deployment. The installation token is the website credential; the authenticated account owns the tenant resources.

## Request path

```text
Widget -> early source block/rate limit -> installation-token + domain authorization
       -> FAQ lookup -> Redis response cache -> indexed vector search -> LLM
       -> tenant-scoped analytics and cache entry
```

A cache key is `tenant_id + website_id + normalized domain + language + SHA-256(normalized question)`. Answers store the response and sources. FAQ and cache hits happen before quota reservation, embedding lookup, or an LLM call. Re-indexing invalidates only that website's answer namespace.

## Persistence

PostgreSQL is the system of record for accounts, websites, subscriptions, usage, chat analytics, cached-answer metadata, and locked/pinned FAQs. `app/sql/subscriptions.sql` is the deployable idempotent schema, and the inline bootstrap schema is kept compatible for existing installations. Every tenant-owned table carries `account_id` or references a website owned by an account.

The current local vector adapter is Chroma with one collection per normalized domain. For production, run the vector adapter against shared pgvector or a managed vector service. A multi-replica deployment must not use local Chroma storage as its source of truth.

Redis is optional for local development and recommended in production. It stores response-cache values and is the natural location for distributed rate-limit counters and blocked-source state. The code falls back to a process-local cache only when Redis is not configured, which is not suitable for multiple replicas.

## API surface

- `POST /api/auth/signup`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- `POST /api/sites`, `POST /api/sites/{domain}/rotate-token`
- `POST /api/sites/{domain}/recrawl`, `GET /api/sites/{domain}/crawl-status`
- `POST /api/sites/{domain}/faqs`
- `POST /api/register`, `GET /api/status`, `POST /api/chat`
- `GET /api/usage`, `GET /api/analytics`
- Billing and signed webhook endpoints under `/api/billing/*`

Mutating dashboard routes use the account session. Widget routes require `X-Installation-Token`; domain-only access is rejected.

## Crawl and refresh strategy

The current crawl starts asynchronously from the web process and preserves the existing vector index until a successful crawl produces a replacement. It is same-origin, robots-aware, page-capped, and rejects private-network targets. The next production deployment step is to move `indexer.reindex` into a durable queue worker with leases, retries, cancellation, and persisted progress. BullMQ is a suitable choice when Redis is already operated; a Postgres job table is simpler if the deployment wants fewer moving parts.

A worker should persist `crawl_jobs` and page fingerprints (`etag`, `last_modified`, content hash, URL, and index version). It can then enqueue only new or changed pages, delete embeddings for removed pages, and invalidate the affected website's answer namespace after the new index becomes active. Failed jobs must leave the previous index active.

## Unauthorized traffic and rate limits

Invalid installation tokens are counted by source IP and normalized domain. Once the configurable threshold is reached, the source is rejected before tenant lookup for the temporary block duration. Configure `UNAUTHORIZED_THRESHOLD`, `UNAUTHORIZED_WINDOW_SECONDS`, and `UNAUTHORIZED_BLOCK_SECONDS`. Add Redis-backed counters at the edge for a multi-replica deployment.

Apply separate distributed buckets for IP, account, website, and installation token. Keep the web-process limiter as a last-resort protection, not as the only control. Aggregate unauthorized logs by source, domain, and time window rather than emitting one log event per request.

## Required production operations

- Set `DATABASE_URL`, `REDIS_URL`, a restrictive `ALLOWED_ORIGINS`, and provider secrets through the deployment secret manager.
- Rotate any credentials that were ever stored in a local `.env` or committed to source control.
- Use managed PostgreSQL with pgvector or implement the pgvector adapter before running more than one web replica.
- Run crawling and embeddings in dedicated workers with CPU/memory limits and a global tenant concurrency cap.
- Add OpenTelemetry/Prometheus metrics for cache hits, RAG latency, token usage, crawl progress, failed URLs, and unauthorized blocks.
- Add migrations and automated tests for tenant isolation, token rotation, quota races, cache key isolation, webhook replay, SSRF rejection, and failed-crawl preservation before launch.

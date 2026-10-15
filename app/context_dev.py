"""Single server-side wrapper around the Context.dev API.

Every Context.dev call in this project goes through this module. Nothing else
imports the SDK directly, so there is exactly one place that knows the API key,
the endpoints, retry/backoff rules, and how responses map onto our domain
objects.

Why Context.dev for this app
----------------------------
The chatbot is a plugin: a site owner drops ``widget.js`` on their page, we scan
the site, embed it, and answer visitor questions with that content as the RAG
source. Scanning a live site reliably is the hard part - many pages are
JavaScript-rendered or sit behind a WAF. Context.dev's hosted crawler runs a
real browser fleet and returns clean Markdown per page, which is exactly the
text we chunk and embed. That replaces the local Playwright crawler, which is
heavy to deploy (ships a headless Chromium) and brittle on protected sites.

Endpoint used
-------------
- Crawl Website  POST /web/crawl  (1 credit per page crawled)
  Docs: https://docs.context.dev/api-reference/web-scraping/crawl
  SDK:  client.web.web_crawl_md(...)

The wrapper returns ``list[Page]`` - the same (url, title, text) shape the
Playwright crawler produced - so the indexer pipeline is unchanged downstream.

Secret handling
---------------
``CONTEXT_DEV_API_KEY`` is a server-side secret. It is read from the environment
(the SDK reads it automatically; we also pass it explicitly from settings) and
must never reach the browser bundle.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from .config import get_settings

logger = logging.getLogger(__name__)
_settings = get_settings()

# Retry policy for transient failures (429 / 5xx), with bounded backoff.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 30.0


@dataclass
class Page:
    """A crawled page, matching the shape the indexer/vectorstore expect."""
    url: str
    title: str
    text: str


# Progress callback: (pages_done, pages_known, last_url). Mirrors the crawler's.
ProgressCallback = Callable[[int, int, str], None]


class ContextDevError(RuntimeError):
    """Raised when a Context.dev call fails in a way we can't recover from."""


def is_available() -> bool:
    """True when a Context.dev API key is configured."""
    return bool(_settings.context_dev_api_key)


_client = None


def _get_client():
    """Lazily build and cache the SDK client.

    Imported lazily so the module loads even if the SDK isn't installed; callers
    that don't use Context.dev never pay the import cost.
    """
    global _client
    if _client is not None:
        return _client

    if not is_available():
        raise ContextDevError(
            "CONTEXT_DEV_API_KEY is not set; cannot use the Context.dev crawler."
        )

    try:
        from context.dev import ContextDev
    except ImportError as exc:  # pragma: no cover - import guard
        raise ContextDevError(
            "The 'context.dev' SDK is not installed. Run: pip install context.dev"
        ) from exc

    _client = ContextDev(api_key=_settings.context_dev_api_key)
    return _client


def _status_of(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    return status


def _should_retry(exc: Exception) -> tuple[bool, Optional[float]]:
    """Decide whether to retry, and how long to wait first.

    Honors ``Retry-After`` on 429. Retries 5xx and transport-level errors
    (connection drops, timeouts) that carry no HTTP status. Never retries
    validation errors (4xx other than 429).
    """
    status = _status_of(exc)

    # Transport errors (ConnectionError, timeouts) surface with no status code.
    # These are transient — large JSON payloads can drop mid-read — so retry.
    if status is None:
        name = type(exc).__name__.lower()
        if "connect" in name or "timeout" in name or "apiconnection" in name:
            return True, None

    if status == 429:
        retry_after = None
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if headers is not None:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        return True, retry_after

    if status is not None and 500 <= status < 600:
        return True, None

    # Validation errors and other 4xx: do not retry.
    return False, None


def _backoff_seconds(attempt: int, retry_after: Optional[float]) -> float:
    if retry_after is not None:
        return min(retry_after, _BACKOFF_CAP_SECONDS)
    return min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)


def _crawl_once(
    url: str,
    *,
    max_pages: int,
    max_age_ms: Optional[int],
    use_main_content_only: bool,
    follow_subdomains: bool,
):
    """One raw crawl call. Separated so retry logic stays readable."""
    client = _get_client()
    kwargs: dict = {
        "url": url,
        "max_pages": max_pages,
        # Strip nav/headers/footers so embeddings capture real content, not
        # boilerplate repeated on every page.
        "use_main_content_only": use_main_content_only,
        # Treat www and apex as the same site, and follow subdomains. Without
        # this, a homepage served on the apex (example.com) whose links point to
        # www.example.com has those links treated as a different host and
        # skipped - which strands the crawl on a single page.
        "follow_subdomains": follow_subdomains,
        # Tag requests so usage is filterable in the Context.dev dashboard.
        "tags": ["rag-chatbot", "crawl-ingest"],
    }
    if max_age_ms is not None:
        kwargs["max_age_ms"] = max_age_ms
    return client.web.web_crawl_md(**kwargs)


def _results_to_pages(
    resp, progress_cb: Optional[ProgressCallback]
) -> list[Page]:
    """Map a crawl response's results into usable ``Page`` objects."""
    results = getattr(resp, "results", None) or []
    total = len(results)
    pages: list[Page] = []

    for i, item in enumerate(results, start=1):
        markdown = (getattr(item, "markdown", "") or "").strip()
        meta = getattr(item, "metadata", None)

        url = getattr(meta, "url", "") or getattr(meta, "final_url", "") or ""
        title = getattr(meta, "title", "") or ""
        success = getattr(meta, "success", True)
        status_code = getattr(meta, "status_code", 200)

        if progress_cb is not None:
            try:
                progress_cb(i, total, url)
            except Exception:  # progress is best-effort, never fatal
                logger.debug("progress_cb raised", exc_info=True)

        # Skip failed fetches and near-empty pages (same bar the old crawler used).
        if success is False or (status_code and status_code >= 400):
            continue
        if len(markdown) <= 50:
            continue

        pages.append(Page(url=url, title=title, text=markdown))

    return pages


def _crawl_link_following(
    start_url: str,
    *,
    max_pages: int,
    max_age_ms: Optional[int] = None,
    use_main_content_only: bool = True,
    follow_subdomains: bool = True,
    progress_cb: Optional[ProgressCallback] = None,
) -> list[Page]:
    """Link-following crawl via Context.dev's ``/web/crawl``.

    Returns pages reachable by following links from ``start_url``. This is the
    fallback discovery path in ``crawl_site``: it works well for sites with
    crawlable navigation, but strands on sites whose links are JS-injected or
    whose inner pages are only listed in a sitemap (see the WordPress/WAF case
    handled by the discovery helpers above).
    """
    if not start_url.startswith(("http://", "https://")):
        start_url = f"https://{start_url}"

    max_pages = max(1, min(max_pages, 500))  # clamp to documented API range

    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = _crawl_once(
                start_url,
                max_pages=max_pages,
                max_age_ms=max_age_ms,
                use_main_content_only=use_main_content_only,
                follow_subdomains=follow_subdomains,
            )
            break
        except Exception as exc:  # noqa: BLE001 - we classify below
            retry, retry_after = _should_retry(exc)
            if not retry or attempt == _MAX_RETRIES:
                raise ContextDevError(
                    f"Context.dev crawl failed for {start_url}: {exc}"
                ) from exc
            wait = _backoff_seconds(attempt, retry_after)
            logger.warning(
                "Context.dev crawl transient error (attempt %d/%d), retrying in "
                "%.1fs: %s",
                attempt + 1, _MAX_RETRIES, wait, exc,
            )
            time.sleep(wait)

    pages = _results_to_pages(resp, progress_cb)

    meta = getattr(resp, "metadata", None)
    req_id = getattr(resp, "request_id", "")
    logger.info(
        "Context.dev crawl of %s: %d usable pages (request_id=%s, meta=%s)",
        start_url, len(pages), req_id, _summarize_meta(meta),
    )
    return pages


def _summarize_meta(meta) -> dict:
    """Compact crawl stats for logging, tolerant of SDK shape changes."""
    if meta is None:
        return {}
    return {
        "num_urls": getattr(meta, "num_urls", None),
        "num_succeeded": getattr(meta, "num_succeeded", None),
        "num_failed": getattr(meta, "num_failed", None),
        "num_skipped": getattr(meta, "num_skipped", None),
    }


# ---------------------------------------------------------------------------
# Single-page scrape (used both for discovery and for Option-B content fetch).
# ---------------------------------------------------------------------------

def _scrape_markdown(url: str, *, max_age_ms: Optional[int]) -> tuple[bool, str, str]:
    """Scrape one URL and return (success, markdown, title).

    Routes through Context.dev's browser, so WAF-protected endpoints that reject
    plain HTTP clients (403) are reachable. Retries 429/5xx with bounded backoff.
    """
    client = _get_client()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            kwargs: dict = {"url": url, "formats": {"markdown": True}}
            if max_age_ms is not None:
                kwargs["max_age_ms"] = max_age_ms
            resp = client.web.scrape(**kwargs)
            md = getattr(resp, "markdown", None)
            success = bool(getattr(md, "success", False)) if md else False
            data = (getattr(md, "data", "") or "") if md else ""
            meta = getattr(resp, "metadata", None)
            title = getattr(meta, "title", "") or "" if meta else ""
            return success, data, title
        except Exception as exc:  # noqa: BLE001
            retry, retry_after = _should_retry(exc)
            if not retry or attempt == _MAX_RETRIES:
                logger.warning("Scrape failed for %s: %s", url, exc)
                return False, "", ""
            time.sleep(_backoff_seconds(attempt, retry_after))
    return False, "", ""


def scrape_urls(
    urls: list[str],
    *,
    max_age_ms: Optional[int] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> list[Page]:
    """Scrape an explicit list of URLs to Markdown (Option B of discovery).

    Used after discovery (WP REST / sitemap) yields a URL list. Each URL is
    fetched through Context.dev for faithful, JS-rendered content.
    """
    pages: list[Page] = []
    total = len(urls)
    for i, url in enumerate(urls, start=1):
        success, markdown, title = _scrape_markdown(url, max_age_ms=max_age_ms)
        if progress_cb is not None:
            try:
                progress_cb(i, total, url)
            except Exception:
                logger.debug("progress_cb raised", exc_info=True)
        if success and len(markdown.strip()) > 50:
            pages.append(Page(url=url, title=title, text=markdown.strip()))
    return pages


# ---------------------------------------------------------------------------
# Discovery: find a site's page URLs even when link-following can't.
# Both helpers fetch through Context.dev's browser, bypassing WAF 403s.
# ---------------------------------------------------------------------------

def _discover_wordpress_urls(origin: str) -> list[str]:
    """Return page+post URLs from the WordPress REST API, or [] if not WP.

    Hits ``/wp-json/wp/v2/pages`` and ``/posts`` (paginated) via scrape, parses
    the JSON, and collects each entry's ``link``.
    """
    found: list[str] = []
    for kind in ("pages", "posts"):
        # One page of up to 100 is plenty for small-business sites; most have
        # far fewer. (Pagination could be added later for very large sites.)
        api = f"{origin}/wp-json/wp/v2/{kind}?per_page=100"
        success, data, _ = _scrape_markdown(api, max_age_ms=0)
        if not success or not data:
            continue
        text = data.strip()
        # Scrape may wrap JSON in a code fence; strip to the first '['..']'.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            continue
        try:
            items = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            logger.debug("WP %s JSON parse failed for %s", kind, origin)
            continue
        for item in items:
            link = item.get("link") if isinstance(item, dict) else None
            if link and link.startswith(("http://", "https://")):
                found.append(link)
    # De-dupe, preserve order.
    seen: set[str] = set()
    out = [u for u in found if not (u in seen or seen.add(u))]
    if out:
        logger.info("WordPress REST discovery for %s: %d URLs", origin, len(out))
    return out


def _discover_sitemap_urls(origin: str) -> list[str]:
    """Return page URLs by reading the sitemap (index + child sitemaps).

    Tries common sitemap locations, follows one level of sitemap-index nesting,
    and extracts ``<loc>`` entries. Fetched via scrape so WAFs don't block it.
    """
    candidates = [
        f"{origin}/sitemap_index.xml",
        f"{origin}/sitemap.xml",
        f"{origin}/wp-sitemap.xml",
    ]
    loc_re = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
    # Markdown conversion may turn <loc> links into [url](url); capture both.
    md_link_re = re.compile(r"https?://[^\s)\]<>\"']+")

    def _fetch_locs(url: str) -> list[str]:
        success, data, _ = _scrape_markdown(url, max_age_ms=0)
        if not success or not data:
            return []
        locs = loc_re.findall(data)
        if not locs:
            locs = md_link_re.findall(data)
        return locs

    page_urls: list[str] = []
    for sm in candidates:
        locs = _fetch_locs(sm)
        if not locs:
            continue
        for loc in locs:
            if loc.endswith(".xml") and "sitemap" in loc.lower():
                # One level of nesting: child sitemap -> page URLs.
                page_urls.extend(u for u in _fetch_locs(loc) if not u.endswith(".xml"))
            elif loc.startswith(("http://", "https://")):
                page_urls.append(loc)
        if page_urls:
            break  # first sitemap that yielded real pages wins

    seen: set[str] = set()
    out = [u for u in page_urls if not (u in seen or seen.add(u))]
    if out:
        logger.info("Sitemap discovery for %s: %d URLs", origin, len(out))
    return out


def _origin_of(url: str) -> str:
    """scheme://host[:port] of a URL (no path)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def crawl_site(
    start_url: str,
    *,
    max_pages: int,
    max_age_ms: Optional[int] = None,
    use_main_content_only: bool = True,
    follow_subdomains: bool = True,
    progress_cb: Optional[ProgressCallback] = None,
) -> list[Page]:
    """Scan a website and return its pages as ``Page(url, title, text)``.

    Discovery cascade, stopping at the first path that yields multiple pages:

    1. **WordPress REST API** (``/wp-json/wp/v2/pages`` + ``/posts``) — complete
       and reliable for WordPress sites, including WAF-protected ones (fetched
       through Context.dev's browser).
    2. **Sitemap** (``/sitemap_index.xml`` and variants) — works for any CMS
       that publishes one.
    3. **Link-following crawl** (``/web/crawl``) — the general fallback for sites
       with crawlable navigation.

    For (1) and (2), discovered URLs are then scraped individually for faithful,
    JS-rendered Markdown (Option B). Results are capped at ``max_pages``.

    Raises:
        ContextDevError: only if the fallback crawl itself errors unrecoverably.
    """
    if not start_url.startswith(("http://", "https://")):
        start_url = f"https://{start_url}"
    origin = _origin_of(start_url)
    cap = max(1, min(max_pages, 500))

    # 1) WordPress REST, then 2) sitemap. Both yield a URL list we scrape.
    discovered = _discover_wordpress_urls(origin) or _discover_sitemap_urls(origin)
    if len(discovered) > 1:
        urls = discovered[:cap]
        logger.info(
            "crawl_site(%s): discovery found %d URLs (capped to %d); scraping each.",
            origin, len(discovered), len(urls),
        )
        pages = scrape_urls(urls, max_age_ms=max_age_ms, progress_cb=progress_cb)
        if pages:
            logger.info("crawl_site(%s): %d pages via discovery.", origin, len(pages))
            return pages
        logger.warning(
            "crawl_site(%s): discovery URLs scraped empty; falling back to crawl.",
            origin,
        )

    # 3) Fallback: link-following crawl.
    return _crawl_link_following(
        start_url,
        max_pages=cap,
        max_age_ms=max_age_ms,
        use_main_content_only=use_main_content_only,
        follow_subdomains=follow_subdomains,
        progress_cb=progress_cb,
    )

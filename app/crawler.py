"""Same-domain, JS-aware website crawler built on Playwright (headless Chromium).

Many real sites are JavaScript-rendered single-page apps and/or sit behind a
WAF that rejects non-browser TLS fingerprints. A plain HTTP client sees an
empty shell or gets its TLS handshake refused. Driving a real headless browser
solves both: the page's JS runs, and the TLS/HTTP behaviour looks like Chrome.

Both http:// and https:// are supported, including explicit ports (so local
dev servers like http://localhost:3000 work). If the user supplies a bare
domain with no scheme, the crawler tries https first and falls back to http.

Public surface:
    normalize_domain(raw) -> str          # stable tenant key (host[:port])
    Page(url, title, text)
    crawl_domain(domain, *, max_pages, timeout, respect_robots) -> list[Page]
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 RAGChatbot/1.0"
)

# Tags whose text we never want in the knowledge base.
_STRIP_TAGS = ["script", "style", "noscript", "template", "svg"]

# After the DOM is ready, wait for the network to go idle (JS frameworks
# finishing their fetches) but never block longer than this cap. This replaces
# a flat multi-second sleep on every page, which dominated crawl time.
_NETWORKIDLE_CAP_MS = 1500
# If a page already yielded this much visible text at domcontentloaded, it's a
# server-rendered/static page and needs no extra wait for client rendering.
_ENOUGH_TEXT_CHARS = 600

# File extensions we skip (binary / non-HTML).
_SKIP_EXT = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".json", ".xml", ".zip", ".gz", ".mp4", ".mp3",
    ".woff", ".woff2", ".ttf", ".eot", ".doc", ".docx", ".xls", ".xlsx",
)


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass
class Page:
    url: str
    title: str
    text: str


# Called after each page is processed, with (pages_done, pages_known, last_url).
# ``pages_known`` is the current size of the crawl frontier (seen + queued),
# which is an upper bound that grows as new links are discovered.
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class Target:
    """A parsed crawl target."""
    host: str            # bare host, www. stripped, lowercase (e.g. "example.com", "localhost")
    port: int | None     # explicit port if given, else None
    scheme: str | None   # "http"/"https" if the user was explicit, else None

    @property
    def key(self) -> str:
        """Stable tenant identity: host plus port when non-default."""
        return f"{self.host}:{self.port}" if self.port else self.host

    def seed_urls(self) -> list[str]:
        """Candidate start URLs, honouring an explicit scheme or trying both."""
        schemes = [self.scheme] if self.scheme else ["https", "http"]
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        urls: list[str] = []
        for scheme in schemes:
            urls.append(f"{scheme}://{authority}/")
            # Also seed the www. variant for real hostnames (not IPs, not
            # localhost, and only when no explicit port was given).
            if (
                "." in self.host
                and self.host != "localhost"
                and self.port is None
                and not _is_ip(self.host)
            ):
                urls.append(f"{scheme}://www.{self.host}/")
        return urls


def parse_target(raw: str) -> Target:
    """Parse a user-supplied domain or URL into a Target.

    Accepts: "example.com", "https://example.com", "http://localhost:3000",
    "www.example.com/path", etc. Scheme and port are preserved when present.
    """
    raw = raw.strip().lower()
    had_scheme = raw.startswith(("http://", "https://"))
    parsed = urlparse(raw if had_scheme else "//" + raw, scheme="")

    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]

    port = parsed.port  # None if not specified
    scheme = parsed.scheme if had_scheme else None
    return Target(host=host, port=port, scheme=scheme)


def normalize_domain(raw: str) -> str:
    """Stable tenant key for a target (host, plus :port when explicit).

    Scheme is intentionally dropped so http:// and https:// of the same host
    map to one tenant. Port is kept because it identifies a distinct origin.
    """
    return parse_target(raw).key


def _authority(url: str) -> str:
    """host[:port] of a URL, www. stripped, for same-site comparison."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}:{p.port}" if p.port else host


def _clean_html(html: str) -> tuple[str, str, list[str]]:
    """Return (title, text, links) extracted from rendered HTML."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)

    links = [a["href"] for a in soup.find_all("a", href=True)]
    return title, text, links


def _load_robots(base_url: str, respect: bool) -> RobotFileParser | None:
    if not respect:
        return None
    rp = RobotFileParser()
    rp.set_url(urljoin(base_url, "/robots.txt"))
    try:
        rp.read()
    except Exception as exc:  # robots is best-effort; don't fail the crawl
        logger.warning("Could not read robots.txt for %s: %s", base_url, exc)
        return None
    return rp


def crawl_domain(
    domain: str,
    *,
    max_pages: int,
    timeout: int,
    respect_robots: bool,
    progress_cb: Optional[ProgressCallback] = None,
) -> list[Page]:
    """Breadth-first crawl of a single origin using a headless browser.

    Must be called from a worker thread (not inside the asyncio event loop),
    which is how the indexer invokes it.

    ``progress_cb``, if given, is invoked after each page is visited with
    ``(pages_done, pages_known, last_url)`` so callers can report progress.
    """
    target = parse_target(domain)
    if not target.host:
        logger.warning("Could not parse a host from %r", domain)
        return []

    seeds = target.seed_urls()
    goto_timeout_ms = max(timeout, 15) * 1000

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
        page = context.new_page()
        # Don't download heavy media; we only need text + links.
        page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )

        try:
            # Pick the first seed that actually loads; its scheme+authority
            # becomes the canonical origin we keep crawling within.
            start_url = _first_reachable(page, seeds, goto_timeout_ms)
            if start_url is None:
                logger.warning("No reachable seed for %s (tried %s)", target.key, seeds)
                return []

            root_authority = _authority(start_url)
            robots = _load_robots(start_url, respect_robots)

            seen: set[str] = set()
            queue: deque[str] = deque([start_url])
            pages: list[Page] = []

            visited = 0
            while queue and len(pages) < max_pages:
                url = queue.popleft()
                url, _ = urldefrag(url)
                if url in seen:
                    continue
                seen.add(url)

                if robots is not None and not robots.can_fetch(USER_AGENT, url):
                    logger.info("robots.txt disallows %s", url)
                    continue
                if url.lower().endswith(_SKIP_EXT):
                    continue

                try:
                    resp = page.goto(
                        url, wait_until="domcontentloaded", timeout=goto_timeout_ms
                    )
                    html = _wait_for_content(page)
                except PWTimeout:
                    logger.warning("Timeout loading %s", url)
                    continue
                except Exception as exc:
                    logger.warning("Failed to load %s: %s", url, exc)
                    continue

                if resp is not None and resp.status >= 400:
                    continue

                title, text, links = _clean_html(html)
                if len(text) > 50:  # skip near-empty pages
                    pages.append(Page(url=url, title=title, text=text))

                for href in links:
                    nxt = urljoin(url, href)
                    nxt, _ = urldefrag(nxt)
                    if nxt.startswith(("http://", "https://")) and (
                        _authority(nxt) == root_authority
                    ):
                        if nxt not in seen:
                            queue.append(nxt)

                visited += 1
                # Upper bound on how many pages this crawl may touch: everything
                # seen so far plus whatever is still queued, capped at max_pages.
                known = min(len(seen) + len(queue), max_pages)
                logger.info(
                    "[crawl %s] %d/%d visited (%d kept) - %s",
                    root_authority, visited, known, len(pages), url,
                )
                if progress_cb is not None:
                    try:
                        progress_cb(visited, known, url)
                    except Exception:  # progress is best-effort, never fatal
                        logger.debug("progress_cb raised", exc_info=True)
        finally:
            context.close()
            browser.close()

    logger.info("Crawled %d pages from %s", len(pages), target.key)
    return pages


def _first_reachable(page, seeds: list[str], timeout_ms: int) -> str | None:
    """Return the first seed URL that loads without error, else None."""
    for url in seeds:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWTimeout:
            logger.info("Seed timed out: %s", url)
            _reset_page(page)
            continue
        except Exception as exc:
            logger.info("Seed unreachable %s: %s", url, exc)
            # A failed navigation can leave the page on chrome-error://, which
            # interrupts the next goto. Reset to a blank page before retrying.
            _reset_page(page)
            continue
        if resp is None or resp.status < 400:
            return page.url  # follow any redirect to the final URL
    return None


def _wait_for_content(page) -> str:
    """Return page HTML after a short, adaptive wait for client rendering.

    Static / server-rendered pages already have their text at
    ``domcontentloaded``, so we return immediately. For pages that look empty
    (likely a JS app still rendering), we wait for the network to go idle, but
    never longer than ``_NETWORKIDLE_CAP_MS``. This avoids the old flat
    multi-second sleep that was paid on every single page.
    """
    html = page.content()
    if _has_enough_text(html):
        return html
    try:
        page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
    except PWTimeout:
        pass  # cap reached; take whatever rendered so far
    except Exception:
        pass
    return page.content()


def _has_enough_text(html: str) -> bool:
    """Cheap check: does the rendered HTML already carry substantial text?"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    return len(soup.get_text(strip=True)) >= _ENOUGH_TEXT_CHARS


def _reset_page(page) -> None:
    """Clear any error state so the next navigation isn't interrupted."""
    try:
        page.goto("about:blank", timeout=5000)
    except Exception:
        pass

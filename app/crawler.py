"""Sitemap-first, browser-assisted crawler for modern websites."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 RAGChatbot/2.0")
_STRIP_TAGS = ["script", "style", "noscript", "template", "svg"]
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".json", ".xml", ".zip", ".gz", ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".eot", ".doc", ".docx", ".xls", ".xlsx")
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_ga"}
_COMMON_ROUTES = ("/about", "/products", "/services", "/contact", "/faq", "/blog", "/shop", "/menu", "/collections")
_ROUTE_RE = re.compile(r"(?:https?://[^\"'\s<>]+|/(?:[A-Za-z0-9][^\"'\s<>]*)?)")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _assert_safe_host(host: str) -> None:
    candidates = [host]
    if not _is_ip(host):
        try:
            candidates = [item[4][0] for item in socket.getaddrinfo(host, None)]
        except socket.gaierror as exc:
            raise ValueError("target host cannot be resolved") from exc
    for value in set(candidates):
        address = ipaddress.ip_address(value)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise ValueError("private or reserved crawler target")


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class CrawlMetrics:
    sitemap_urls_found: int = 0
    navigation_urls_found: int = 0
    dynamic_urls_found: int = 0
    jsonld_urls_found: int = 0
    total_unique_urls_discovered: int = 0
    total_pages_crawled: int = 0
    pages_skipped: int = 0
    crawl_coverage_percent: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    pages: list[Page]
    metrics: CrawlMetrics

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class Target:
    host: str
    port: int | None
    scheme: str | None

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host

    def seed_urls(self) -> list[str]:
        schemes = [self.scheme] if self.scheme else ["https", "http"]
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        result = []
        for scheme in schemes:
            result.append(f"{scheme}://{authority}/")
            if "." in self.host and self.port is None and not _is_ip(self.host) and self.host != "localhost":
                result.append(f"{scheme}://www.{self.host}/")
        return result


def parse_target(raw: str) -> Target:
    raw = raw.strip().lower()
    explicit = raw.startswith(("http://", "https://"))
    parsed = urlparse(raw if explicit else "//" + raw)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return Target(host, parsed.port, parsed.scheme if explicit else None)


def normalize_domain(raw: str) -> str:
    return parse_target(raw).key


def _authority(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    port = parsed.port
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
        port = None
    return f"{host}:{port}" if port else host


def _normalize_url(raw: str, base: str) -> str | None:
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    value = urljoin(base, raw.strip())
    value, _ = urldefrag(value)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in _TRACKING_PARAMS]
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query, doseq=True), ""))


def _same_domain(url: str, root: str) -> bool:
    return _authority(url) == root


def _safe_route_candidate(value: str, base: str, root: str) -> str | None:
    candidate = _normalize_url(value, base)
    if candidate and _same_domain(candidate, root) and not candidate.lower().endswith(_SKIP_EXT):
        return candidate
    return None


def _recursive_json_routes(value, base: str, root: str, output: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _recursive_json_routes(item, base, root, output)
    elif isinstance(value, list):
        for item in value:
            _recursive_json_routes(item, base, root, output)
    elif isinstance(value, str):
        for match in _ROUTE_RE.findall(value):
            route = _safe_route_candidate(match, base, root)
            if route:
                output.add(route)


def _extract_structured_urls(soup: BeautifulSoup, page_url: str, root: str) -> tuple[set[str], set[str]]:
    structured, jsonld = set(), set()
    for tag in soup.find_all(["script", "link"]):
        content = tag.get_text() if tag.name == "script" else tag.get("href", "")
        if tag.name == "link" and tag.get("rel") and any(x in {"canonical", "next", "prev"} for x in tag.get("rel", [])):
            route = _safe_route_candidate(content, page_url, root)
            if route: structured.add(route)
        if tag.name == "script":
            raw = content.strip()
            if not raw: continue
            try:
                _recursive_json_routes(json.loads(raw), page_url, root, jsonld)
            except (ValueError, TypeError):
                for match in _ROUTE_RE.findall(raw):
                    route = _safe_route_candidate(match, page_url, root)
                    if route: structured.add(route)
    for tag in soup.find_all(attrs={"content": True}):
        if tag.get("property") in {"og:url", "og:see_also"}:
            route = _safe_route_candidate(tag["content"], page_url, root)
            if route: structured.add(route)
    for tag in soup.find_all(attrs={"itemtype": re.compile("schema.org")}, itemprop=True):
        route = _safe_route_candidate(tag.get("href") or tag.get("content") or "", page_url, root)
        if route: structured.add(route)
    return structured, jsonld


def _clean_html(html: str, page_url: str = "", root: str = "") -> tuple[str, str, set[str], set[str], set[str]]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    links, dynamic, structured = set(), set(), set()
    for tag in soup.find_all(True):
        for attr in ("href", "data-href", "routerlink", "to", "data-to", "data-route", "data-url", "action"):
            if tag.get(attr):
                route = _safe_route_candidate(tag[attr], page_url, root)
                if route: links.add(route)
        if tag.get("onclick"):
            for raw in _ROUTE_RE.findall(tag["onclick"]):
                route = _safe_route_candidate(raw, page_url, root)
                if route: dynamic.add(route)
        if tag.name == "button" and (tag.get("data-navigation") or tag.get("aria-controls") or tag.get("onclick")):
            dynamic.update(_safe_route_candidate(raw, page_url, root) for raw in _ROUTE_RE.findall(tag.get_text(" ") + " " + tag.get("onclick", "")) if _safe_route_candidate(raw, page_url, root))
    for rel in ("canonical", "next", "prev"):
        for tag in soup.find_all("link", rel=lambda value: value and rel in value):
            route = _safe_route_candidate(tag.get("href", ""), page_url, root)
            if route: structured.add(route)
    extra, jsonld = _extract_structured_urls(soup, page_url, root)
    structured |= extra
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return title, text, links, dynamic, structured | jsonld


def _load_robots_and_sitemaps(base_url: str, respect: bool, client: httpx.Client) -> tuple[RobotFileParser | None, list[str], list[str]]:
    robots_url = urljoin(base_url, "/robots.txt")
    rp = RobotFileParser(robots_url) if respect else None
    sitemap_candidates = [urljoin(base_url, "/sitemap.xml"), urljoin(base_url, "/sitemap_index.xml")]
    try:
        response = client.get(robots_url, timeout=15)
        if response.status_code < 400:
            body = response.text
            if rp:
                rp.parse(body.splitlines())
            sitemap_candidates.extend(line.split(":", 1)[1].strip() for line in body.splitlines() if line.lower().startswith("sitemap:") and ":" in line)
        elif rp:
            logger.warning("robots.txt returned HTTP %s for %s", response.status_code, robots_url)
    except Exception as exc:
        logger.warning("Could not read robots.txt for %s: %s", base_url, exc)
    return rp, list(dict.fromkeys(sitemap_candidates)), []


def _parse_sitemaps(candidates: list[str], client: httpx.Client, root: str, robots: RobotFileParser | None, metrics: CrawlMetrics) -> set[str]:
    found, pending, visited = set(), deque(candidates), set()
    while pending:
        sitemap_url = pending.popleft()
        sitemap_url = _normalize_url(sitemap_url, sitemap_url)
        if not sitemap_url or sitemap_url in visited or not _same_domain(sitemap_url, root): continue
        visited.add(sitemap_url)
        if robots and not robots.can_fetch(USER_AGENT, sitemap_url): continue
        try:
            response = client.get(sitemap_url, timeout=20)
            if response.status_code >= 400 or not response.text.lstrip().startswith("<"): continue
            root_node = ET.fromstring(response.content)
            tag = root_node.tag.rsplit("}", 1)[-1].lower()
            for node in root_node.iter():
                if node.tag.rsplit("}", 1)[-1].lower() != "loc" or not node.text: continue
                url = _normalize_url(node.text, sitemap_url)
                if not url or not _same_domain(url, root): continue
                if tag == "sitemapindex": pending.append(url)
                else: found.add(url)
        except Exception as exc:
            logger.warning("Could not parse sitemap %s: %s", sitemap_url, exc)
            metrics.warnings.append(f"sitemap: {sitemap_url}: {exc}")
    metrics.sitemap_urls_found = len(found)
    return found


def _prepare_playwright_subprocess_loop() -> None:
    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _first_reachable(page, seeds: list[str], timeout_ms: int) -> str | None:
    for url in seeds:
        try:
            response = page.goto(url, wait_until="commit", timeout=timeout_ms)
            if response is None or response.status < 400: return page.url
        except Exception:
            try: page.goto("about:blank", timeout=5000)
            except Exception: pass
    return None


def _wait_for_content(page, network_idle_timeout_ms: int, post_render_delay_ms: int, url: str) -> str:
    try:
        page.wait_for_load_state("networkidle", timeout=network_idle_timeout_ms)
    except PWTimeout:
        logger.warning("Network idle timed out for %s; continuing with available DOM", url)
    except Exception as exc:
        logger.warning("Network idle failed for %s: %s; continuing", url, exc)
    try: page.wait_for_timeout(post_render_delay_ms)
    except Exception: pass
    return page.content()


def _expand_navigation(page, url: str, root: str) -> set[str]:
    before = set()
    try:
        candidates = page.locator("button, [role='button'], [aria-expanded='false']")
        count = min(candidates.count(), 30)
        for index in range(count):
            item = candidates.nth(index)
            label = (item.get_attribute("aria-label") or "") + " " + (item.inner_text(timeout=500) or "")
            if re.search(r"menu|navigation|more|explore|category|drawer", label, re.I):
                try: item.click(timeout=1200)
                except Exception: pass
        page.wait_for_timeout(250)
        html = page.content()
        _, _, _, dynamic, structured = _clean_html(html, url, root)
        before |= dynamic | structured
    except Exception as exc:
        logger.debug("Navigation expansion failed for %s: %s", url, exc_info=True)
    return before


def crawl_domain(domain: str, *, max_pages: int, timeout: int, respect_robots: bool, progress_cb: Optional[ProgressCallback] = None, render_javascript: bool = True, network_idle_timeout: int = 30, post_render_delay: int = 5, return_result: bool = False):
    target = parse_target(domain)
    if not target.host: return CrawlResult([], CrawlMetrics()) if return_result else []
    _assert_safe_host(target.host)
    metrics = CrawlMetrics()
    pages: list[Page] = []
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    try:
        reachable = None
        for seed in target.seed_urls():
            try:
                response = client.get(seed, timeout=timeout)
                if response.status_code < 400:
                    reachable = str(response.url); break
            except Exception: continue
        if not reachable:
            metrics.warnings.append("No HTTP seed was reachable; browser fallback will decide reachability")
            reachable = target.seed_urls()[0]
        root = _authority(reachable)
        robots, sitemap_candidates, _ = _load_robots_and_sitemaps(reachable, respect_robots, client)
        sitemap_urls = _parse_sitemaps(sitemap_candidates, client, root, robots, metrics)
        queue: deque[str] = deque(sitemap_urls)
        discovered = set(sitemap_urls)
        start = _normalize_url(reachable, reachable)
        if start:
            discovered.add(start)
            queue.append(start)
        _prepare_playwright_subprocess_loop()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=False)
            page = context.new_page()
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
            try:
                if not start:
                    start = _first_reachable(page, target.seed_urls(), max(timeout, 15) * 1000)
                    if not start: return CrawlResult([], metrics) if return_result else []
                    root = _authority(start)
                    queue.append(start); discovered.add(start)
                seen: set[str] = set()
                while queue and len(pages) < max_pages:
                    url = queue.popleft()
                    url = _normalize_url(url, start or reachable)
                    if not url or url in seen: continue
                    seen.add(url)
                    if robots and not robots.can_fetch(USER_AGENT, url): metrics.pages_skipped += 1; continue
                    if url.lower().endswith(_SKIP_EXT): metrics.pages_skipped += 1; continue
                    try:
                        response = page.goto(url, wait_until="commit", timeout=max(timeout, 15) * 1000)
                        try: page.wait_for_load_state("domcontentloaded", timeout=max(timeout, 15) * 1000)
                        except PWTimeout: logger.warning("DOMContentLoaded timed out for %s; using available DOM", url)
                        html = _wait_for_content(page, min(network_idle_timeout, 5) * 1000, post_render_delay * 1000, url) if render_javascript else page.content()
                    except Exception as exc:
                        logger.warning("Failed to load %s; continuing: %s", url, exc)
                        metrics.pages_skipped += 1; continue
                    if response is not None and response.status >= 400:
                        metrics.pages_skipped += 1; continue
                    title, text, nav, dynamic, structured = _clean_html(html, url, root)
                    if len(nav) < 5 and render_javascript:
                        dynamic |= _expand_navigation(page, url, root)
                        html = page.content()
                        second_title, second_text, second_nav, second_dynamic, second_structured = _clean_html(html, url, root)
                        if len(second_text) > len(text): title, text = second_title, second_text
                        nav |= second_nav; dynamic |= second_dynamic; structured |= second_structured
                    metrics.navigation_urls_found += len(nav)
                    metrics.dynamic_urls_found += len(dynamic)
                    metrics.jsonld_urls_found += len(structured)
                    for candidate in nav | dynamic | structured:
                        if candidate not in discovered and _same_domain(candidate, root): discovered.add(candidate); queue.append(candidate)
                    if len(text) > 50: pages.append(Page(url, title, text))
                    else: metrics.pages_skipped += 1
                    metrics.total_unique_urls_discovered = len(discovered)
                    metrics.total_pages_crawled += 1
                    metrics.crawl_coverage_percent = round(metrics.total_pages_crawled / max(1, len(discovered)) * 100, 2)
                    if progress_cb:
                        try: progress_cb(metrics.total_pages_crawled, min(len(discovered), max_pages), url)
                        except Exception: logger.debug("progress callback failed", exc_info=True)
                if len(discovered) < 3:
                    for route in _COMMON_ROUTES:
                        candidate = _safe_route_candidate(route, start or reachable, root)
                        if candidate and candidate not in discovered:
                            try:
                                response = client.get(candidate, timeout=10, follow_redirects=True)
                                if response.status_code < 400: discovered.add(candidate); queue.append(candidate)
                            except Exception: pass
                    while queue and len(pages) < max_pages:
                        url = queue.popleft()
                        if url in seen: continue
                        seen.add(url)
                        try:
                            response = page.goto(url, wait_until="commit", timeout=max(timeout, 15) * 1000)
                            try: page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception: pass
                            title, text, *_ = _clean_html(page.content(), url, root)
                            if response is None or response.status < 400:
                                if len(text) > 50:
                                    pages.append(Page(url, title, text))
                                else:
                                    metrics.pages_skipped += 1
                                metrics.total_pages_crawled += 1
                        except Exception as exc:
                            metrics.pages_skipped += 1
                            logger.warning("Fallback route failed for %s: %s", url, exc)
            finally:
                context.close(); browser.close()
    finally:
        client.close()
    metrics.total_unique_urls_discovered = len(discovered)
    metrics.crawl_coverage_percent = round(metrics.total_pages_crawled / max(1, len(discovered)) * 100, 2)
    logger.info("Crawl summary: sitemap=%d navigation=%d dynamic=%d jsonld=%d unique=%d crawled=%d skipped=%d coverage=%.2f%%", metrics.sitemap_urls_found, metrics.navigation_urls_found, metrics.dynamic_urls_found, metrics.jsonld_urls_found, metrics.total_unique_urls_discovered, metrics.total_pages_crawled, metrics.pages_skipped, metrics.crawl_coverage_percent)
    result = CrawlResult(pages, metrics)
    return result if return_result else pages

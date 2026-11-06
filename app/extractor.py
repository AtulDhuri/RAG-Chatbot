"""Provider abstraction for website analysis and knowledge extraction."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib, logging, re, time
from typing import Any
from urllib.parse import urlparse
import httpx
from .config import get_settings

logger = logging.getLogger(__name__)

@dataclass
class DiscoveredPage:
    url: str
    title: str = ""

@dataclass
class WebsiteAnalysisResult:
    url: str
    pages: list[DiscoveredPage] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    estimated_knowledge_size: str = "unknown"
    analyzed_at: str = ""
    provider: str = "CONTEXT_DEV"

@dataclass
class ExtractedPage:
    url: str
    title: str
    text: str

@dataclass
class KnowledgeExtractionResult:
    pages: list[ExtractedPage] = field(default_factory=list)
    provider: str = "CONTEXT_DEV"
    warnings: list[str] = field(default_factory=list)
    raw_pages: int = 0

class WebsiteKnowledgeExtractor(ABC):
    @abstractmethod
    def analyze_website(self, url: str) -> WebsiteAnalysisResult:
        """Discover URLs and estimate scope without generating embeddings."""

    @abstractmethod
    def extract_knowledge(self, url: str) -> KnowledgeExtractionResult:
        """Extract page content for approved ingestion."""

class ContextDevExtractor(WebsiteKnowledgeExtractor):
    provider_name = "CONTEXT_DEV"

    def __init__(self, *, force_recrawl: bool = False):
        settings = get_settings()
        self.api_key = settings.context_dev_api_key
        configured_base = settings.context_dev_base_url.rstrip("/")
        self.base_url = configured_base if configured_base.endswith("/v1") else configured_base + "/v1"
        self.max_pages = settings.context_dev_max_pages
        self.timeout = settings.context_dev_timeout
        self.analysis_max_links = settings.context_dev_analysis_max_links
        self.analysis_url_regex = settings.context_dev_analysis_url_regex
        self.force_recrawl = force_recrawl

    def _validate(self, url: str):
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"Invalid website URL: {url}")
        return parsed

    def analyze_website(self, url: str) -> WebsiteAnalysisResult:
        if not self.api_key: raise RuntimeError("CONTEXT_DEV_API_KEY is not configured")
        parsed = self._validate(url)
        params = {"domain": parsed.netloc}
        if self.analysis_max_links > 0:
            params["maxLinks"] = self.analysis_max_links
        if self.analysis_url_regex:
            params["urlRegex"] = self.analysis_url_regex
        response = self._request("GET", f"{self.base_url}/web/urls", params=params)
        body = response.json()
        pages = _urls_from_response(body)
        if not pages:
            pages = [DiscoveredPage(url=url, title="Homepage")]
        return WebsiteAnalysisResult(url=url, pages=pages, categories=_categories_from_pages(pages), estimated_knowledge_size=_estimate_size(len(pages)), analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), provider=self.provider_name)

    def extract_knowledge(self, url: str) -> KnowledgeExtractionResult:
        if not self.api_key: raise RuntimeError("CONTEXT_DEV_API_KEY is not configured")
        self._validate(url)
        payload: dict[str, Any] = {"url": url}
        # Context.dev has a provider-side maximum. Leave this unset when using
        # that provider's default, and use the local backend for unbounded sites.
        if self.max_pages > 0:
            payload["maxPages"] = self.max_pages
        if self.force_recrawl: payload["maxAgeMs"] = 0
        started = time.monotonic()
        logger.info("Context.dev extraction started url=%s provider=%s", url, self.provider_name)
        body = self._request("POST", f"{self.base_url}/web/crawl", json=payload).json()
        pages = _clean_pages(_pages_from_response(body))
        logger.info("Context.dev extraction finished url=%s duration=%.2fs pages=%d provider=%s", url, time.monotonic()-started, len(pages), self.provider_name)
        if not pages: raise RuntimeError("Context.dev returned no usable website content")
        return KnowledgeExtractionResult(pages=pages, provider=self.provider_name, raw_pages=len(_pages_from_response(body)))

    # Backwards-compatible alias for callers from the earlier MVP adapter.
    def extract(self, url: str) -> KnowledgeExtractionResult:
        return self.extract_knowledge(url)

    def preview(self, url: str) -> int:
        return len(self.analyze_website(url).pages)

    def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=self.timeout, headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "RAGChatbot/1.0"}) as client:
                response = client.request(method, endpoint, **kwargs)
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Context.dev request failed ({exc.response.status_code}): {_error_detail(exc.response)}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Context.dev request failed: {exc}") from exc

def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json(); return str(body.get("message") or body.get("error") or body)[:500]
    except ValueError: return response.text[:500]

def _categories_from_pages(pages: list[DiscoveredPage]) -> list[str]:
    labels = {
        "about": "About", "contact": "Contact", "product": "Products", "products": "Products",
        "service": "Services", "services": "Services", "faq": "FAQs", "blog": "Blog",
        "shop": "Shop", "menu": "Menu", "collection": "Collections", "collections": "Collections",
        "help": "Help", "support": "Support", "pricing": "Pricing", "team": "Team",
    }
    found = set()
    for page in pages:
        for segment in urlparse(page.url).path.lower().split('/'):
            if segment in labels: found.add(labels[segment])
    return sorted(found) or ["General website content"]


def _estimate_size(count: int) -> str:
    if count < 1: return "unknown"
    if count < 25: return "small"
    if count < 100: return "medium"
    return "large"

def _urls_from_response(body: Any) -> list[DiscoveredPage]:
    if not isinstance(body, dict): return []
    values = body.get("urls") or body.get("results") or body.get("data") or []
    if isinstance(values, dict): values = values.get("urls") or values.get("results") or []
    result=[]
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str): result.append(DiscoveredPage(item))
            elif isinstance(item, dict):
                url=item.get("url") or item.get("sourceUrl") or item.get("finalUrl")
                if url: result.append(DiscoveredPage(str(url), str(item.get("title") or item.get("metadata", {}).get("title") or "")))
    return list({p.url:p for p in result}.values())

def _pages_from_response(body: Any) -> list[ExtractedPage]:
    if not isinstance(body, dict): return []
    candidates = body.get("results") or body.get("pages") or body.get("data") or []
    if isinstance(candidates, dict): candidates = candidates.get("results") or candidates.get("pages") or []
    pages=[]
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict): continue
            metadata=item.get("metadata") or {}
            page_url=item.get("url") or metadata.get("sourceUrl") or metadata.get("finalUrl")
            text=item.get("markdown") or item.get("text") or item.get("content") or item.get("data")
            if isinstance(text, dict): text=text.get("data") or text.get("markdown") or text.get("text")
            title=item.get("title") or metadata.get("title") or ""
            if page_url and isinstance(text, str): pages.append(ExtractedPage(str(page_url), str(title), text))
    if not pages and isinstance(body.get("markdown"), str):
        metadata=body.get("metadata") or {}; pages.append(ExtractedPage(str(metadata.get("finalUrl") or metadata.get("sourceUrl") or ""), str(metadata.get("title") or ""), body["markdown"]))
    return pages

def _clean_pages(pages: list[ExtractedPage]) -> list[ExtractedPage]:
    seen=set(); result=[]
    for page in pages:
        text=_clean_text(page.text)
        fingerprint=hashlib.sha256(re.sub(r"\s+", " ", text).lower().encode()).hexdigest()
        if not page.url or len(text)<40 or fingerprint in seen: continue
        seen.add(fingerprint); result.append(ExtractedPage(page.url, page.title.strip(), text))
    return result

def _clean_text(text: str) -> str:
    text=re.sub(r"(?is)<(script|style|noscript|nav|footer|header|aside)[^>]*>.*?</\1>", " ", text)
    noise=re.compile(r"cookie|accept all|subscribe to our newsletter|privacy settings|skip to content", re.I)
    lines=[]
    for line in text.splitlines():
        line=re.sub(r"\s+", " ", line).strip()
        if line and not noise.fullmatch(line): lines.append(line)
    return "\n".join(lines).strip()


def get_website_extractor(*, force_recrawl: bool = False) -> WebsiteKnowledgeExtractor:
    settings = get_settings()
    backend = settings.crawl_backend.lower()
    if backend == "playwright" or (backend == "auto" and (not settings.context_dev_api_key or settings.crawl_max_pages <= 0)):
        return PlaywrightExtractor()
    if backend not in {"auto", "context_dev"}:
        raise ValueError(f"Unsupported CRAWL_BACKEND: {settings.crawl_backend}")
    return ContextDevExtractor(force_recrawl=force_recrawl)


class PlaywrightExtractor(WebsiteKnowledgeExtractor):
    """Crawl the complete same-domain frontier without a page-count cap."""

    provider_name = "PLAYWRIGHT"

    def __init__(self):
        self.settings = get_settings()

    def _crawl(self, url: str):
        from .crawler import crawl_domain
        return crawl_domain(
            url,
            max_pages=self.settings.crawl_max_pages,
            timeout=self.settings.crawl_timeout,
            respect_robots=self.settings.crawl_respect_robots,
            render_javascript=self.settings.render_javascript,
            network_idle_timeout=self.settings.network_idle_timeout,
            post_render_delay=self.settings.post_render_delay,
            return_result=True,
        )

    def analyze_website(self, url: str) -> WebsiteAnalysisResult:
        result = self._crawl(url)
        pages = [DiscoveredPage(page.url, page.title) for page in result.pages]
        return WebsiteAnalysisResult(
            url=url, pages=pages, categories=_categories_from_pages(pages),
            estimated_knowledge_size=_estimate_size(len(pages)),
            analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            provider=self.provider_name,
        )

    def extract_knowledge(self, url: str) -> KnowledgeExtractionResult:
        result = self._crawl(url)
        pages = _clean_pages([ExtractedPage(p.url, p.title, p.text) for p in result.pages])
        if not pages:
            raise RuntimeError("Crawler returned no usable website content")
        return KnowledgeExtractionResult(
            pages=pages, provider=self.provider_name,
            raw_pages=result.metrics.total_pages_crawled,
            warnings=result.metrics.warnings,
        )

    def extract(self, url: str) -> KnowledgeExtractionResult:
        return self.extract_knowledge(url)
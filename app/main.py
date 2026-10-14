"""FastAPI app: domain registration, chat, and the embeddable widget."""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import get_settings
from .crawler import normalize_domain
from . import indexer, rag, vectorstore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="RAG Website Chatbot", version="1.0.0")
app.state.limiter = limiter

STATIC_DIR = Path(__file__).parent / "static"


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please slow down."},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------- schemas ---------------------------
class RegisterRequest(BaseModel):
    domain: str = Field(..., min_length=1)
    force: bool = False


class RegisterResponse(BaseModel):
    domain: str
    status: str  # "ready" | "indexing" | "started"
    indexed: bool


class StatusResponse(BaseModel):
    domain: str
    phase: str  # "crawling" | "embedding" | "ready" | "error" | "idle"
    indexing: bool
    indexed: bool
    pages_done: int
    pages_known: int
    chunks: int
    last_url: str


class ChatRequest(BaseModel):
    domain: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]
    status: str  # "ready" | "indexing"


# --------------------------- routes ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/register", response_model=RegisterResponse)
def register(req: RegisterRequest):
    domain = normalize_domain(req.domain)
    status = indexer.ensure_indexed(domain, force=req.force)
    return RegisterResponse(
        domain=domain,
        status=status,
        indexed=vectorstore.is_indexed(domain),
    )


@app.get("/api/status", response_model=StatusResponse)
def status(domain: str):
    """Crawl/index progress for a domain, suitable for client-side polling."""
    domain = normalize_domain(domain)
    snap = indexer.get_progress(domain)
    return StatusResponse(
        domain=domain,
        phase=snap["phase"],
        indexing=snap["indexing"],
        indexed=vectorstore.is_indexed(domain),
        pages_done=snap["pages_done"],
        pages_known=snap["pages_known"],
        chunks=snap["chunks"],
        last_url=snap["last_url"],
    )


@app.post("/api/chat", response_model=ChatResponse)
@limiter.limit(settings.chat_rate_limit)
def chat(request: Request, req: ChatRequest):
    domain = normalize_domain(req.domain)

    # Make sure content exists / is fresh; kicks off a background crawl if not.
    index_status = indexer.ensure_indexed(domain)

    if not vectorstore.is_indexed(domain):
        return ChatResponse(
            answer=(
                "I'm still reading through this website to learn about it. "
                "Please ask again in a minute or two."
            ),
            sources=[],
            status="indexing",
        )

    result = rag.answer(domain, req.message)
    status = "indexing" if indexer.is_indexing(domain) else "ready"
    return ChatResponse(
        answer=result["answer"], sources=result["sources"], status=status
    )


@app.get("/widget.js")
def widget_js():
    path = STATIC_DIR / "widget.js"
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/demo")
def demo():
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api")
def api_info():
    return {
        "service": "RAG Website Chatbot",
        "endpoints": [
            "/api/register",
            "/api/status",
            "/api/chat",
            "/widget.js",
            "/health",
        ],
    }

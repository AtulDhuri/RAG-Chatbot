"""FastAPI app: domain registration, chat, and the embeddable widget."""
import logging
from pathlib import Path
from decimal import Decimal

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import get_settings
from .crawler import normalize_domain
from . import indexer, rag, vectorstore, subscriptions

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


class SignupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=10, max_length=200)
    role: str = Field(default="customer", pattern=r"^(customer|admin)$")
    admin_code: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=10, max_length=200)


class SiteRequest(BaseModel):
    domain: str = Field(..., min_length=1)


class SiteResponse(BaseModel):
    domain: str
    site_id: str
    installation_token: str


class RazorpayVerifyRequest(BaseModel):
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_subscription_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)


class PlanRequest(BaseModel):
    id: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=80)
    price: Decimal = Field(..., ge=0, description="Price in normal currency units, for example 499 means ₹499")
    currency: str = Field(default="INR", min_length=3, max_length=3)
    billing_interval: str = Field(default="monthly", pattern=r"^(monthly|yearly)$")
    chat_limit: int = Field(..., gt=0)
    website_limit: int = Field(default=1, gt=0)
    indexed_page_limit: int = Field(default=100, gt=0)


# --------------------------- routes ---------------------------
@app.get("/api/plans")
def plans():
    return subscriptions.list_plans()


@app.get("/api/usage")
def usage(request: Request):
    return subscriptions.usage_summary(subscriptions.account_for_request(request))


@app.get("/api/admin/me")
def admin_me(request: Request):
    account_id = subscriptions.account_for_request(request)
    subscriptions.require_admin(account_id)
    return {"admin": True}


@app.post("/api/admin/plans")
def admin_create_plan(request: Request, req: PlanRequest):
    account_id = subscriptions.account_for_request(request)
    subscriptions.require_admin(account_id)
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(503, "Razorpay billing is not configured")
    try:
        import razorpay
        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        remote = client.plan.create({
            "period": req.billing_interval,
            "interval": 1,
            "item": {"name": req.name, "amount": int(req.price * 100), "currency": req.currency.upper(), "description": req.name},
        })
        return subscriptions.create_plan({
            "id": req.id, "name": req.name, "chat_limit": req.chat_limit,
            "website_limit": req.website_limit, "indexed_page_limit": req.indexed_page_limit,
            "price_amount": int(req.price * 100), "currency": req.currency.upper(),
            "billing_interval": req.billing_interval, "razorpay_plan_id": remote["id"],
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin plan creation failed")
        raise HTTPException(502, "Unable to create Razorpay plan") from exc


@app.post("/api/billing/create-subscription")
def create_subscription(request: Request, plan_id: str = "starter"):
    """Create a Razorpay subscription using a database-managed plan."""
    account_id = subscriptions.account_for_request(request)
    razorpay_id = subscriptions.razorpay_plan_id(plan_id)
    if not settings.razorpay_key_id or not settings.razorpay_key_secret or not razorpay_id:
        raise HTTPException(status_code=503, detail="Razorpay billing is not configured")
    try:
        import razorpay
        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        subscription = client.subscription.create({
            "plan_id": razorpay_id,
            "total_count": 12,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {"account_id": account_id, "plan_id": plan_id},
        })
        return {"key_id": settings.razorpay_key_id, "subscription_id": subscription["id"], "plan_id": plan_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Razorpay subscription creation failed")
        raise HTTPException(status_code=502, detail="Unable to start payment") from exc


@app.post("/api/billing/verify")
def verify_payment(request: Request, req: RazorpayVerifyRequest):
    """Verify Razorpay Checkout's callback; webhooks remain authoritative."""
    account_id = subscriptions.account_for_request(request)
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="Razorpay billing is not configured")
    try:
        import razorpay
        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        client.utility.verify_subscription_payment_signature({
            "razorpay_payment_id": req.razorpay_payment_id,
            "razorpay_subscription_id": req.razorpay_subscription_id,
            "razorpay_signature": req.razorpay_signature,
        })
        subscription = client.subscription.fetch(req.razorpay_subscription_id)
        subscriptions.apply_subscription(
            str(subscription.get("customer_id", "")),
            req.razorpay_subscription_id,
            "active",
            subscription.get("end_at") or subscription.get("charge_at"),
            account_id,
        )
        return {"verified": True, "status": "active"}
    except Exception as exc:
        logger.warning("Razorpay payment verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Payment verification failed") from exc


@app.post("/api/billing/webhook")
async def razorpay_webhook(request: Request):
    if not settings.razorpay_webhook_secret:
        raise HTTPException(status_code=503, detail="Razorpay billing is not configured")
    payload = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    try:
        import razorpay
        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        client.utility.verify_webhook_signature(payload.decode("utf-8"), signature, settings.razorpay_webhook_secret)
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Razorpay webhook") from exc
    event_id = request.headers.get("x-razorpay-event-id") or event.get("id") or event.get("event")
    if not event_id or not subscriptions.mark_webhook(str(event_id)):
        return {"received": True, "duplicate": True}
    payload_data = event.get("payload", {})
    sub_entity = ((payload_data.get("subscription", {}) or {}).get("entity", {}) or {})
    if not sub_entity:
        sub_entity = ((payload_data.get("payment", {}) or {}).get("entity", {}) or {})
    event_name = str(event.get("event", ""))
    status_map = {
        "subscription.activated": "active",
        "subscription.charged": "active",
        "subscription.completed": "expired",
        "subscription.cancelled": "canceled",
        "subscription.paused": "suspended",
        "subscription.resumed": "active",
        "payment.failed": "past_due",
    }
    status = status_map.get(event_name)
    if status and sub_entity:
        notes = sub_entity.get("notes") or {}
        subscriptions.apply_subscription(
            str(sub_entity.get("customer_id", "")),
            str(sub_entity.get("id", "")),
            status,
            sub_entity.get("end_at") or sub_entity.get("charge_at"),
            str(notes.get("account_id", "")),
        )
    return {"received": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/signup")
@app.post("/api/auth/register")
def signup(req: SignupRequest):
    account = subscriptions.signup(req.email, req.password, role=req.role, admin_code=req.admin_code)
    token = subscriptions.login(req.email, req.password)
    response = JSONResponse({**account, "status": "registered", "role": req.role})
    response.set_cookie("session", token, httponly=True, secure=bool(settings.public_app_url.startswith("https://")), samesite="lax", max_age=settings.session_ttl_seconds)
    return response


@app.post("/api/auth/login")
def login(req: LoginRequest):
    token = subscriptions.login(req.email, req.password)
    response = JSONResponse({"status": "ok"})
    response.set_cookie("session", token, httponly=True, secure=bool(settings.public_app_url.startswith("https://")), samesite="lax", max_age=settings.session_ttl_seconds)
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    subscriptions.logout(request)
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session")
    return response


@app.get("/api/auth/me")
def me(request: Request):
    account_id = subscriptions.account_for_request(request)
    info = subscriptions.account_info(account_id)
    info["role"] = "admin" if subscriptions.is_admin(account_id) else "customer"
    return info


@app.post("/api/sites", response_model=SiteResponse)
def create_site(request: Request, req: SiteRequest):
    account_id = subscriptions.account_for_request(request)
    return subscriptions.create_site(account_id, req.domain)


@app.post("/api/sites/{domain:path}/rotate-token", response_model=SiteResponse)
def rotate_site_token(request: Request, domain: str):
    account_id = subscriptions.account_for_request(request)
    return subscriptions.rotate_site_token(account_id, domain)


@app.post("/api/register", response_model=RegisterResponse)
def register(request: Request, req: RegisterRequest):
    domain = normalize_domain(req.domain)
    site_id, account_id = subscriptions.require_site(request, domain)
    subscriptions.require_active_subscription(account_id, domain)
    status = indexer.ensure_indexed(domain, force=req.force if not settings.database_url else False)
    return RegisterResponse(
        domain=domain,
        status=status,
        indexed=vectorstore.is_indexed(domain),
    )


@app.get("/api/status", response_model=StatusResponse)
def status(request: Request, domain: str, token: str | None = None):
    """Crawl/index progress for an installation, suitable for polling."""
    domain = normalize_domain(domain)
    subscriptions.require_site(request, domain)
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
    site_id, account_id = subscriptions.require_site(request, domain)
    subscriptions.require_active_subscription(account_id, domain)

    # Make sure content exists / is fresh; kicks off a background crawl if not.
    indexer.ensure_indexed(domain)

    if not vectorstore.is_indexed(domain):
        return ChatResponse(
            answer=(
                "I'm still reading through this website to learn about it. "
                "Please ask again in a minute or two."
            ),
            sources=[],
            status="indexing",
        )

    # Reserve one quota unit only for a request that will call the RAG/LLM path.
    subscriptions.enforce_chat_quota(site_id, account_id, domain)
    subscriptions.record_chat_query(site_id, account_id, domain, req.message)
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


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html", media_type="text/html")


@app.get("/admin/login")
def admin_login_page():
    return FileResponse(STATIC_DIR / "admin_login.html", media_type="text/html")


@app.get("/admin")
def admin():
    return FileResponse(STATIC_DIR / "admin.html", media_type="text/html")


@app.get("/pricing")
def pricing():
    return FileResponse(STATIC_DIR / "pricing.html", media_type="text/html")


@app.get("/dashboard")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


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
            "/api/auth/signup",
            "/api/auth/login",
            "/api/sites",
            "/api/plans",
            "/api/admin/plans",
            "/api/billing/create-subscription",
            "/api/billing/verify",
            "/api/billing/webhook",
            "/api/register",
            "/api/status",
            "/api/chat",
            "/widget.js",
            "/health",
        ],
    }

"""Accounts, installation tokens, subscriptions, sessions, and chat quotas.

Postgres is the production store. The intentionally small in-memory store keeps
local development usable without DATABASE_URL; it is process-local and must not
be used for a multi-replica deployment.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import HTTPException, Request

from .config import get_settings
from .crawler import normalize_domain

_settings = get_settings()
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_memory: dict[str, dict[str, Any]] = {"accounts": {}, "sessions": {}, "sites": {}, "usage": {}}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','admin')), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, name TEXT NOT NULL, chat_limit INTEGER NOT NULL, website_limit INTEGER NOT NULL DEFAULT 1, indexed_page_limit INTEGER NOT NULL DEFAULT 100, price_amount INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'INR', billing_interval TEXT NOT NULL DEFAULT 'monthly', razorpay_plan_id TEXT UNIQUE, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS subscriptions (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, plan_id TEXT NOT NULL REFERENCES plans(id), status TEXT NOT NULL, start_date TIMESTAMPTZ, end_date TIMESTAMPTZ, current_period_start TIMESTAMPTZ, current_period_end TIMESTAMPTZ, razorpay_customer_id TEXT UNIQUE, razorpay_subscription_id TEXT UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS websites (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, domain TEXT NOT NULL UNIQUE, installation_token_hash TEXT NOT NULL UNIQUE, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS usage (website_id UUID NOT NULL REFERENCES websites(id) ON DELETE CASCADE, period TEXT NOT NULL, chat_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (website_id, period));
CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, received_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'customer';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS indexed_page_limit INTEGER NOT NULL DEFAULT 100;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS price_amount INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'INR';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS billing_interval TEXT NOT NULL DEFAULT 'monthly';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS razorpay_plan_id TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS razorpay_subscription_id TEXT UNIQUE;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_date TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_date TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_start TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
INSERT INTO plans(id, name, chat_limit) VALUES ('starter', 'Starter', 500) ON CONFLICT (id) DO NOTHING;
"""

def using_db() -> bool:
    return bool(_settings.database_url)

def _conn():
    conn = psycopg.connect(_settings.database_url, autocommit=True, prepare_threshold=None)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute(SCHEMA)
    return conn

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + digest.hex()

def _check_password(password: str, encoded: str) -> bool:
    try:
        _, salt, digest = encoded.split("$", 2)
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")

def _expiry() -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=_settings.session_ttl_seconds)

def _active(status: str, end: Any) -> bool:
    return status in {"active", "trialing"} and end is not None and end > datetime.now(timezone.utc)

def signup(email: str, password: str, *, role: str = "customer", admin_code: str = "") -> dict:
    email = email.strip().lower()
    if role not in {"customer", "admin"}:
        raise HTTPException(400, "Invalid account role")
    if role == "admin":
        if not _settings.admin_registration_code or not hmac.compare_digest(admin_code, _settings.admin_registration_code):
            raise HTTPException(403, "Valid administrator registration code required")
    if using_db():
        try:
            with _conn() as conn, conn.cursor() as cur:
                try:
                    cur.execute("INSERT INTO accounts(email,password_hash,role) VALUES (%s,%s,%s) RETURNING id", (email, _hash_password(password), role))
                except psycopg.errors.UniqueViolation:
                    raise HTTPException(409, "Email already registered")
                account_id = str(cur.fetchone()[0])
                trial_start = datetime.now(timezone.utc)
                trial_end = trial_start + timedelta(days=30)
                cur.execute(
                    "INSERT INTO subscriptions(account_id,plan_id,status,start_date,end_date,current_period_start,current_period_end) "
                    "VALUES (%s,'starter','trialing',%s,%s,%s,%s)",
                    (account_id, trial_start, trial_end, trial_start, trial_end),
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Account registration failed")
            raise HTTPException(503, "Account service is unavailable. Check the database connection.") from exc
    else:
        with _lock:
            if email in _memory["accounts"]:
                raise HTTPException(409, "Email already registered")
            account_id = secrets.token_hex(16)
            _memory["accounts"][email] = {"id": account_id, "password": _hash_password(password), "role": role, "subscription": {"status": "active", "limit": _settings.starter_chat_limit}}
    return {"account_id": account_id, "email": email}

def login(email: str, password: str) -> str:
    email = email.strip().lower()
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,password_hash FROM accounts WHERE email=%s", (email,)); row = cur.fetchone()
            if not row or not _check_password(password, row[1]): raise HTTPException(401, "Invalid credentials")
            token = secrets.token_urlsafe(32); cur.execute("INSERT INTO sessions(token_hash,account_id,expires_at) VALUES (%s,%s,%s)", (_token_hash(token), row[0], _expiry())); return token
    with _lock:
        account = _memory["accounts"].get(email)
        if not account or not _check_password(password, account["password"]): raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32); _memory["sessions"][token] = {"account_id": account["id"], "expires_at": time.time()+_settings.session_ttl_seconds}; return token

def account_for_request(request: Request) -> str:
    token = request.cookies.get("session") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token: raise HTTPException(401, "Authentication required")
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT account_id FROM sessions WHERE token_hash=%s AND expires_at>now()", (_token_hash(token),)); row = cur.fetchone()
            if not row: raise HTTPException(401, "Session expired")
            return str(row[0])
    with _lock:
        session = _memory["sessions"].get(token)
        if not session or session["expires_at"] <= time.time(): raise HTTPException(401, "Session expired")
        return session["account_id"]

def logout(request: Request) -> None:
    token = request.cookies.get("session") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash=%s", (_token_hash(token),))
    else:
        with _lock:
            _memory["sessions"].pop(token, None)


def account_info(account_id: str) -> dict:
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,email,role,created_at FROM accounts WHERE id=%s", (account_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(401, "Account not found")
        return {"id": str(row[0]), "email": row[1], "role": row[2], "created_at": row[3]}
    with _lock:
        account = next((a for a in _memory["accounts"].values() if a["id"] == account_id), None)
    if not account:
        raise HTTPException(401, "Account not found")
    return {"id": account_id, "email": next(email for email, value in _memory["accounts"].items() if value is account)}
def create_site(account_id: str, domain: str) -> dict:
    domain = normalize_domain(domain)
    if not domain or "." not in domain:
        raise HTTPException(400, "Enter a valid public domain")
    installation_token = secrets.token_urlsafe(32)
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,account_id FROM websites WHERE domain=%s", (domain,))
            existing = cur.fetchone()
            if existing:
                if str(existing[1]) != account_id:
                    raise HTTPException(409, "This domain is already connected to another account")
                raise HTTPException(409, "This domain is already connected to your account")
            cur.execute(
                "INSERT INTO websites(account_id,domain,installation_token_hash,active) "
                "VALUES (%s,%s,%s,TRUE) RETURNING id",
                (account_id, domain, _token_hash(installation_token)),
            )
            site_id = str(cur.fetchone()[0])
    else:
        with _lock:
            if domain in _memory["sites"]:
                raise HTTPException(409, "This domain is already connected")
            site_id = secrets.token_hex(16)
            _memory["sites"][domain] = {
                "id": site_id,
                "account_id": account_id,
                "token_hash": _token_hash(installation_token),
            }
    return {"domain": domain, "installation_token": installation_token, "site_id": site_id}


def site_from_token(token: str, domain: str) -> tuple[str, str]:
    domain = normalize_domain(domain)
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT w.id,w.account_id FROM websites w WHERE w.domain=%s AND w.installation_token_hash=%s AND w.active", (domain,_token_hash(token)))
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "Invalid installation token")
            return str(row[0]), str(row[1])
    site = _memory["sites"].get(domain)
    if not site or not hmac.compare_digest(site["token_hash"], _token_hash(token)):
        raise HTTPException(401, "Invalid installation token")
    return site["id"], site["account_id"]

def require_site(request: Request, domain: str) -> tuple[str, str]:
    """Resolve a registered website only from its installation token.

    Domain-only requests are rejected in every environment so old widget embeds
    cannot crawl or chat. The token hash and normalized domain must match the
    same website row.
    """
    token = request.headers.get("X-Installation-Token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Installation token required")
    return site_from_token(token, domain)

def require_active_subscription(account_id: str, domain: str) -> None:
    """Fail closed for production requests without an active subscription."""
    if account_id == "local":
        return
    if not using_db():
        with _lock:
            account = next((a for a in _memory["accounts"].values() if a["id"] == account_id), None)
            if not account or not _active(account["subscription"]["status"], None):
                raise HTTPException(403, f"Subscription ended, Please connect {domain}")
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.status, s.current_period_end "
            "FROM subscriptions s WHERE s.account_id = %s "
            "ORDER BY s.updated_at DESC LIMIT 1",
            (account_id,),
        )
        row = cur.fetchone()
    if not row or not _active(row[0], row[1]):
        raise HTTPException(403, f"Subscription ended, Please connect {domain}")


def enforce_chat_quota(site_id: str, account_id: str, domain: str) -> None:
    if account_id == "local":
        return
    if not using_db():
        with _lock:
            key = (site_id, _period()); count = _memory["usage"].get(key, 0)
            account = next((a for a in _memory["accounts"].values() if a["id"] == account_id), None)
            if not account or not _active(account["subscription"]["status"], None): raise HTTPException(402, "Subscription required")
            if count >= account["subscription"]["limit"]: raise HTTPException(429, "Chat quota exceeded")
            _memory["usage"][key] = count + 1
        return
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT s.status,s.current_period_start,s.current_period_end,p.chat_limit FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.account_id=%s ORDER BY s.updated_at DESC LIMIT 1", (account_id,)); row=cur.fetchone()
        if not row or not _active(row[0], row[2]):
            raise HTTPException(403, f"Subscription ended, Please connect {domain}")
        period = row[1].isoformat() if row[1] else _period()
        cur.execute("INSERT INTO usage(website_id,period,chat_count) VALUES (%s,%s,1) ON CONFLICT (website_id,period) DO UPDATE SET chat_count=usage.chat_count+1 WHERE usage.chat_count < %s RETURNING chat_count", (site_id,period,row[3]))
        if cur.fetchone() is None:
            raise HTTPException(429, "Your monthly chat limit has been reached. Please upgrade your plan.")

def mark_webhook(event_id: str) -> bool:
    if not using_db(): return True
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO webhook_events(event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_id", (event_id,)); return cur.fetchone() is not None

def apply_subscription(customer_id: str, subscription_id: str, status: str, period_end: int | None = None, account_id: str = "") -> None:
    if not using_db(): return
    end = datetime.fromtimestamp(period_end, timezone.utc) if period_end else None
    with _conn() as conn, conn.cursor() as cur: cur.execute("UPDATE subscriptions SET status=%s,current_period_end=%s,razorpay_customer_id=NULLIF(%s,''),razorpay_subscription_id=NULLIF(%s,''),updated_at=now() WHERE account_id::text=%s OR razorpay_customer_id=%s OR razorpay_subscription_id=%s", (status,end,customer_id,subscription_id,account_id,customer_id,subscription_id))


def _admin_allowed(account_id: str) -> bool:
    if using_db():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT role,email FROM accounts WHERE id=%s", (account_id,))
            row = cur.fetchone()
        if row and row[0] == "admin":
            return True
    with _lock:
        for email, value in _memory["accounts"].items():
            if value["id"] == account_id and value.get("role") == "admin":
                return True
    return False


def require_admin(account_id: str) -> None:
    if not _admin_allowed(account_id):
        raise HTTPException(403, "Administrator access required")


def list_plans(*, active_only: bool = True) -> list[dict]:
    if not using_db():
        return [{"id": "starter", "name": "Starter", "chat_limit": _settings.starter_chat_limit, "website_limit": 1, "indexed_page_limit": 100, "price_amount": 0, "currency": "INR", "billing_interval": "monthly", "razorpay_plan_id": "", "active": True}]
    with _conn() as conn, conn.cursor() as cur:
        query = "SELECT id,name,chat_limit,website_limit,indexed_page_limit,price_amount,currency,billing_interval,razorpay_plan_id,active FROM plans"
        if active_only:
            query += " WHERE active=TRUE"
        query += " ORDER BY price_amount,id"
        cur.execute(query)
        rows = cur.fetchall()
    keys = ("id", "name", "chat_limit", "website_limit", "indexed_page_limit", "price_amount", "currency", "billing_interval", "razorpay_plan_id", "active")
    return [dict(zip(keys, row)) for row in rows]


def create_plan(plan: dict) -> dict:
    if not using_db():
        raise HTTPException(503, "Plans require the configured PostgreSQL database")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT razorpay_plan_id FROM plans WHERE id=%s", (plan["id"],))
        existing = cur.fetchone()
        if existing and existing[0]:
            raise HTTPException(409, "A plan with this ID already exists")
        if existing:
            cur.execute(
                "UPDATE plans SET name=%s,chat_limit=%s,website_limit=%s,indexed_page_limit=%s,price_amount=%s,currency=%s,billing_interval=%s,razorpay_plan_id=%s,active=TRUE WHERE id=%s",
                (plan["name"], plan["chat_limit"], plan["website_limit"], plan["indexed_page_limit"], plan["price_amount"], plan["currency"], plan["billing_interval"], plan["razorpay_plan_id"], plan["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO plans(id,name,chat_limit,website_limit,indexed_page_limit,price_amount,currency,billing_interval,razorpay_plan_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (plan["id"], plan["name"], plan["chat_limit"], plan["website_limit"], plan["indexed_page_limit"], plan["price_amount"], plan["currency"], plan["billing_interval"], plan["razorpay_plan_id"]),
            )
    return next(item for item in list_plans(active_only=False) if item["id"] == plan["id"])


def razorpay_plan_id(plan_id: str) -> str:
    if not using_db():
        if plan_id == "starter":
            return _settings.razorpay_plan_starter
        return ""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT razorpay_plan_id FROM plans WHERE id=%s AND active=TRUE", (plan_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "Selected plan is unavailable")
    return row[0]


def is_admin(account_id: str) -> bool:
    return _admin_allowed(account_id)

"""Isolated lead-capture services. This module does not modify the RAG pipeline."""
from __future__ import annotations
import json
import logging
import re
import secrets
import smtplib
from email.message import EmailMessage
from openai import OpenAI
from .config import get_settings
from . import db

logger = logging.getLogger(__name__)
settings = get_settings()
_client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

_INTENT_PROMPT = """Classify whether the visitor shows genuine commercial buying intent. Return JSON only with keys intent (boolean), confidence (0 to 1), category, and reason. Consider the latest message and prior context. Questions asking for information alone are not consent to contact them."""
_INTENT_TERMS = {
    "sales": ("consultation", "schedule", "book", "appointment", "inquire", "connect", "callback", "evaluate", "audit", "assessment", "contact sales", "call me", "talk to team", "proposal", "quotation", "demo"),
    "pricing": ("pricing", "plans", "cost", "rate", "fee", "budget", "estimate", "discount", "cheaper", "affordable"),
    "trial": ("trial", "sandbox", "access", "account", "setup", "install", "deploy", "integrate"),
    "purchase": ("buy", "purchase", "order", "subscribe", "upgrade", "renew", "premium", "license"),
    "evaluation": ("compare", "versus", "alternative", "review", "features", "capability", "requirements", "sla", "procurement"),
}


def _fallback_intent(message: str, context: str = "") -> dict:
    text = f"{context} {message}".lower()
    scores = {category: sum(1 for term in terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)) for category, terms in _INTENT_TERMS.items()}
    category, score = max(scores.items(), key=lambda item: item[1])
    consent_language = any(phrase in text for phrase in ("contact me", "call me", "talk to me", "send me", "i want a demo", "schedule a demo"))
    return {"intent": bool(score and (consent_language or category in {"sales", "trial", "purchase"})), "confidence": min(0.95, 0.55 + score * 0.1) if score else 0.1, "category": category if score else "general", "reason": "commercial intent signals detected" if score else "no clear commercial intent"}


def analyze_intent(message: str, context: str = "") -> dict:
    if not _client:
        return _fallback_intent(message, context)
    try:
        response = _client.chat.completions.create(model=settings.openai_chat_model, messages=[{"role": "system", "content": _INTENT_PROMPT}, {"role": "user", "content": json.dumps({"latest_message": message, "recent_context": context[-4000:]})}], temperature=0, response_format={"type": "json_object"})
        value = json.loads(response.choices[0].message.content or "{}")
        return {"intent": bool(value.get("intent")), "confidence": float(value.get("confidence", 0)), "category": str(value.get("category", "general"))[:80], "reason": str(value.get("reason", ""))[:300]}
    except Exception:
        logger.exception("Lead intent analysis failed; using fallback classifier")
        return _fallback_intent(message, context)


def summarize_lead(name: str, interested_in: str, context: str = "") -> str:
    fallback = f"{name or 'Visitor'} is interested in {interested_in.strip()}."
    if not _client:
        return fallback[:500]
    try:
        response = _client.chat.completions.create(model=settings.openai_chat_model, messages=[{"role": "system", "content": "Write one short factual lead summary in under  twenty words. Do not invent details."}, {"role": "user", "content": json.dumps({"name": name, "interest": interested_in, "context": context[-3000:]})}], temperature=0.1)
        return (response.choices[0].message.content or fallback).strip()[:500]
    except Exception:
        logger.exception("Lead summary generation failed; using fallback summary")
        return fallback[:500]


def get_settings_for_tenant(tenant_id: str):
    with db._get_pool().connection() as connection:
        return connection.execute("SELECT * FROM lead_capture_settings WHERE tenant_id=%s", (tenant_id,)).fetchone()


def save_settings(tenant_id: str, enabled: bool, notification_email: str):
    stamp = db.now(); record_id = secrets.token_urlsafe(18)
    with db._get_pool().connection() as connection:
        with connection.transaction():
            return connection.execute("""INSERT INTO lead_capture_settings (id, tenant_id, enabled, notification_email, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id) DO UPDATE SET enabled=EXCLUDED.enabled, notification_email=EXCLUDED.notification_email, updated_at=EXCLUDED.updated_at
                RETURNING *""", (record_id, tenant_id, enabled, notification_email, stamp, stamp)).fetchone()


def create_lead(tenant_id: str, conversation_id: str | None, name: str, email: str, phone: str, interested_in: str, context: str = ""):
    summary = summarize_lead(name, interested_in, context)
    lead_id = secrets.token_urlsafe(18); stamp = db.now()
    with db._get_pool().connection() as connection:
        with connection.transaction():
            lead = connection.execute("""INSERT INTO leads (id, tenant_id, conversation_id, name, email, phone, interested_in, lead_summary, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'new',%s,%s) RETURNING *""", (lead_id, tenant_id, conversation_id, name, email, phone, interested_in, summary, stamp, stamp)).fetchone()
    notify_lead(lead)
    return lead


def notify_lead(lead) -> bool:
    configuration = get_settings_for_tenant(lead["tenant_id"])
    if configuration and not configuration["enabled"]:
        return False
    recipient = getattr(settings, "lead_notification_email", "") or (configuration["notification_email"] if configuration else "") or ""
    if not recipient:
        with db._get_pool().connection() as connection:
            owner = connection.execute("SELECT email FROM chatbots WHERE id=%s", (lead["tenant_id"],)).fetchone()
        recipient = (owner["email"] if owner else "") or ""
    if not recipient:
        logger.warning("Lead notification skipped because the site owner has no email")
        return False
    host = getattr(settings, "smtp_host", "")
    if not host:
        logger.warning("Lead notification skipped because SMTP_HOST is not configured")
        return False
    message = EmailMessage(); message["Subject"] = "New Lead Received"; message["To"] = recipient; message["From"] = getattr(settings, "smtp_from", "") or recipient
    message.set_content(f"New Lead Received\n\nName: {lead['name']}\nEmail: {lead['email']}\nPhone: {lead['phone']}\nInterested In: {lead['interested_in']}\n\nLead Summary:\n{lead['lead_summary']}\n")
    try:
        with smtplib.SMTP(host, int(getattr(settings, "smtp_port", 587)), timeout=10) as smtp:
            if getattr(settings, "smtp_tls", True): smtp.starttls()
            username = getattr(settings, "smtp_username", "")
            password = getattr(settings, "smtp_password", "")
            if username: smtp.login(username, password)
            smtp.send_message(message)
        with db._get_pool().connection() as connection:
            with connection.transaction(): connection.execute("UPDATE leads SET status='notified', updated_at=%s WHERE id=%s", (db.now(), lead["id"]))
        return True
    except Exception:
        logger.exception("Lead notification failed for %s", lead["id"])
        return False


def list_leads(tenant_id: str | None = None, limit: int = 50, offset: int = 0):
    with db._get_pool().connection() as connection:
        if tenant_id:
            return connection.execute("SELECT * FROM leads WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s", (tenant_id, limit, offset)).fetchall()
        return connection.execute("SELECT * FROM leads ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()


def get_lead(lead_id: str):
    with db._get_pool().connection() as connection:
        return connection.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()

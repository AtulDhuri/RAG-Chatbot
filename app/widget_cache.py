"""Fast domain authorization cache with PostgreSQL fallback."""
from __future__ import annotations
import threading, time
from datetime import datetime, timezone
from . import db
from .config import get_settings
from .security import _registered_origin, reject

_lock=threading.RLock(); _entries={}

def _cache_entry(data):
    data['cached_at']=time.monotonic(); return data

def normalize_domain(value: str) -> str:
    return value.strip().lower().removeprefix('https://').removeprefix('http://').removeprefix('www.').split('/')[0].split(':')[0]

def invalidate(chatbot_id: str | None = None, domain: str | None = None):
    with _lock:
        if chatbot_id is None and domain is None: _entries.clear(); return
        for key,entry in list(_entries.items()):
            if (chatbot_id and entry.get('chatbot_id')==chatbot_id) or (domain and key==normalize_domain(domain)): _entries.pop(key,None)

def refresh(rows=None):
    if rows is None: rows=db.active_chatbots()
    fresh={}
    for bot in rows:
        fresh[normalize_domain(bot['allowed_domain'])]=_cache_entry({'chatbot_id':bot['id'],'status':bot['status'],'subscription_status':'active','knowledge_status':bot.get('knowledge_status'),'expires_at':bot['subscription_end_date']})
    with _lock: _entries.clear(); _entries.update(fresh)

def _not_allowed(request, chatbot_id, event, reason, domain):
    reject(request,chatbot_id,event,reason,domain)

def validate(request, chatbot_id: str, current_domain: str):
    """Authorize a chatbot only when its ID and page hostname match in PostgreSQL."""
    domain=normalize_domain(current_domain)
    if not domain or not _registered_origin(domain, request.headers.get('origin')):
        _not_allowed(request,chatbot_id,'invalid_domain','origin mismatch',domain)
    referer=request.headers.get('referer')
    if referer and not _registered_origin(domain,referer): _not_allowed(request,chatbot_id,'invalid_referer','referer mismatch',domain)

    # Always perform the authoritative ID-to-domain lookup. Do not trust a
    # process-local cache for tenant identity: a copied script must fail when
    # it is embedded on a different domain, even during cache transitions.
    bot=db.get_chatbot_by_domain(domain)
    if not bot or bot['id'] != chatbot_id:
        _not_allowed(request,chatbot_id,'invalid_domain','chatbot/domain mismatch',domain)

    from .security import validate_chatbot
    validate_chatbot(request,chatbot_id)
    entry=_cache_entry({'chatbot_id':bot['id'],'status':bot['status'],'subscription_status':'active','knowledge_status':bot.get('knowledge_status'),'expires_at':bot['subscription_end_date']})
    with _lock: _entries[domain]=entry

    now=datetime.now(timezone.utc)
    try: end=datetime.fromisoformat(entry['expires_at']); end=end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError): _not_allowed(request,chatbot_id,'expired_subscription','invalid expiry',domain)
    if end < now: invalidate(chatbot_id,domain); _not_allowed(request,chatbot_id,'expired_subscription','expired',domain)
    return entry

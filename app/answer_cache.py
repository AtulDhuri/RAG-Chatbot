"""Bounded process-local cache for successful RAG answers."""
from __future__ import annotations
import hashlib, threading, time
from .config import get_settings

_lock=threading.RLock(); _items={}; _max_items=2048

def _key(chatbot_id,version,question):
    normalized=' '.join(question.lower().split())
    return hashlib.sha256(f'{chatbot_id}|{version}|{normalized}|{get_settings().openai_chat_model}'.encode()).hexdigest()

def get(chatbot_id,version,question):
    key=_key(chatbot_id,version,question); now=time.monotonic()
    with _lock:
        item=_items.get(key)
        if not item or item['expires']<=now:
            _items.pop(key,None); return None
        item['used']=now; return dict(item['value'])

def set(chatbot_id,version,question,value):
    now=time.monotonic(); key=_key(chatbot_id,version,question)
    with _lock:
        _items[key]={'value':dict(value),'expires':now+get_settings().answer_cache_ttl_seconds,'used':now}
        if len(_items)>_max_items:
            oldest=min(_items,key=lambda k:_items[k]['used']); _items.pop(oldest,None)

def clear_chatbot(chatbot_id):
    with _lock:
        for key in list(_items):
            # chatbot id is hashed, so clear all is the safe invalidation boundary.
            _items.pop(key,None)

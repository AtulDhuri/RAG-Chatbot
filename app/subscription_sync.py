"""Nightly UTC subscription reconciliation and domain-cache refresh."""
from __future__ import annotations
import logging, threading
from datetime import datetime, timedelta, timezone
from . import db
from .widget_cache import refresh

logger=logging.getLogger(__name__); _started=False

def sync_once():
    rows=db.sync_subscription_statuses(); refresh(rows); logger.info('Subscription sync completed: %d active domains cached',len(rows))

def _loop():
    while True:
        now=datetime.now(timezone.utc); next_midnight=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        threading.Event().wait(max(1,(next_midnight-now).total_seconds()))
        try: sync_once()
        except Exception: logger.exception('Nightly subscription sync failed')

def start():
    global _started
    if _started:return
    _started=True
    try: sync_once()
    except Exception: logger.exception('Initial subscription cache refresh failed')
    threading.Thread(target=_loop,name='subscription-sync',daemon=True).start()

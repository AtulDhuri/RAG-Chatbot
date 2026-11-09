"""Background cleanup for the 14-day conversation retention policy."""
from __future__ import annotations
import logging
import threading
from . import db

logger = logging.getLogger(__name__)
_started = False


def purge_once() -> int:
    deleted = db.purge_expired_conversations(14)
    if deleted:
        logger.info("Purged %d conversations older than 14 days", deleted)
    return deleted


def _loop() -> None:
    while True:
        threading.Event().wait(3600)
        try:
            purge_once()
        except Exception:
            logger.exception("Conversation retention purge failed")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    try:
        purge_once()
    except Exception:
        logger.exception("Initial conversation retention purge failed")
    threading.Thread(target=_loop, name="conversation-retention", daemon=True).start()

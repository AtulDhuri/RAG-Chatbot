"""Tenant-scoped customer portal services and analytics aggregation."""
from __future__ import annotations
import hashlib, hmac, json, logging, secrets
from datetime import datetime, timedelta, timezone
from . import db

logger = logging.getLogger(__name__)
_SESSION_TTL = timedelta(hours=8)

def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"

def _verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, digest = encoded.split('$', 2)
        candidate = _hash_password(password, bytes.fromhex(salt)).split('$', 2)[2]
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False

def create_credentials(tenant_id: str, password: str):
    if len(password) < 10: raise ValueError('Password must contain at least 10 characters')
    stamp = db.now(); credential_id = secrets.token_urlsafe(18); client_id = 'cli_' + secrets.token_urlsafe(12)
    with db._get_pool().connection() as c:
        with c.transaction():
            return c.execute("""INSERT INTO portal_credentials (id,tenant_id,client_id,password_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id) DO UPDATE SET password_hash=EXCLUDED.password_hash, enabled=TRUE, updated_at=EXCLUDED.updated_at RETURNING id,tenant_id,client_id,enabled,created_at,updated_at""", (credential_id,tenant_id,client_id,_hash_password(password),stamp,stamp)).fetchone()

def login(client_id: str, password: str):
    with db._get_pool().connection() as c:
        credential = c.execute('SELECT * FROM portal_credentials WHERE client_id=%s AND enabled=TRUE', (client_id,)).fetchone()
    if not credential or not _verify_password(password, credential['password_hash']): return None
    raw = secrets.token_urlsafe(32); stamp = datetime.now(timezone.utc); expires = stamp + _SESSION_TTL
    with db._get_pool().connection() as c:
        with c.transaction(): c.execute('INSERT INTO portal_sessions (id,credential_id,session_hash,expires_at,created_at,last_seen_at) VALUES (%s,%s,%s,%s,%s,%s)', (secrets.token_urlsafe(18),credential['id'],hashlib.sha256(raw.encode()).hexdigest(),expires.isoformat(),stamp.isoformat(),stamp.isoformat()))
    return {'token':raw,'expires_at':expires.isoformat(),'tenant_id':credential['tenant_id'],'client_id':credential['client_id']}

def session(token: str):
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db._get_pool().connection() as c:
        row = c.execute("SELECT s.*,p.tenant_id,p.client_id FROM portal_sessions s JOIN portal_credentials p ON p.id=s.credential_id WHERE s.session_hash=%s AND s.expires_at > %s AND p.enabled=TRUE", (digest,db.now())).fetchone()
        if row:
            with c.transaction(): c.execute('UPDATE portal_sessions SET last_seen_at=%s WHERE id=%s', (db.now(),row['id']))
    return row

def create_conversation(tenant_id: str, conversation_id: str | None = None, visitor_id: str | None = None, source_page_url: str | None = None):
    conversation_id = conversation_id or secrets.token_urlsafe(18); stamp = db.now()
    with db._get_pool().connection() as c:
        with c.transaction():
            existing=c.execute('SELECT tenant_id FROM chat_conversations WHERE id=%s FOR UPDATE',(conversation_id,)).fetchone()
            if existing and existing['tenant_id'] != tenant_id:
                conversation_id=secrets.token_urlsafe(18)
            c.execute("INSERT INTO chat_conversations (id,tenant_id,visitor_id,source_page_url,started_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING", (conversation_id,tenant_id,visitor_id,source_page_url,stamp,stamp,stamp))
    return conversation_id

def record_exchange(tenant_id: str, conversation_id: str, question: str, answer: str, sources=None, answer_status='answered', source_page_url=None):
    now = db.now(); question_id = secrets.token_urlsafe(18); answer_id = secrets.token_urlsafe(18); analytics_id = secrets.token_urlsafe(18); sources_json=json.dumps(sources or [])
    conversation_id=create_conversation(tenant_id,conversation_id,source_page_url=source_page_url)
    with db._get_pool().connection() as c:
        with c.transaction():
            c.execute("INSERT INTO chat_messages (id,conversation_id,tenant_id,role,content,source_urls,answer_status,created_at) VALUES (%s,%s,%s,'visitor',%s,'[]',NULL,%s),(%s,%s,%s,'assistant',%s,%s,%s,%s)", (question_id,conversation_id,tenant_id,question,now,answer_id,conversation_id,tenant_id,answer,sources_json,answer_status,now))
            c.execute("INSERT INTO chat_analytics (id,tenant_id,conversation_id,message_id,source_page_url,question,answer_status,fallback_used,normalized_question,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (analytics_id,tenant_id,conversation_id,answer_id,source_page_url,question,answer_status,answer_status != 'answered',' '.join(question.lower().split()),now))
            c.execute("UPDATE chat_conversations SET message_count=message_count+2,updated_at=%s WHERE id=%s AND tenant_id=%s", (now,conversation_id,tenant_id))
            if source_page_url: c.execute("INSERT INTO page_analytics (id,tenant_id,page_url,conversations_started,question_count,last_seen_at,created_at,updated_at) VALUES (%s,%s,%s,1,1,%s,%s,%s) ON CONFLICT (tenant_id,page_url) DO UPDATE SET question_count=page_analytics.question_count+1,last_seen_at=EXCLUDED.last_seen_at,updated_at=EXCLUDED.updated_at", (secrets.token_urlsafe(18),tenant_id,source_page_url,now,now,now))
    return analytics_id

def dashboard(tenant_id: str):
    with db._get_pool().connection() as c:
        def one(sql,args=()): return c.execute(sql,args).fetchone()
        total=one('SELECT count(*) AS value FROM chat_conversations WHERE tenant_id=%s',(tenant_id,))['value']
        leads=one('SELECT count(*) AS value FROM leads WHERE tenant_id=%s',(tenant_id,))['value']
        unanswered=one("SELECT count(*) AS value FROM chat_analytics WHERE tenant_id=%s AND answer_status <> 'answered'",(tenant_id,))['value']
        topics=c.execute("SELECT COALESCE(topic,'Other') AS topic,count(*) AS count FROM chat_analytics WHERE tenant_id=%s GROUP BY 1 ORDER BY count DESC LIMIT 10",(tenant_id,)).fetchall()
        recent=c.execute('SELECT name,email,interested_in,lead_summary,created_at FROM leads WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 5',(tenant_id,)).fetchall()
        return {'total_conversations':total,'total_leads':leads,'lead_conversion_percent':round(leads*100/total,2) if total else 0,'unanswered_questions':unanswered,'top_topics':[dict(x) for x in topics],'recent_leads':[dict(x) for x in recent]}

def conversations(tenant_id: str, limit=50, offset=0):
    with db._get_pool().connection() as c: return c.execute('SELECT * FROM chat_conversations WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s OFFSET %s',(tenant_id,limit,offset)).fetchall()

def conversation(tenant_id: str, conversation_id: str):
    with db._get_pool().connection() as c:
        result=c.execute('SELECT * FROM chat_conversations WHERE tenant_id=%s AND id=%s',(tenant_id,conversation_id)).fetchone()
        if not result:return None
        messages=c.execute('SELECT * FROM chat_messages WHERE tenant_id=%s AND conversation_id=%s ORDER BY created_at',(tenant_id,conversation_id)).fetchall()
        return {'conversation':dict(result),'messages':[dict(x) for x in messages]}

def unanswered(tenant_id: str, limit=50):
    with db._get_pool().connection() as c: return c.execute('SELECT * FROM unanswered_questions WHERE tenant_id=%s ORDER BY frequency DESC,last_seen_at DESC LIMIT %s',(tenant_id,limit)).fetchall()

def most_asked(tenant_id: str, limit=50):
    with db._get_pool().connection() as c: return c.execute('SELECT * FROM question_groups WHERE tenant_id=%s ORDER BY question_count DESC LIMIT %s',(tenant_id,limit)).fetchall()

def topics(tenant_id: str):
    with db._get_pool().connection() as c: return c.execute("SELECT COALESCE(topic,'Other') AS topic,count(*) AS count FROM chat_analytics WHERE tenant_id=%s GROUP BY 1 ORDER BY count DESC",(tenant_id,)).fetchall()

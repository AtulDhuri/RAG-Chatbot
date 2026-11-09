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

def _topic_for(question: str) -> str:
    """Assign a predictable topic without another model call."""
    text = question.lower()
    topics = {
        'Pricing': ('price', 'pricing', 'cost', 'fee', 'quote', 'plan'),
        'Services': ('service', 'offer', 'provide', 'solution', 'product'),
        'Hours & Location': ('hour', 'open', 'close', 'location', 'address', 'where'),
        'Support': ('help', 'support', 'issue', 'problem', 'repair', 'cancel'),
        'Contact': ('contact', 'call', 'email', 'phone', 'appointment', 'book'),
    }
    for topic, keywords in topics.items():
        if any(keyword in text for keyword in keywords):
            return topic
    return 'Other'


def record_exchange(tenant_id: str, conversation_id: str, question: str, answer: str, sources=None, answer_status='answered', source_page_url=None, visitor_id=None):
    now = db.now(); question_id = secrets.token_urlsafe(18); answer_id = secrets.token_urlsafe(18); analytics_id = secrets.token_urlsafe(18); sources_json=json.dumps(sources or [])
    normalized_question = ' '.join(question.lower().split())
    topic = _topic_for(question)
    conversation_id=create_conversation(tenant_id,conversation_id,visitor_id=visitor_id,source_page_url=source_page_url)
    with db._get_pool().connection() as c:
        with c.transaction():
            c.execute("INSERT INTO chat_messages (id,conversation_id,tenant_id,role,content,source_urls,answer_status,created_at) VALUES (%s,%s,%s,'visitor',%s,'[]',NULL,%s),(%s,%s,%s,'assistant',%s,%s,%s,%s)", (question_id,conversation_id,tenant_id,question,now,answer_id,conversation_id,tenant_id,answer,sources_json,answer_status,now))
            c.execute("INSERT INTO chat_analytics (id,tenant_id,conversation_id,message_id,source_page_url,question,answer_status,fallback_used,topic,normalized_question,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (analytics_id,tenant_id,conversation_id,answer_id,source_page_url,question,answer_status,answer_status != 'answered',topic,normalized_question,now))
            group = c.execute("SELECT id FROM question_groups WHERE tenant_id=%s AND group_name=%s FOR UPDATE", (tenant_id,normalized_question)).fetchone()
            if group:
                c.execute("UPDATE question_groups SET question_count=question_count+1,last_seen_at=%s,updated_at=%s WHERE id=%s", (now,now,group['id']))
            else:
                group_id = secrets.token_urlsafe(18)
                c.execute("INSERT INTO question_groups (id,tenant_id,group_name,representative_question,question_count,last_seen_at,created_at,updated_at) VALUES (%s,%s,%s,%s,1,%s,%s,%s)", (group_id,tenant_id,normalized_question,question,now,now,now))
                group = {'id': group_id}
            c.execute("INSERT INTO question_group_members (id,group_id,analytics_id,similarity_score,created_at) VALUES (%s,%s,%s,1,%s)", (secrets.token_urlsafe(18),group['id'],analytics_id,now))
            if answer_status != 'answered':
                unanswered_row = c.execute("SELECT id FROM unanswered_questions WHERE tenant_id=%s AND normalized_question=%s FOR UPDATE", (tenant_id,normalized_question)).fetchone()
                if unanswered_row:
                    c.execute("UPDATE unanswered_questions SET frequency=frequency+1,last_seen_at=%s,updated_at=%s WHERE id=%s", (now,now,unanswered_row['id']))
                else:
                    c.execute("INSERT INTO unanswered_questions (id,tenant_id,analytics_id,question,normalized_question,frequency,suggested_action,first_seen_at,last_seen_at,updated_at) VALUES (%s,%s,%s,%s,%s,1,'Review and add this answer to the knowledge base',%s,%s,%s)", (secrets.token_urlsafe(18),tenant_id,analytics_id,question,normalized_question,now,now,now))
            c.execute("INSERT INTO topic_analytics (id,tenant_id,analytics_id,topic_name,confidence_score,created_at) VALUES (%s,%s,%s,%s,1,%s)", (secrets.token_urlsafe(18),tenant_id,analytics_id,topic,now))
            c.execute("UPDATE chat_conversations SET message_count=message_count+2,updated_at=%s WHERE id=%s AND tenant_id=%s", (now,conversation_id,tenant_id))
            if source_page_url: c.execute("INSERT INTO page_analytics (id,tenant_id,page_url,conversations_started,question_count,last_seen_at,created_at,updated_at) VALUES (%s,%s,%s,1,1,%s,%s,%s) ON CONFLICT (tenant_id,page_url) DO UPDATE SET question_count=page_analytics.question_count+1,last_seen_at=EXCLUDED.last_seen_at,updated_at=EXCLUDED.updated_at", (secrets.token_urlsafe(18),tenant_id,source_page_url,now,now,now))
    return analytics_id

def _range_bounds(date_from=None, date_to=None):
    end = datetime.now(timezone.utc) if not date_to else datetime.fromisoformat(date_to.replace('Z','+00:00'))
    start = end - timedelta(days=30) if not date_from else datetime.fromisoformat(date_from.replace('Z','+00:00'))
    if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None: end=end.replace(tzinfo=timezone.utc)
    if start >= end: raise ValueError('date_from must be before date_to')
    return start.isoformat(), end.isoformat()

def dashboard(tenant_id: str, date_from=None, date_to=None):
    start,end=_range_bounds(date_from,date_to)
    with db._get_pool().connection() as c:
        def count(sql,args): return c.execute(sql,args).fetchone()['value']
        conversations=count('SELECT count(*) AS value FROM chat_conversations WHERE tenant_id=%s AND started_at >= %s AND started_at < %s',(tenant_id,start,end))
        visitors=count("SELECT count(DISTINCT visitor_id) AS value FROM chat_conversations WHERE tenant_id=%s AND started_at >= %s AND started_at < %s AND visitor_id IS NOT NULL",(tenant_id,start,end))
        leads=count('SELECT count(*) AS value FROM leads WHERE tenant_id=%s AND created_at >= %s AND created_at < %s',(tenant_id,start,end))
        unanswered=count("SELECT count(*) AS value FROM unanswered_questions WHERE tenant_id=%s AND status IN ('open','reviewed')",(tenant_id,))
        topics=c.execute("SELECT COALESCE(topic,'Other') AS topic,count(*) AS count FROM chat_analytics WHERE tenant_id=%s AND created_at >= %s AND created_at < %s GROUP BY 1 ORDER BY count DESC LIMIT 10",(tenant_id,start,end)).fetchall()
        recent=c.execute('SELECT id,name,email,phone,company,interested_in,lead_score,lead_score_label,status,lead_summary,created_at FROM leads WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 5',(tenant_id,)).fetchall()
        kb=c.execute('SELECT knowledge_status,crawl_status,pages_crawled,pages_found,last_crawled_at,crawl_completed_at,error_details FROM knowledge_bases WHERE chatbot_id=%s',(tenant_id,)).fetchone()
        failed_pages=c.execute("SELECT count(*) AS value FROM ingestion_pages p JOIN ingestion_runs r ON r.id=p.run_id WHERE r.chatbot_id=%s AND p.status='failed'",(tenant_id,)).fetchone()['value']
        if kb: kb=dict(kb); kb['failed_pages']=failed_pages; kb['failed_documents']=None
        trend_start=(datetime.fromisoformat(start)- (datetime.fromisoformat(end)-datetime.fromisoformat(start))).isoformat()
        prev_conv=count('SELECT count(*) AS value FROM chat_conversations WHERE tenant_id=%s AND started_at >= %s AND started_at < %s',(tenant_id,trend_start,start))
        prev_leads=count('SELECT count(*) AS value FROM leads WHERE tenant_id=%s AND created_at >= %s AND created_at < %s',(tenant_id,trend_start,start))
        def trend(current,previous): return round((current-previous)*100/previous,1) if previous else None
        return {'range':{'from':start,'to':end},'total_visitors':visitors,'total_conversations':conversations,'total_leads':leads,'lead_conversion_percent':round(leads*100/conversations,2) if conversations else 0,'unanswered_questions':unanswered,'trends':{'visitors_percent':trend(visitors,count("SELECT count(DISTINCT visitor_id) AS value FROM chat_conversations WHERE tenant_id=%s AND started_at >= %s AND started_at < %s AND visitor_id IS NOT NULL",(tenant_id,trend_start,start))),'conversations_percent':trend(conversations,prev_conv),'leads_percent':trend(leads,prev_leads)},'top_topics':[dict(x) for x in topics],'recent_leads':[dict(x) for x in recent],'knowledge_base':dict(kb) if kb else None}

def conversations(tenant_id: str, limit=50, offset=0, date_from=None, date_to=None, search='', lead_status=''):
    start,end=_range_bounds(date_from,date_to); retention_cutoff=(datetime.now(timezone.utc)-timedelta(days=14)).isoformat(); start=max(start,retention_cutoff)
    where=['c.tenant_id=%s','c.started_at >= %s','c.started_at < %s']; args=[tenant_id,start,end]
    if search: where.append('(c.id ILIKE %s OR EXISTS (SELECT 1 FROM chat_messages m WHERE m.conversation_id=c.id AND m.content ILIKE %s))'); args += [f'%{search}%',f'%{search}%']
    if lead_status: where.append('l.status=%s'); args.append(lead_status)
    with db._get_pool().connection() as c:return c.execute('SELECT c.*,l.name AS lead_name,l.status AS lead_status FROM chat_conversations c LEFT JOIN leads l ON l.id=c.lead_id WHERE '+' AND '.join(where)+' ORDER BY c.started_at DESC LIMIT %s OFFSET %s',args+[limit,offset]).fetchall()

def conversation(tenant_id: str, conversation_id: str):
    cutoff=(datetime.now(timezone.utc)-timedelta(days=14)).isoformat()
    with db._get_pool().connection() as c:
        result=c.execute('SELECT * FROM chat_conversations WHERE tenant_id=%s AND id=%s AND started_at >= %s',(tenant_id,conversation_id,cutoff)).fetchone()
        if not result:return None
        messages=c.execute('SELECT * FROM chat_messages WHERE tenant_id=%s AND conversation_id=%s ORDER BY created_at',(tenant_id,conversation_id)).fetchall()
        return {'conversation':dict(result),'messages':[dict(x) for x in messages]}

def unanswered(tenant_id: str, limit=50, date_from=None, date_to=None, search='', status=''):
    start,end=_range_bounds(date_from,date_to); where=['tenant_id=%s','last_seen_at >= %s','last_seen_at < %s']; args=[tenant_id,start,end]
    if search: where.append('(question ILIKE %s OR suggested_action ILIKE %s)'); args += [f'%{search}%',f'%{search}%']
    if status: where.append('status=%s'); args.append(status)
    with db._get_pool().connection() as c:return c.execute('SELECT * FROM unanswered_questions WHERE '+' AND '.join(where)+' ORDER BY frequency DESC,last_seen_at DESC LIMIT %s',args+[limit]).fetchall()

def most_asked(tenant_id: str, limit=50, date_from=None, date_to=None, search=''):
    start,end=_range_bounds(date_from,date_to); where=['qg.tenant_id=%s','m.created_at >= %s','m.created_at < %s']; args=[tenant_id,start,end]
    if search: where.append('(qg.representative_question ILIKE %s OR qg.group_name ILIKE %s)'); args += [f'%{search}%',f'%{search}%']
    with db._get_pool().connection() as c:return c.execute('SELECT qg.id,qg.group_name,qg.representative_question,count(m.id) AS question_count,max(m.created_at) AS last_asked FROM question_groups qg JOIN question_group_members m ON m.group_id=qg.id WHERE '+' AND '.join(where)+' GROUP BY qg.id,qg.group_name,qg.representative_question ORDER BY question_count DESC,last_asked DESC LIMIT %s',args+[limit]).fetchall()

def topics(tenant_id: str, date_from=None, date_to=None):
    start,end=_range_bounds(date_from,date_to)
    with db._get_pool().connection() as c:return c.execute("SELECT COALESCE(topic,'Other') AS topic,count(*) AS count FROM chat_analytics WHERE tenant_id=%s AND created_at >= %s AND created_at < %s GROUP BY 1 ORDER BY count DESC",(tenant_id,start,end)).fetchall()

def update_lead(tenant_id, lead_id, fields):
    allowed={'status','company'}; fields={k:v for k,v in fields.items() if k in allowed and v is not None}
    if not fields:return None
    stamp=db.now(); fields['updated_at']=stamp; clause=', '.join(f'{k}=%s' for k in fields)
    with db._get_pool().connection() as c:
        with c.transaction(): return c.execute(f'UPDATE leads SET {clause} WHERE id=%s AND tenant_id=%s RETURNING *',(*fields.values(),lead_id,tenant_id)).fetchone()

def update_unanswered(tenant_id, question_id, status):
    with db._get_pool().connection() as c:
        with c.transaction(): return c.execute("UPDATE unanswered_questions SET status=%s,updated_at=%s WHERE id=%s AND tenant_id=%s RETURNING *",(status,db.now(),question_id,tenant_id)).fetchone()

def page_analytics(tenant_id, limit=50):
    with db._get_pool().connection() as c:return c.execute('SELECT page_url,conversations_started,leads_generated,question_count,last_seen_at FROM page_analytics WHERE tenant_id=%s ORDER BY question_count DESC LIMIT %s',(tenant_id,limit)).fetchall()

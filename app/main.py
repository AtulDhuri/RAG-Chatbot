"""Secure widget API and PostgreSQL-backed SaaS administration portal API."""
import csv, io, json, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from .config import get_settings
from .extractor import ContextDevExtractor
from . import auth, db, indexer, rag, vectorstore, widget_cache
from .subscription_sync import start as start_subscription_sync
from .security import validate_chatbot, reject

logging.basicConfig(level=logging.INFO); logger=logging.getLogger(__name__)
settings=get_settings(); limiter=Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(application):
    # Schema migrations run in Render's preDeployCommand, not in every web
    # process. Keeping startup read-only avoids DDL deadlocks during deploys.
    start_subscription_sync()
    yield
    db.close_pool()

app=FastAPI(title='Secure SaaS RAG Chatbot',version='3.0.0',lifespan=lifespan); app.state.limiter=limiter
STATIC_DIR=Path(__file__).parent/'static'

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request,exc): return JSONResponse(status_code=429,content={'success':False,'message':'Too many requests'})
@app.exception_handler(HTTPException)
async def http_error_handler(request,exc):
    if exc.status_code==403:return JSONResponse(status_code=403,content={'success':False,'message':'Chatbot unavailable'})
    return JSONResponse(status_code=exc.status_code,content=exc.detail if isinstance(exc.detail,dict) else {'detail':exc.detail})
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=False,allow_methods=['GET','POST','PUT','PATCH','OPTIONS'],allow_headers=['Authorization','Content-Type'])

class InitRequest(BaseModel): chatbot_id:str=Field(...,min_length=1,max_length=100)
class WidgetValidateRequest(BaseModel): chatbot_id:str=Field(...,min_length=1,max_length=100); current_domain:str=Field(...,min_length=1,max_length=255); widget_version:str=Field(...,min_length=1,max_length=40)
class ChatRequest(BaseModel): message:str=Field(...,min_length=1,max_length=2000)
class AdminLoginRequest(BaseModel): api_key:str=Field(...,min_length=1)
class ChatbotRequest(BaseModel):
    customer_name:str=Field(...,min_length=1,max_length=160); email:str=Field(...,min_length=3,max_length=254); mobile:str=''
    chatbot_name:str=Field(...,min_length=1,max_length=160); website_url:str=Field(...,min_length=1,max_length=1000); allowed_domain:str=Field(...,min_length=1,max_length=255)
    subscription_plan:str=Field(...,pattern='^(monthly|quarterly|yearly)$'); subscription_start_date:datetime; subscription_end_date:datetime; status:str=Field('active',pattern='^(active|suspended)$'); customer_id:str=''
class ChatbotUpdate(BaseModel):
    customer_name:str|None=None; email:str|None=None; mobile:str|None=None; chatbot_name:str|None=None; website_url:str|None=None; allowed_domain:str|None=None; subscription_plan:str|None=None; subscription_start_date:datetime|None=None; subscription_end_date:datetime|None=None; status:str|None=None
class RenewRequest(BaseModel): plan:str=Field(...,pattern='^(monthly|quarterly|yearly)$')

def normalize_domain(raw):
    return raw.strip().lower().removeprefix('https://').removeprefix('http://').removeprefix('www.').split('/')[0]

def admin(request:Request): auth.require_admin(request)

@app.get('/health')
def health(): return {'status':'ok'}
@app.post('/api/admin/login')
def admin_login(req:AdminLoginRequest):
    if not settings.admin_api_key or not auth.hmac.compare_digest(req.api_key,settings.admin_api_key): raise HTTPException(status_code=401,detail={'success':False,'message':'Invalid admin credentials'})
    return {'success':True,'token':auth.issue_admin_session(),'expiresIn':3600}

@app.post('/api/widget/validate')
def widget_validate(req:WidgetValidateRequest,request:Request):
    entry=widget_cache.validate(request,req.chatbot_id,req.current_domain)
    return {'success':True,'chatbotId':entry['chatbot_id'],'status':'active'}

@app.post('/api/chat/init')
def chat_init(req:InitRequest,request:Request):
    origin=urlparse(request.headers.get('origin','')).hostname or ''
    entry=widget_cache.validate(request,req.chatbot_id,origin)
    return {'success':True,'sessionToken':auth.issue_session(entry['chatbot_id']),'expiresIn':settings.widget_session_ttl_seconds}
def authorized_bot(request:Request):
    claims=auth.session_claims(request)
    origin=urlparse(request.headers.get('origin','')).hostname or ''
    entry=widget_cache.validate(request,claims.get('sub'),origin)
    return {'id':entry['chatbot_id'],'allowed_domain':origin}
@app.post('/api/chat')
@limiter.limit(settings.chat_rate_limit)
def chat(request:Request,req:ChatRequest,bot=Depends(authorized_bot)):
    kb=db.knowledge(bot['id'])
    if not kb or kb['knowledge_status']!='ready' or not vectorstore.is_indexed(bot['id']):return {'answer':"This site's knowledge base is not ready yet. Please try again shortly.",'sources':[],'status':'indexing'}
    result=rag.answer(bot['id'],req.message); return {'answer':result['answer'],'sources':result['sources'],'status':'ready'}
@app.post('/api/chat/stream')
@limiter.limit(settings.chat_rate_limit)
def chat_stream(request:Request,req:ChatRequest,bot=Depends(authorized_bot)):
    def events():
        for event in rag.stream_answer(bot['id'],req.message): yield f"data: {json.dumps(event)}\n\n"
        yield 'data: [DONE]\n\n'
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
@app.get('/api/status')
def status(request:Request,bot=Depends(authorized_bot)):
    snap=indexer.get_progress(bot['id']); kb=db.knowledge(bot['id']); return {'chatbot_id':bot['id'],'phase':kb['knowledge_status'] if kb else snap['phase'],'indexing':snap['indexing'],'indexed':bool(kb and kb['knowledge_status']=='ready'),'pages_done':snap['pages_done'],'pages_known':snap['pages_known'],'chunks':snap['chunks'],'last_url':snap['last_url'],'knowledge_version':kb['knowledge_base_version'] if kb else 0,'crawl_metrics':snap.get('crawl_metrics',{}),'source_provider':kb['source_provider'] if kb else 'CONTEXT_DEV','crawl_status':kb['crawl_status'] if kb else 'PENDING','website_name':kb['website_name'] if kb else '','estimated_knowledge_size':kb['estimated_knowledge_size'] if kb else 'unknown','categories':json.loads(kb['categories'] or '[]') if kb else [],'pages_found':kb['pages_found'] if kb else 0,'last_analyzed_at':kb['last_analyzed_at'] if kb else None,'total_pages':kb['pages_crawled'] if kb else 0,'total_chunks':kb['total_chunks'] if kb else 0,'total_tokens':kb['total_tokens'] if kb else 0,'error_details':kb['error_details'] if kb else ''}

@app.get('/admin')
def admin_page(): return FileResponse(STATIC_DIR/'admin.html',media_type='text/html')
@app.get('/api/admin/website-preview',dependencies=[Depends(admin)])
def website_preview(url:str=Query(...,min_length=1,max_length=1000)):
    try:
        result=ContextDevExtractor().analyze_website(url)
        return {'website_url':url,'website_name':urlparse(url if '://' in url else 'https://'+url).hostname or url,'pages_found':len(result.pages),'categories':result.categories,'estimated_knowledge_size':result.estimated_knowledge_size,'analyzed_at':result.analyzed_at,'status':'AWAITING_APPROVAL','pages':[{'url':p.url,'title':p.title} for p in result.pages[:20]],'provider':result.provider}
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception as exc:
        logger.warning('Website preview failed for %s: %s',url,exc)
        raise HTTPException(502,'Unable to discover website pages through Context.dev')

@app.post('/api/admin/chatbots/{chatbot_id}/analyze',dependencies=[Depends(admin)])
def analyze_chatbot(chatbot_id:str):
    bot=db.get_chatbot(chatbot_id)
    if not bot: raise HTTPException(404,'Chatbot not found')
    started=db.now(); db.update_knowledge(chatbot_id,knowledge_status='pending',crawl_status='ANALYZING',crawl_started_at=started,error_details='')
    try:
        result=ContextDevExtractor().analyze_website(bot['website_url'] or bot['allowed_domain'])
        db.update_knowledge(chatbot_id,website_name=urlparse(bot['website_url'] or bot['allowed_domain']).hostname or bot['allowed_domain'],estimated_knowledge_size=result.estimated_knowledge_size,categories=json.dumps(result.categories),pages_found=len(result.pages),discovered_urls=json.dumps([{'url':p.url,'title':p.title} for p in result.pages]),last_analyzed_at=result.analyzed_at,crawl_status='AWAITING_APPROVAL',source_provider=result.provider)
        return {'chatbot_id':chatbot_id,'status':'AWAITING_APPROVAL','pages_found':len(result.pages),'categories':result.categories,'estimated_knowledge_size':result.estimated_knowledge_size,'pages':[{'url':p.url,'title':p.title} for p in result.pages[:20]]}
    except Exception as exc:
        db.update_knowledge(chatbot_id,knowledge_status='failed',crawl_status='FAILED',crawl_completed_at=db.now(),error_details=str(exc)[:1000])
        logger.exception('Website analysis failed for %s',chatbot_id)
        raise HTTPException(502,'Website analysis failed')

@app.post('/api/admin/chatbots',dependencies=[Depends(admin)])
def create_chatbot(req:ChatbotRequest, crawl:bool=False):
    if req.subscription_end_date<req.subscription_start_date:raise HTTPException(400,'Invalid subscription dates')
    data=req.model_dump(); data['allowed_domain']=normalize_domain(data['allowed_domain']); data['website_url']=data['website_url'].strip(); cid=db.create_chatbot(data)
    if crawl:indexer.ensure_indexed(cid,data['website_url'] or data['allowed_domain'],force=True)
    return {'chatbot_id':cid,'status':'started' if crawl else 'pending','install_code':f'<script src="{settings.public_base_url or ""}/chatbot.js" data-chatbot-id="{cid}" defer></script>'}
@app.get('/api/admin/chatbots',dependencies=[Depends(admin)])
def list_chatbots(search:str='',status:str='',plan:str='',knowledge_status:str='',page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),sort:str='created_at',direction:str='desc'):
    result=db.list_chatbots(search,status,plan,knowledge_status,page_size,(page-1)*page_size,sort,direction); return {'items':result['items'],'total':result['total'],'page':page,'page_size':page_size,'pages':(result['total']+page_size-1)//page_size}
@app.get('/api/admin/chatbots/{chatbot_id}',dependencies=[Depends(admin)])
def get_admin_chatbot(chatbot_id:str):
    bot=db.get_chatbot(chatbot_id)
    if not bot:raise HTTPException(404,'Chatbot not found')
    data=dict(bot); knowledge=db.knowledge(chatbot_id)
    if knowledge:
        data.update({key:knowledge[key] for key in ('knowledge_status','crawl_status','pages_found','pages_crawled','total_chunks','total_tokens','estimated_knowledge_size','last_analyzed_at','last_crawled_at','source_provider','embedding_provider','error_details') if key in knowledge})
    data['install_code']=f'<script src="{settings.public_base_url or ""}/chatbot.js" data-chatbot-id="{chatbot_id}" defer></script>'; return data
@app.get('/api/admin/chatbots/{chatbot_id}/crawled-urls',dependencies=[Depends(admin)])
def crawled_urls(chatbot_id:str,limit:int=Query(1000,ge=1,le=5000),offset:int=Query(0,ge=0)):
    if not db.get_chatbot(chatbot_id): raise HTTPException(404,'Chatbot not found')
    return {'chatbot_id':chatbot_id,'items':db.crawled_urls(chatbot_id,limit,offset)}
@app.patch('/api/admin/chatbots/{chatbot_id}',dependencies=[Depends(admin)])
def update_admin_chatbot(chatbot_id:str,req:ChatbotUpdate):
    if not db.get_chatbot(chatbot_id):raise HTTPException(404,'Chatbot not found')
    fields=req.model_dump(exclude_unset=True)
    if 'allowed_domain' in fields:fields['allowed_domain']=normalize_domain(fields['allowed_domain'])
    db.update_chatbot(chatbot_id,fields); widget_cache.invalidate(chatbot_id); return get_admin_chatbot(chatbot_id)
@app.post('/api/admin/chatbots/{chatbot_id}/status',dependencies=[Depends(admin)])
def change_status(chatbot_id:str,status:str=Query(...,pattern='^(active|suspended|expired)$')):
    if not db.get_chatbot(chatbot_id):raise HTTPException(404,'Chatbot not found')
    db.set_chatbot_status(chatbot_id,status); widget_cache.invalidate(chatbot_id); return {'chatbot_id':chatbot_id,'status':status}
@app.post('/api/admin/chatbots/{chatbot_id}/recrawl',dependencies=[Depends(admin)])
def recrawl(chatbot_id:str):
    bot=db.get_chatbot(chatbot_id)
    if not bot:raise HTTPException(404,'Chatbot not found')
    widget_cache.invalidate(chatbot_id)
    return {'chatbot_id':chatbot_id,'status':indexer.ensure_indexed(chatbot_id,bot['website_url'] or bot['allowed_domain'],force=True)}
@app.post('/api/admin/chatbots/{chatbot_id}/renew',dependencies=[Depends(admin)])
def renew(chatbot_id:str,req:RenewRequest):
    bot=db.get_chatbot(chatbot_id)
    if not bot:raise HTTPException(404,'Chatbot not found')
    old=datetime.fromisoformat(bot['subscription_end_date']); old=old if old.tzinfo else old.replace(tzinfo=timezone.utc); start=max(datetime.now(timezone.utc),old); end=start+timedelta(days={'monthly':30,'quarterly':90,'yearly':365}[req.plan]); db.renew_chatbot(chatbot_id,start.isoformat(),end.isoformat(),req.plan); widget_cache.invalidate(chatbot_id); return {'status':'active','subscription_end_date':end.isoformat()}
@app.get('/api/admin/metrics',dependencies=[Depends(admin)])
def metrics():return db.metrics()
@app.get('/api/admin/security-logs',dependencies=[Depends(admin)])
def logs(chatbot_id:str='',event_type:str='',limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):return {'items':db.security_logs(chatbot_id,event_type,limit,offset)}
@app.get('/api/admin/export.csv',dependencies=[Depends(admin)])
def export_csv():
    result=db.list_chatbots(limit=100000); output=io.StringIO(); fields=list(result['items'][0].keys()) if result['items'] else ['id','chatbot_name','allowed_domain','status']; writer=csv.DictWriter(output,fieldnames=fields); writer.writeheader(); writer.writerows([dict(row) for row in result['items']]); return StreamingResponse(iter([output.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=chatbots.csv'})
@app.get('/chatbot.js')
def chatbot_js():return FileResponse(STATIC_DIR/'widget.js',media_type='application/javascript',headers={'Cache-Control':'public,max-age=300'})
@app.get('/widget.js')
def widget_js():return FileResponse(STATIC_DIR/'widget.js',media_type='application/javascript',headers={'Cache-Control':'public,max-age=300'})
@app.get('/demo')
def demo():return FileResponse(STATIC_DIR/'demo.html',media_type='text/html')
@app.get('/')
def root():return FileResponse(STATIC_DIR/'index.html',media_type='text/html')
@app.get('/api')
def api_info():return {'service':'Secure SaaS RAG Chatbot','admin_portal':'/admin','endpoints':['/api/admin/login','/api/admin/chatbots','/api/admin/metrics','/api/admin/security-logs','/api/chat/stream','/api/chat/init','/api/chat','/api/status']}

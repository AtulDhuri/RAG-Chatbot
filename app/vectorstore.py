"""Business-facing vector operations delegated to the configured VectorStore provider."""
from __future__ import annotations
import hashlib
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from .config import get_settings
from .vector_store import get_vector_store
from . import db

settings=get_settings(); client=OpenAI(api_key=settings.openai_api_key)

@retry(stop=stop_after_attempt(4),wait=wait_exponential(multiplier=1,max=20))
def _embed(texts):
    response=client.embeddings.create(model=settings.openai_embedding_model,input=texts)
    return [item.embedding for item in response.data]

def _embed_batched(texts,batch_size=None):
    batch_size = batch_size or max(1, settings.embedding_batch_size)
    result=[]
    for start in range(0,len(texts),batch_size): result.extend(_embed(texts[start:start+batch_size]))
    return result

def _documents(records,chunk_fn,changed_ids=None):
    docs=[]
    for record in records:
        for index,chunk in enumerate(chunk_fn(record['text'])):
            doc_id=hashlib.sha256(f"{record['url']}#{index}".encode()).hexdigest()
            if changed_ids is not None and doc_id not in changed_ids: embedding=None
            else: embedding=[]
            docs.append({'id':doc_id,'source_url':record['url'],'title':record.get('title',''),'content':chunk,'content_hash':hashlib.sha256(chunk.encode()).hexdigest(),'embedding':embedding,'created_at':db.now(),'updated_at':db.now()})
    return docs

def count(chatbot_id): return get_vector_store().get_document_count(chatbot_id)
def is_indexed(chatbot_id): return count(chatbot_id)>0

def index_documents(chatbot_id,records,*,chunk_fn,version):
    store=get_vector_store(); previous_version=max(version-1,0); old_hashes=store.get_document_hashes(chatbot_id,previous_version)
    docs=_documents(records,chunk_fn,None)
    changed_ids={d['id'] for d in docs if old_hashes.get(d['id']) != d['content_hash']}
    for doc in docs:
        if doc['id'] not in changed_ids: doc['embedding']=None
    changed=[d for d in docs if d['embedding'] is not None]
    if changed:
        embeddings=_embed_batched([d['content'] for d in changed])
        for doc,embedding in zip(changed,embeddings): doc['embedding']=embedding
    store.sync_documents(chatbot_id,docs,version,previous_version)
    return len(docs)

def _retrieval_query(question: str) -> str:
    """Add useful retrieval context for short, ambiguous commercial questions."""
    normalized = question.lower()
    pricing_terms = ("price", "pricing", "cost", "plan", "plans", "fee", "fees", "subscription")
    if any(term in normalized for term in pricing_terms):
        return f"{question}\nPricing plans, prices, costs, setup fees, subscriptions, and billing options"
    return question


def query(chatbot_id,question,*,top_k):
    retrieval_query = _retrieval_query(question)
    return get_vector_store().similarity_search(chatbot_id,_embed([retrieval_query])[0],top_k,query_text=question)

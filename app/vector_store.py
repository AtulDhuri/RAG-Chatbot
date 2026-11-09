"""Provider-neutral vector store contract and the PostgreSQL pgvector provider."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any

class VectorStore(ABC):
    @abstractmethod
    def add_documents(self, chatbot_id: str, documents: list[dict], version: int) -> None: ...
    @abstractmethod
    def update_documents(self, chatbot_id: str, documents: list[dict], version: int) -> None: ...
    @abstractmethod
    def delete_documents(self, chatbot_id: str, document_ids: list[str], version: int) -> None: ...
    @abstractmethod
    def similarity_search(self, chatbot_id: str, embedding: list[float], top_k: int, query_text: str = "") -> list[dict]: ...
    @abstractmethod
    def delete_chatbot_documents(self, chatbot_id: str) -> None: ...
    @abstractmethod
    def get_document_count(self, chatbot_id: str) -> int: ...

    def get_document_hashes(self, chatbot_id: str, version: int) -> dict[str, str]:
        raise NotImplementedError

    def sync_documents(self, chatbot_id: str, documents: list[dict], version: int, previous_version: int) -> None:
        """Provider hook for copying unchanged vectors and writing changed vectors."""
        self.add_documents(chatbot_id, documents, version)


class PgVectorStore(VectorStore):
    def _insert(self, conn, chatbot_id, documents, version):
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO chatbot_documents
                (id, chatbot_id, source_url, title, content, embedding, content_hash, knowledge_version, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s)
                ON CONFLICT (id, knowledge_version) DO UPDATE SET content=EXCLUDED.content,
                embedding=EXCLUDED.embedding, content_hash=EXCLUDED.content_hash, updated_at=EXCLUDED.updated_at""",
                [(d["id"], chatbot_id, d["source_url"], d.get("title", ""), d["content"], str(d["embedding"]), d["content_hash"], version, d["created_at"], d["updated_at"]) for d in documents],
            )

    def add_documents(self, chatbot_id, documents, version):
        from . import db
        with db.connection() as conn:
            with conn.transaction(): self._insert(conn, chatbot_id, documents, version)

    def update_documents(self, chatbot_id, documents, version):
        self.add_documents(chatbot_id, documents, version)

    def delete_documents(self, chatbot_id, document_ids, version):
        from . import db
        if not document_ids: return
        with db.connection() as conn:
            with conn.transaction(): conn.execute("DELETE FROM chatbot_documents WHERE chatbot_id=%s AND knowledge_version=%s AND id=ANY(%s)", (chatbot_id, version, document_ids))

    def delete_chatbot_documents(self, chatbot_id):
        from . import db
        with db.connection() as conn:
            with conn.transaction(): conn.execute("DELETE FROM chatbot_documents WHERE chatbot_id=%s", (chatbot_id,))

    def get_document_count(self, chatbot_id):
        from . import db
        with db.connection() as conn: return conn.execute("SELECT count(*) AS count FROM chatbot_documents d JOIN knowledge_bases k ON k.chatbot_id=d.chatbot_id AND k.knowledge_base_version=d.knowledge_version WHERE d.chatbot_id=%s", (chatbot_id,)).fetchone()["count"]

    def get_document_hashes(self, chatbot_id, version):
        from . import db
        with db.connection() as conn: return {row["id"]: row["content_hash"] for row in conn.execute("SELECT id,content_hash FROM chatbot_documents WHERE chatbot_id=%s AND knowledge_version=%s", (chatbot_id, version)).fetchall()}

    def sync_documents(self, chatbot_id, documents, version, previous_version):
        from . import db
        stamp=db.now()
        with db.connection() as conn:
            with conn.transaction():
                for doc in documents:
                    if doc.get("embedding") is None:
                        conn.execute("""INSERT INTO chatbot_documents (id,chatbot_id,source_url,title,content,embedding,content_hash,knowledge_version,created_at,updated_at)
                            SELECT id,chatbot_id,source_url,title,%s,embedding,%s,%s,created_at,%s FROM chatbot_documents
                            WHERE chatbot_id=%s AND knowledge_version=%s AND id=%s
                            ON CONFLICT (id, knowledge_version) DO UPDATE SET content=EXCLUDED.content, content_hash=EXCLUDED.content_hash, updated_at=EXCLUDED.updated_at""", (doc["content"],doc["content_hash"],version,stamp,chatbot_id,previous_version,doc["id"]))
                    else:
                        self._insert(conn,chatbot_id,[dict(doc,created_at=stamp,updated_at=stamp)],version)

    def publish_version(self, chatbot_id, version, previous_version):
        from . import db
        with db.connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM chatbot_documents WHERE chatbot_id=%s AND knowledge_version NOT IN (%s,%s)",(chatbot_id,version,previous_version))
                conn.execute("DELETE FROM chatbot_documents WHERE chatbot_id=%s AND knowledge_version=%s",(chatbot_id,previous_version))

    def similarity_search(self, chatbot_id, embedding, top_k, query_text=""):
        from . import db
        vector=str(embedding)
        # Combine vector similarity with PostgreSQL lexical relevance. The
        # lexical component is important for short queries such as "pricing",
        # where an embedding alone can rank generic pages above an exact match.
        lexical_query = query_text.strip() or "__no_matching_query__"
        with db.connection() as conn:
            rows=conn.execute("""SELECT d.content,d.source_url,d.title,
                    1-(d.embedding <=> %s::vector) AS vector_score,
                    ts_rank_cd(
                        to_tsvector('simple', coalesce(d.title,'') || ' ' || coalesce(d.source_url,'') || ' ' || d.content),
                        websearch_to_tsquery('simple', %s)
                    ) AS lexical_score
                FROM chatbot_documents d JOIN knowledge_bases k ON k.chatbot_id=d.chatbot_id AND k.knowledge_base_version=d.knowledge_version
                WHERE d.chatbot_id=%s
                ORDER BY (1-(d.embedding <=> %s::vector)) + (0.35 * ts_rank_cd(
                    to_tsvector('simple', coalesce(d.title,'') || ' ' || coalesce(d.source_url,'') || ' ' || d.content),
                    websearch_to_tsquery('simple', %s)
                )) DESC
                LIMIT %s""",(vector,lexical_query,chatbot_id,vector,lexical_query,top_k)).fetchall()
        return [{"text":r["content"],"url":r["source_url"],"title":r["title"],"score":float(r["vector_score"]),"lexical_score":float(r["lexical_score"])} for r in rows]


def get_vector_store() -> VectorStore:
    from .config import get_settings
    provider=get_settings().vector_provider.lower()
    if provider=="pgvector": return PgVectorStore()
    raise RuntimeError(f"Unsupported VECTOR_PROVIDER: {provider}. Implement a VectorStore provider first.")

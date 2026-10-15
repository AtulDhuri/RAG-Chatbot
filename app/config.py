"""Environment-based settings for the RAG chatbot backend."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM / Embeddings provider (OpenAI) ----
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    # ---- Context.dev (web data provider for site scanning) ----
    # Server-side secret. The SDK also reads CONTEXT_DEV_API_KEY from the
    # environment automatically; we surface it here so the rest of the app can
    # detect whether the hosted crawler is available.
    context_dev_api_key: str = ""
    # Which crawl backend to use: "context_dev" (hosted) or "playwright" (local).
    # "auto" uses Context.dev when a key is present and falls back to Playwright.
    crawl_backend: str = "auto"

    # ---- Crawler ----
    crawl_max_pages: int = 100
    crawl_ttl_seconds: int = 86400
    crawl_respect_robots: bool = True
    crawl_timeout: int = 15

    # ---- Vector store ----
    # Which backend persists chunks + embeddings:
    #   "pgvector" -> Postgres with the pgvector extension (Supabase); durable,
    #                 multi-tenant, survives redeploys and multiple app replicas.
    #   "chroma"   -> local on-disk ChromaDB; fine for local dev, ephemeral in
    #                 containers.
    # "auto" uses pgvector when DATABASE_URL is set, else falls back to chroma.
    vector_backend: str = "auto"
    # Postgres connection string for the pgvector backend. Supabase provides this
    # (use the connection pooler URL for serverless/edge deploys). Server-side
    # secret: never ship it to the browser.
    database_url: str = ""
    # Embedding dimension; must match the embedding model (text-embedding-3-small
    # = 1536). The pgvector column is sized to this.
    embedding_dim: int = 1536
    chroma_persist_dir: str = "./data/chroma"

    # ---- Retrieval / chunking ----
    retrieval_top_k: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 150

    # ---- Server ----
    allowed_origins: str = "*"
    chat_rate_limit: str = "30/minute"

    @property
    def origins_list(self) -> list[str]:
        if self.allowed_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

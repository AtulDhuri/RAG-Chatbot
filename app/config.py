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
    chat_max_tokens: int = 400
    openai_embedding_model: str = "text-embedding-3-small"

    # ---- Context.dev ----
    context_dev_api_key: str = ""
    crawl_backend: str = "auto"

    # ---- Vector store ----
    vector_backend: str = "auto"
    database_url: str = ""
    embedding_dim: int = 1536
    chroma_persist_dir: str = "./data/chroma"

    # ---- Retrieval / chunking ----
    retrieval_top_k: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 150
    crawl_max_pages: int = 100
    crawl_timeout: int = 30
    crawl_respect_robots: bool = True
    crawl_ttl_seconds: int = 86400

    # ---- Response cache / distributed controls ----
    redis_url: str = ""
    cache_ttl_seconds: int = 86400
    unauthorized_threshold: int = 50
    unauthorized_window_seconds: int = 900
    unauthorized_block_seconds: int = 3600
    rate_limit_per_minute: int = 60

    # ---- Subscription / authentication ----
    session_ttl_seconds: int = 2592000
    starter_chat_limit: int = 500
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_plan_starter: str = ""
    public_app_url: str = "http://localhost:8000"
    admin_registration_code: str = ""
    # ---- Server ----
    allowed_origins: str = "*"
    chat_rate_limit: str = "30/minute"

    @property
    def origins_list(self) -> list[str]:
        configured = self.allowed_origins.strip()
        # The embeddable widget is intentionally cross-origin. It authenticates
        # with a per-website installation token and never uses browser cookies,
        # so wildcard CORS is safe for the widget API and avoids requiring the
        # operator to update Render whenever a tenant adds a website.
        if configured == "*":
            return ["*"]
        return [o.strip() for o in configured.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

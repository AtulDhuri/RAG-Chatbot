"""Environment-based settings for the multi-tenant RAG chatbot."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    crawl_max_pages: int = 100
    crawl_ttl_seconds: int = 86400
    crawl_respect_robots: bool = True
    crawl_timeout: int = 60
    render_javascript: bool = True
    network_idle_timeout: int = 30
    post_render_delay: int = 2
    crawl_cache_ttl_seconds: int = 86400
    context_dev_api_key: str = ""
    context_dev_base_url: str = "https://api.context.dev/v1"
    context_dev_max_pages: int = 100
    context_dev_analysis_max_links: int = 50
    context_dev_analysis_url_regex: str = ""
    context_dev_timeout: int = 300
    database_url: str
    embedding_dim: int = 1536
    vector_provider: str = "pgvector"
    public_base_url: str = ""
    retrieval_top_k: int = 5
    chunk_size: int = 800
    chunk_overlap: int = 150
    allowed_origins: str = ""
    chat_rate_limit: str = "30/minute"
    widget_session_ttl_seconds: int = 600
    jwt_secret: str = ""
    jwt_issuer: str = "rag-chatbot"
    jwt_audience: str = "rag-widget"
    admin_api_key: str = ""
    widget_validation_cache_ttl_seconds: int = 300

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

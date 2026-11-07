"""Environment-based settings for the multi-tenant RAG chatbot."""
from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    # 0 means crawl until the domain frontier is exhausted.
    crawl_backend: str = "auto"
    crawl_max_pages: int = 0
    crawl_ttl_seconds: int = 86400
    crawl_respect_robots: bool = True
    crawl_timeout: int = 60
    render_javascript: bool = True
    network_idle_timeout: int = 30
    post_render_delay: int = 2
    crawl_cache_ttl_seconds: int = 86400
    context_dev_api_key: str = ""
    context_dev_base_url: str = "https://api.context.dev/v1"
    context_dev_max_pages: int = 0
    context_dev_analysis_max_links: int = 0
    context_dev_analysis_url_regex: str = ""
    context_dev_timeout: int = 300
    database_url: str
    embedding_dim: int = 1536
    vector_provider: str = "pgvector"
    public_base_url: str = ""
    embedding_batch_size: int = 64
    ingestion_page_batch_size: int = 32
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
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = Field(default="", validation_alias=AliasChoices("SMTP_USERNAME", "SMTP_USER", "EMAIL_USER"))
    smtp_password: str = Field(default="", validation_alias=AliasChoices("SMTP_PASSWORD", "SMTP_PASS", "EMAIL_PASS"))
    smtp_from: str = ""
    smtp_tls: bool = True
    lead_notification_email: str = "dineossite@gmail.com"
    widget_validation_cache_ttl_seconds: int = 300

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

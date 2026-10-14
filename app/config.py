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

    # ---- Crawler ----
    crawl_max_pages: int = 100
    crawl_ttl_seconds: int = 86400
    crawl_respect_robots: bool = True
    crawl_timeout: int = 15

    # ---- Vector store ----
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

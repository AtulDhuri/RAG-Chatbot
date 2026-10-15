"""Shared OpenAI embedding helpers used by every vector-store backend.

Keeping embedding logic in one place means the Chroma and pgvector backends
produce identical vectors (same model, same batching, same retry policy), so a
tenant indexed under one backend is directly comparable under the other.
"""
from __future__ import annotations

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

_settings = get_settings()
_openai = OpenAI(api_key=_settings.openai_api_key)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with the configured OpenAI embedding model."""
    resp = _openai.embeddings.create(
        model=_settings.openai_embedding_model, input=texts
    )
    return [d.embedding for d in resp.data]


def embed_batched(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed many texts in fixed-size batches to respect request limits."""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        out.extend(embed(texts[i : i + batch_size]))
    return out

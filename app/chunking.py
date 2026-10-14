"""Split page text into overlapping, roughly word-bounded chunks."""
from __future__ import annotations


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """Split ``text`` into chunks of ~``chunk_size`` characters.

    Chunks break on whitespace so words aren't cut in half, and consecutive
    chunks share ``overlap`` characters of context.
    """
    text = text.strip()
    if not text:
        return []
    if overlap >= chunk_size:
        overlap = chunk_size // 4

    chunks: list[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + chunk_size, n)

        # Try not to cut a word in half: back up to the last whitespace.
        if end < n:
            window = text.rfind(" ", start, end)
            if window != -1 and window > start + chunk_size // 2:
                end = window

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= n:
            break
        start = max(end - overlap, start + 1)

    return chunks

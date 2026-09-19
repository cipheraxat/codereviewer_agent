from __future__ import annotations


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError(f"max_chunk_chars must be > 0, got {max_chars}")
    if overlap < 0:
        raise ValueError(f"chunk_overlap must be >= 0, got {overlap}")
    if overlap >= max_chars:
        raise ValueError(f"chunk_overlap ({overlap}) must be < max_chunk_chars ({max_chars})")
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks

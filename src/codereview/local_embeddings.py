from __future__ import annotations

import hashlib
import math
import re

from codereview.embeddings import EMBEDDING_DIM

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]{3,}")


def local_embed_text(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic bag-of-words style embedding for offline demos and tests."""
    vec = [0.0] * dim
    for token in _TOKEN_PATTERN.findall(text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vec[index] += 1.0
        vec[(index + 17) % dim] += 0.5

    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0.0:
        return vec
    return [value / norm for value in vec]


class LocalEmbeddingClient:
    """Offline embedding client — no API key required."""

    def __init__(self) -> None:
        self.model = "local/hash-embedding"

    @property
    def available(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [local_embed_text(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return local_embed_text(text)

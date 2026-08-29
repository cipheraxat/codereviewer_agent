from __future__ import annotations

import math
from dataclasses import dataclass, field

from codereview.config import SupabaseConfig
from codereview.external_context import content_hash
from codereview.models import CodeSnippet


@dataclass
class _StoredChunk:
    repo: str
    path: str
    chunk_index: int
    content: str
    embedding: list[float]
    source: str


@dataclass
class InMemoryVectorStore:
    """In-process vector store for demo and integration tests."""

    config: SupabaseConfig = field(default_factory=SupabaseConfig)

    def __post_init__(self) -> None:
        self._chunks: list[_StoredChunk] = []

    @property
    def available(self) -> bool:
        return True

    def clear(self, repo: str | None = None) -> None:
        if repo is None:
            self._chunks.clear()
            return
        self._chunks = [chunk for chunk in self._chunks if chunk.repo != repo]

    def upsert_embeddings(
        self,
        repo: str,
        path: str,
        chunks: list[str],
        embeddings: list[list[float]],
        *,
        source: str = "code",
    ) -> int:
        if not chunks:
            return 0

        self._chunks = [
            chunk
            for chunk in self._chunks
            if not (chunk.repo == repo and chunk.path == path)
        ]

        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            self._chunks.append(
                _StoredChunk(
                    repo=repo,
                    path=path,
                    chunk_index=index,
                    content=chunk[: self.config.max_chunk_chars],
                    embedding=embedding,
                    source=source,
                )
            )
        return len(chunks)

    def similarity_search(self, repo: str, query_embedding: list[float], limit: int) -> list[CodeSnippet]:
        scored: list[tuple[float, _StoredChunk]] = []
        for chunk in self._chunks:
            if chunk.repo != repo:
                continue
            similarity = _cosine_similarity(query_embedding, chunk.embedding)
            if similarity >= self.config.match_threshold:
                scored.append((similarity, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        snippets: list[CodeSnippet] = []
        for similarity, chunk in scored[:limit]:
            snippets.append(
                CodeSnippet(
                    path=chunk.path,
                    content=chunk.content,
                    score=similarity * 10.0,
                    reason=f"vector_match:{chunk.source}",
                )
            )
        return snippets


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)

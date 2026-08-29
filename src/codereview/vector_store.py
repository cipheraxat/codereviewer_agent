from __future__ import annotations

import logging
from typing import Optional

import httpx

from codereview.config import SupabaseConfig
from codereview.external_context import content_hash
from codereview.models import CodeSnippet

logger = logging.getLogger(__name__)


class SupabaseVectorStore:
    """Supabase pgvector store for code chunk embeddings."""

    def __init__(self, config: SupabaseConfig, *, url: Optional[str] = None, key: Optional[str] = None) -> None:
        self.config = config
        self.url = (url or "").rstrip("/")
        self.key = key or ""

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.url and self.key)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }

    def upsert_chunks(self, repo: str, path: str, chunks: list[str]) -> None:
        if not self.available or not chunks:
            return

        rows = []
        for index, chunk in enumerate(chunks):
            digest = content_hash(chunk)
            rows.append(
                {
                    "repo": repo,
                    "path": path,
                    "chunk_index": index,
                    "content_hash": digest,
                    "content": chunk[: self.config.max_chunk_chars],
                }
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.url}/rest/v1/{self.config.table}?on_conflict=repo,path,chunk_index,content_hash",
                    headers=self._headers(),
                    json=rows,
                )
                if response.status_code not in {200, 201}:
                    logger.warning("Supabase upsert metadata failed: %s %s", response.status_code, response.text)
        except httpx.HTTPError as exc:
            logger.warning("Supabase upsert failed: %s", exc)

    def upsert_embeddings(self, repo: str, path: str, chunks: list[str], embeddings: list[list[float]]) -> None:
        if not self.available:
            return

        rows = []
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            rows.append(
                {
                    "repo": repo,
                    "path": path,
                    "chunk_index": index,
                    "content_hash": content_hash(chunk),
                    "content": chunk[: self.config.max_chunk_chars],
                    "embedding": embedding,
                }
            )

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.url}/rest/v1/{self.config.table}?on_conflict=repo,path,chunk_index,content_hash",
                    headers=self._headers(),
                    json=rows,
                )
                if response.status_code not in {200, 201}:
                    logger.warning("Supabase embedding upsert failed: %s %s", response.status_code, response.text)
        except httpx.HTTPError as exc:
            logger.warning("Supabase embedding upsert failed: %s", exc)

    def similarity_search(self, repo: str, query_embedding: list[float], limit: int) -> list[CodeSnippet]:
        if not self.available:
            return []

        payload = {
            "query_embedding": query_embedding,
            "match_repo": repo,
            "match_count": limit,
            "match_threshold": self.config.match_threshold,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.url}/rest/v1/rpc/match_code_embeddings",
                    headers=self._headers(),
                    json=payload,
                )
                if response.status_code != 200:
                    logger.warning("Supabase vector search failed: %s %s", response.status_code, response.text)
                    return []
                rows = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Supabase vector search failed: %s", exc)
            return []

        snippets: list[CodeSnippet] = []
        for row in rows:
            snippets.append(
                CodeSnippet(
                    path=row.get("path", "unknown"),
                    content=row.get("content", ""),
                    score=float(row.get("similarity", 0.0)) * 10.0,
                    reason="supabase_vector_match",
                )
            )
        return snippets

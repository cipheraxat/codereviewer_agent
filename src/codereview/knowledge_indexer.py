from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from codereview.chunking import chunk_text
from codereview.config import ReviewerConfig, Settings
from codereview.embeddings import EmbeddingClient
from codereview.external_context import ExternalContextFetcher
from codereview.models import KnowledgeDocument
from codereview.vector_store import SupabaseVectorStore

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32
# Repo code is reviewed via select/bundle + tools + BM25 — never embed it (token cost).
ALLOWED_INDEX_SOURCES = frozenset({"jira", "confluence"})


class ExternalKnowledgeSource(Protocol):
    def fetch_for_indexing(self) -> list[KnowledgeDocument]: ...


@dataclass
class IndexStats:
    repo: str
    documents: int = 0
    chunks: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    skipped_sources: list[str] = field(default_factory=list)
    deleted_paths: int = 0


class KnowledgeIndexer:
    """Batch indexer: JIRA + Confluence → vector embeddings (code is never indexed)."""

    def __init__(
        self,
        repo_root: Path,
        config: ReviewerConfig,
        settings: Settings | None = None,
        *,
        embeddings: EmbeddingClient | Any | None = None,
        vector_store: Any | None = None,
        external_fetcher: ExternalKnowledgeSource | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
        self.settings = settings or Settings()
        self.embeddings = embeddings or EmbeddingClient(self.settings)
        self.vector_store = vector_store or SupabaseVectorStore(
            config.vector.supabase,
            url=self.settings.supabase_url,
            key=self.settings.supabase_key,
        )
        self.external_fetcher = external_fetcher or ExternalContextFetcher(
            config.external_context,
            atlassian_email=self.settings.atlassian_email,
            atlassian_api_token=self.settings.atlassian_api_token,
            atlassian_domain=self.settings.atlassian_domain or config.external_context.jira.base_url,
        )

    def run(self, repo_slug: str, sources: list[str] | None = None) -> IndexStats:
        if not self._vector_ready():
            raise RuntimeError(
                "Vector indexing requires vector.enabled, supabase credentials, and LLM_API_KEY for embeddings"
            )

        requested = {s.lower() for s in (sources or self.config.vector.indexing.sources)}
        stats = IndexStats(repo=repo_slug)

        if "code" in requested:
            stats.skipped_sources.append("code")
            logger.warning(
                "Skipping source=code: repo code is reviewed via select/bundle + tools + BM25 "
                "(not embedded — avoids tokenize/embed cost). Index jira/confluence only."
            )

        selected = requested & ALLOWED_INDEX_SOURCES
        for name in sorted(requested - ALLOWED_INDEX_SOURCES - {"code"}):
            stats.skipped_sources.append(name)
            logger.warning("Skipping unknown index source: %s", name)

        documents: list[KnowledgeDocument] = []
        if selected:
            if self.config.external_context.enabled:
                external_docs = self.external_fetcher.fetch_for_indexing()
                for doc in external_docs:
                    if doc.source in selected:
                        documents.append(doc)
                for name in sorted(ALLOWED_INDEX_SOURCES - selected):
                    if name not in stats.skipped_sources:
                        stats.skipped_sources.append(name)
            else:
                for name in sorted(selected):
                    stats.skipped_sources.append(name)
                logger.info(
                    "JIRA/Confluence requested but external_context.enabled is false — nothing indexed"
                )
        elif not stats.skipped_sources:
            logger.info("No indexable sources selected (allowed: jira, confluence)")

        stats.documents = len(documents)
        chunk_cfg = self.config.vector.supabase
        keep_by_source: dict[str, set[str]] = {}

        for doc in documents:
            keep_by_source.setdefault(doc.source, set()).add(doc.path)
            # Replace prior chunks for this path so content edits don't leave orphan hashes.
            if hasattr(self.vector_store, "delete_path"):
                try:
                    self.vector_store.delete_path(repo_slug, doc.path, source=doc.source)
                except Exception as exc:
                    logger.warning("Pre-upsert path delete skipped for %s: %s", doc.path, exc)
            chunks = chunk_text(doc.content, chunk_cfg.max_chunk_chars, chunk_cfg.chunk_overlap)
            if not chunks:
                continue
            embedded = 0
            for start in range(0, len(chunks), EMBED_BATCH_SIZE):
                batch = chunks[start : start + EMBED_BATCH_SIZE]
                embeddings = self.embeddings.embed_texts(batch)
                embedded += self.vector_store.upsert_embeddings(
                    repo_slug,
                    doc.path,
                    batch,
                    embeddings,
                    source=doc.source,
                )
            stats.chunks += embedded
            stats.by_source[doc.source] = stats.by_source.get(doc.source, 0) + embedded

        # Drop stale JIRA/Confluence paths no longer present in this index run.
        if hasattr(self.vector_store, "delete_missing_paths"):
            for source, keep_paths in keep_by_source.items():
                try:
                    stats.deleted_paths += self.vector_store.delete_missing_paths(
                        repo_slug,
                        keep_paths,
                        source=source,
                    )
                except Exception as exc:
                    logger.warning("Stale path cleanup skipped for %s: %s", source, exc)

        return stats

    def _vector_ready(self) -> bool:
        return (
            self.config.vector.enabled
            and self.config.vector.supabase.enabled
            and self.vector_store.available
            and self.embeddings.available
        )

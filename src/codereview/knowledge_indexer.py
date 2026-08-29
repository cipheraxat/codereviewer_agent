from __future__ import annotations

import fnmatch
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


class ExternalKnowledgeSource(Protocol):
    def fetch_for_indexing(self) -> list[KnowledgeDocument]: ...


@dataclass
class IndexStats:
    repo: str
    documents: int = 0
    chunks: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    skipped_sources: list[str] = field(default_factory=list)


class KnowledgeIndexer:
    """Batch indexer: repo code + JIRA + Confluence → vector embeddings."""

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

        selected = {s.lower() for s in (sources or self.config.vector.indexing.sources)}
        stats = IndexStats(repo=repo_slug)

        documents: list[KnowledgeDocument] = []
        if "code" in selected:
            documents.extend(self._collect_code_documents())
        else:
            stats.skipped_sources.append("code")

        if "jira" in selected or "confluence" in selected:
            if self.config.external_context.enabled:
                external_docs = self.external_fetcher.fetch_for_indexing()
                for doc in external_docs:
                    if doc.source in selected:
                        documents.append(doc)
                if "jira" not in selected:
                    stats.skipped_sources.append("jira")
                if "confluence" not in selected:
                    stats.skipped_sources.append("confluence")
            else:
                if "jira" in selected:
                    stats.skipped_sources.append("jira")
                if "confluence" in selected:
                    stats.skipped_sources.append("confluence")
                logger.info("External sources requested but external_context.enabled is false")

        stats.documents = len(documents)
        chunk_cfg = self.config.vector.supabase

        for doc in documents:
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

        return stats

    def _vector_ready(self) -> bool:
        return (
            self.config.vector.enabled
            and self.config.vector.supabase.enabled
            and self.vector_store.available
            and self.embeddings.available
        )

    def _collect_code_documents(self) -> list[KnowledgeDocument]:
        documents: list[KnowledgeDocument] = []
        globs = self.config.vector.indexing.code_globs
        seen: set[str] = set()

        for pattern in globs:
            for path in self.repo_root.glob(pattern):
                if not path.is_file():
                    continue
                rel_path = str(path.relative_to(self.repo_root))
                if rel_path in seen or self._should_ignore(rel_path):
                    continue
                seen.add(rel_path)
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
                    continue
                if not content.strip():
                    continue
                documents.append(
                    KnowledgeDocument(
                        path=rel_path,
                        content=content,
                        source="code",
                    )
                )
        return documents

    def _should_ignore(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.config.ignore_globs)

from __future__ import annotations

import fnmatch
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from codereview.config import ReviewerConfig, Settings
from codereview.embeddings import EmbeddingClient
from codereview.external_context import ExternalContextFetcher, repo_slug
from codereview.models import CodeSnippet, PullRequestContext
from codereview.vector_store import SupabaseVectorStore

logger = logging.getLogger(__name__)

CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".cs",
    ".php",
    ".kt",
    ".swift",
    ".yaml",
    ".yml",
    ".json",
    ".md",
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())


def _should_ignore(path: str, ignore_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in ignore_globs)


def _is_code_file(path: Path) -> bool:
    return path.suffix.lower() in CODE_EXTENSIONS


def _chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
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


class ContextEngine:
    """Hybrid retrieval: BM25-lite + optional Supabase vectors + external JIRA/Confluence."""

    def __init__(
        self,
        repo_root: Path,
        config: ReviewerConfig,
        settings: Optional[Settings] = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
        self.settings = settings or Settings()
        self.embeddings = EmbeddingClient(self.settings)
        self.vector_store = SupabaseVectorStore(
            config.vector.supabase,
            url=self.settings.supabase_url,
            key=self.settings.supabase_key,
        )
        self.external_fetcher = ExternalContextFetcher(
            config.external_context,
            atlassian_email=self.settings.atlassian_email,
            atlassian_api_token=self.settings.atlassian_api_token,
            atlassian_domain=self.settings.atlassian_domain or config.external_context.jira.base_url,
        )

    def build_context(self, pr: PullRequestContext) -> list[CodeSnippet]:
        changed = [f for f in pr.changed_files if not _should_ignore(f, self.config.ignore_globs)]
        query_terms = self._build_query_terms(pr, changed)
        candidates: dict[str, CodeSnippet] = {}

        for rel_path in changed:
            abs_path = self.repo_root / rel_path
            if abs_path.exists() and abs_path.is_file():
                content = self._read_truncated(abs_path)
                candidates[rel_path] = CodeSnippet(
                    path=rel_path,
                    content=content,
                    score=10.0,
                    reason="changed_in_pr",
                )
                self._maybe_index_file(pr, rel_path, content)

        for rel_path in changed:
            for neighbor in self._neighbor_paths(rel_path):
                if neighbor in candidates:
                    continue
                abs_path = self.repo_root / neighbor
                if not abs_path.exists() or not abs_path.is_file():
                    continue
                if not _is_code_file(abs_path):
                    continue
                content = self._read_truncated(abs_path)
                score = self._bm25_score(query_terms, content)
                if score <= 0:
                    continue
                candidates[neighbor] = CodeSnippet(
                    path=neighbor,
                    content=content,
                    score=score,
                    reason="neighbor_or_keyword_match",
                )
                self._maybe_index_file(pr, neighbor, content)

        if self._vector_enabled():
            for snippet in self._vector_search(pr, query_terms):
                if snippet.path not in candidates or candidates[snippet.path].score < snippet.score:
                    candidates[snippet.path] = snippet

        for snippet in self.external_fetcher.fetch(pr):
            candidates[snippet.path] = snippet

        ranked = sorted(candidates.values(), key=lambda s: s.score, reverse=True)
        return ranked[: self.config.context.max_snippets]

    def _vector_enabled(self) -> bool:
        return (
            self.config.vector.enabled
            and self.config.vector.supabase.enabled
            and self.vector_store.available
            and self.embeddings.available
        )

    def _maybe_index_file(self, pr: PullRequestContext, rel_path: str, content: str) -> None:
        if not self._vector_enabled() or not self.config.vector.supabase.index_on_review:
            return
        try:
            chunks = _chunk_text(
                content,
                self.config.vector.supabase.max_chunk_chars,
                self.config.vector.supabase.chunk_overlap,
            )
            embeddings = self.embeddings.embed_texts(chunks)
            self.vector_store.upsert_embeddings(repo_slug(pr), rel_path, chunks, embeddings)
        except Exception as exc:
            logger.warning("Vector indexing skipped for %s: %s", rel_path, exc)

    def _vector_search(self, pr: PullRequestContext, query_terms: Counter[str]) -> list[CodeSnippet]:
        query = " ".join(term for term, _ in query_terms.most_common(40))
        if not query.strip():
            query = pr.title
        try:
            query_embedding = self.embeddings.embed_query(query)
            return self.vector_store.similarity_search(
                repo_slug(pr),
                query_embedding,
                self.config.vector.supabase.vector_top_k,
            )
        except Exception as exc:
            logger.warning("Vector search skipped: %s", exc)
            return []

    def _build_query_terms(self, pr: PullRequestContext, changed_files: list[str]) -> Counter[str]:
        terms: list[str] = []
        terms.extend(_tokenize(pr.title))
        if pr.body:
            terms.extend(_tokenize(pr.body))
        for path in changed_files:
            terms.extend(_tokenize(Path(path).stem))
            patch = pr.patches.get(path, "")
            terms.extend(_tokenize(patch))
        return Counter(terms)

    def _neighbor_paths(self, rel_path: str) -> list[str]:
        depth = self.config.context.neighbor_depth
        base = Path(rel_path)
        parent = base.parent
        neighbors: list[str] = []

        if parent != Path("."):
            parent_dir = self.repo_root / parent
            if parent_dir.exists():
                for child in parent_dir.iterdir():
                    if child.is_file() and _is_code_file(child):
                        neighbors.append(str(parent / child.name))

        for _ in range(depth):
            for path in list(neighbors):
                parent_dir = (self.repo_root / path).parent
                if not parent_dir.exists():
                    continue
                for child in parent_dir.iterdir():
                    if child.is_file() and _is_code_file(child):
                        candidate = str(child.relative_to(self.repo_root))
                        if candidate not in neighbors:
                            neighbors.append(candidate)
        return neighbors

    def _read_truncated(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8", errors="replace")
        max_chars = self.config.context.max_snippet_chars
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n... [truncated]"

    def _bm25_score(self, query_terms: Counter[str], document: str) -> float:
        if not query_terms:
            return 0.0
        doc_terms = Counter(_tokenize(document))
        if not doc_terms:
            return 0.0
        score = 0.0
        for term, qf in query_terms.items():
            tf = doc_terms.get(term, 0)
            if tf:
                score += qf * (1 + tf)
        return score

    def format_context_block(self, snippets: list[CodeSnippet]) -> str:
        blocks: list[str] = []
        for snippet in snippets:
            blocks.append(
                f"### {snippet.path} (score={snippet.score:.2f}, {snippet.reason})\n"
                f"```\n{snippet.content}\n```"
            )
        return "\n\n".join(blocks)

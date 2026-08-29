from __future__ import annotations

import fnmatch
import re
from collections import Counter
from pathlib import Path

from codereview.config import ReviewerConfig
from codereview.models import CodeSnippet, PullRequestContext


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


class ContextEngine:
    """RAG-lite retrieval over changed files and local repo neighbors."""

    def __init__(self, repo_root: Path, config: ReviewerConfig) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config

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

        ranked = sorted(candidates.values(), key=lambda s: s.score, reverse=True)
        return ranked[: self.config.context.max_snippets]

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

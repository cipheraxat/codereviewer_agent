from __future__ import annotations

import logging
import re
from pathlib import Path

from codereview.diff_utils import read_file_slice
from codereview.models import PullRequestContext
from codereview.path_utils import is_denied_path, should_ignore_path

logger = logging.getLogger(__name__)

MAX_SEARCH_HITS = 40
MAX_CONTEXT_CHARS = 6000

SYMBOL_STOPWORDS = {
    "true",
    "false",
    "none",
    "null",
    "self",
    "this",
    "return",
    "import",
    "from",
    "class",
    "def",
    "function",
    "const",
    "let",
    "var",
    "export",
    "default",
    "async",
    "await",
    "yield",
    "raise",
    "except",
    "catch",
    "throw",
    "print",
    "console",
    "log",
    "type",
    "interface",
    "public",
    "private",
    "static",
    "void",
    "string",
    "number",
    "boolean",
    "object",
    "array",
    "list",
    "dict",
    "pass",
    "else",
    "elif",
    "with",
    "as",
}


class RepoTools:
    """Read-only repo tools used to enrich review context (OCR-inspired)."""

    def __init__(self, repo_root: Path, ignore_globs: list[str] | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.ignore_globs = ignore_globs or []
        self._file_index: list[Path] | None = None

    def _iter_searchable_files(self) -> list[Path]:
        if self._file_index is not None:
            return self._file_index
        files: list[Path] = []
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(self.repo_root))
            except ValueError:
                continue
            if is_denied_path(rel) or should_ignore_path(rel, self.ignore_globs):
                continue
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".woff", ".woff2", ".pyc"}:
                continue
            files.append(path)
        self._file_index = files
        return files

    def read_file(self, rel_path: str, start_line: int = 1, end_line: int | None = None) -> str:
        if is_denied_path(rel_path):
            raise ValueError(f"Path denied by security denylist: {rel_path}")
        return read_file_slice(self.repo_root, rel_path, start_line=start_line, end_line=end_line)

    def search_code(self, query: str, *, glob: str | None = None) -> str:
        if not query.strip():
            return "No matches found"
        hits: list[str] = []
        candidates = self._iter_searchable_files()
        if glob and glob != "**/*":
            candidates = [path for path in candidates if path.match(glob)]
        for path in candidates:
            try:
                rel = str(path.relative_to(self.repo_root))
            except ValueError:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "No matches found"


def _hunk_start(patch: str) -> int | None:
    match = re.search(r"\+(\d+)", patch)
    return int(match.group(1)) if match else None


def _symbols_from_patch(patch: str) -> list[str]:
    symbols: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b", line[1:]):
            name = match.group(1)
            if name.lower() in SYMBOL_STOPWORDS:
                continue
            if name not in symbols:
                symbols.append(name)
            if len(symbols) >= 5:
                return symbols
    return symbols


def enrich_context_with_tools(
    repo_root: Path,
    pr: PullRequestContext,
    *,
    ignore_globs: list[str] | None = None,
    max_files: int = 6,
    tools: RepoTools | None = None,
) -> str:
    """Deterministically pull nearby file context and light search hits."""
    tools = tools or RepoTools(repo_root, ignore_globs=ignore_globs)
    blocks: list[str] = []

    for path in pr.changed_files[:max_files]:
        if is_denied_path(path):
            continue
        patch = pr.patches.get(path, "")
        start = _hunk_start(patch) or 1
        window_start = max(1, start - 30)
        window_end = start + 80
        try:
            content = tools.read_file(path, window_start, window_end)
        except (OSError, ValueError) as exc:
            logger.debug("tool read_file skipped for %s: %s", path, exc)
            continue
        if content:
            blocks.append(f"### tool:file_read {path} ({window_start}-{window_end})\n```\n{content}\n```")

        for symbol in _symbols_from_patch(patch)[:2]:
            hits = tools.search_code(symbol)
            if hits and hits != "No matches found":
                blocks.append(f"### tool:code_search `{symbol}`\n```\n{hits[:1500]}\n```")

    joined = "\n\n".join(blocks)
    if len(joined) <= MAX_CONTEXT_CHARS:
        return joined
    # Truncate at the last complete block boundary so code fences stay intact.
    cut = joined.rfind("\n\n### ", 0, MAX_CONTEXT_CHARS)
    if cut <= 0:
        cut = MAX_CONTEXT_CHARS
    return joined[:cut] + "\n\n... [truncated tool context]"

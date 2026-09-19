from __future__ import annotations

import re
from pathlib import Path

_SECRET_VALUE = re.compile(
    r"""(?ix)
    (
      (?:api[_-]?key|secret|password|token|passwd|credential)\s*[:=]\s*
    )
    (['\"])[^'\"]{4,}(\2)
    |
    (-----BEGIN[^-]+-----)\s*[A-Za-z0-9+/=\s]+(-----END[^-]+-----)
    """
)


def first_changed_line(patch: str) -> int | None:
    """Return the first new line number introduced in a unified diff hunk."""
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                return int(match.group(1))
    return None


def line_for_pattern(patch: str, pattern: str) -> int | None:
    """Return the line number of the first added line matching pattern, or None."""
    current_line: int | None = None
    compiled = re.compile(pattern)

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            current_line = int(match.group(1)) - 1 if match else None
            continue
        if current_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_line += 1
            if compiled.search(line[1:]):
                return current_line
        elif line.startswith(" "):
            current_line += 1

    return None


def _normalize_code_line(line: str) -> str:
    text = line
    if text.startswith(("+", "-", " ")):
        text = text[1:]
    return text.strip()


def evidence_from_match(patch: str, pattern: str, *, max_lines: int = 3) -> str | None:
    """Capture a small added-line snippet around the first regex match.

    Only added lines (+) are considered — context/removed lines never produce evidence.
    """
    current_line: int | None = None
    compiled = re.compile(pattern)
    added: list[tuple[int, str]] = []

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            current_line = int(match.group(1)) - 1 if match else None
            continue
        if current_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_line += 1
            added.append((current_line, line[1:]))
        elif line.startswith(" "):
            current_line += 1

    for index, (_lineno, content) in enumerate(added):
        if compiled.search(content):
            chunk = added[index : index + max_lines]
            return "\n".join(text for _, text in chunk)
    return None


def line_for_evidence(patch: str, evidence: str) -> int | None:
    """Locate evidence snippet in the diff using whitespace-insensitive matching.

    OCR-style anchoring: match consecutive added/context lines against the
    provided evidence rather than trusting model-invented line numbers.
    """
    if not evidence.strip():
        return None

    needle = [_normalize_code_line(part) for part in evidence.splitlines() if _normalize_code_line(part)]
    if not needle:
        return None

    current_line: int | None = None
    haystack: list[tuple[int, str]] = []

    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            current_line = int(match.group(1)) - 1 if match else None
            continue
        if current_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_line += 1
            haystack.append((current_line, _normalize_code_line(line)))
        elif line.startswith(" "):
            current_line += 1
            haystack.append((current_line, _normalize_code_line(line)))
        elif line.startswith("-") and not line.startswith("---"):
            continue

    needle_len = len(needle)
    for index in range(len(haystack) - needle_len + 1):
        window = haystack[index : index + needle_len]
        if [text for _, text in window] == needle:
            return window[0][0]
    # Single-line fallback: first needle line substring match
    first = needle[0]
    for lineno, text in haystack:
        if first and first in text:
            return lineno
    return None


def resolve_finding_line(patch: str | None, *, evidence: str | None, pattern: str | None = None) -> int | None:
    """Prefer evidence anchoring, then added-line pattern match, then first changed line."""
    if not patch:
        return None
    if evidence:
        anchored = line_for_evidence(patch, evidence)
        if anchored is not None:
            return anchored
    if pattern:
        matched = line_for_pattern(patch, pattern)
        if matched is not None:
            return matched
    return first_changed_line(patch)


def redact_evidence(evidence: str | None) -> str | None:
    """Mask secret-like string literals in evidence snippets stored on findings."""
    if not evidence:
        return evidence

    def _mask(match: re.Match[str]) -> str:
        if match.group(1):
            return f"{match.group(1)}{match.group(2)}***{match.group(3)}"
        return f"{match.group(4)} *** {match.group(5)}"

    return _SECRET_VALUE.sub(_mask, evidence)


def read_file_slice(repo_root: Path, rel_path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a 1-indexed inclusive line range from a file under repo_root."""
    path = (repo_root / rel_path).resolve()
    root = repo_root.resolve()
    if root not in path.parents and path != root:
        raise ValueError(f"Path escapes repo root: {rel_path}")
    if not path.is_file():
        raise FileNotFoundError(rel_path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    end = len(lines) if end_line is None else min(end_line, len(lines))
    if start > len(lines):
        return ""
    chunk = lines[start - 1 : end]
    numbered = [f"{index}|{text}" for index, text in enumerate(chunk, start=start)]
    return "\n".join(numbered)

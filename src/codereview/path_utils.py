from __future__ import annotations

import fnmatch
from pathlib import Path

# Always deny these paths regardless of user ignore_globs — secrets must never
# enter LLM prompts, context snippets, or the vector index.
ALWAYS_DENY_PREFIXES = (
    ".git/",
    ".git",
    ".ssh/",
    ".aws/",
    ".gnupg/",
    ".kube/",
    "node_modules/",
    ".venv/",
    ".venv311/",
    "venv/",
    "__pycache__/",
)
ALWAYS_DENY_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.staging",
    ".env.test",
    ".envrc",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
# Allowlist: common template/sample env files that are safe to review.
ENV_ALLOWLIST = {".env.example", ".env.sample", ".env.template"}
ALWAYS_DENY_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".crt",
    ".cer",
}


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_env_secret_file(name: str) -> bool:
    """True for real env secret files, not templates or unrelated .env* names."""
    if name in ENV_ALLOWLIST:
        return False
    if name in ALWAYS_DENY_NAMES:
        return True
    # Exact ".env.<variant>" only — not ".environment.py"
    if name.startswith(".env.") and name.count(".") >= 2:
        return True
    return False


def is_denied_path(rel_path: str) -> bool:
    """Hard denylist for secrets and junk — always applied."""
    normalized = normalize_path(rel_path)
    name = normalized.rsplit("/", 1)[-1]
    if _is_env_secret_file(name):
        return True
    lower = normalized.lower()
    if any(lower == prefix.rstrip("/") or lower.startswith(prefix) for prefix in ALWAYS_DENY_PREFIXES):
        return True
    suffix = Path(name).suffix.lower()
    if suffix in ALWAYS_DENY_SUFFIXES:
        return True
    return False


def should_ignore_path(path: str, ignore_globs: list[str]) -> bool:
    """Return True if path matches any ignore glob.

    Handles ``**/dir/**`` patterns that plain ``fnmatch`` misses for rooted paths
    like ``.venv311/lib/...``.
    """
    if is_denied_path(path):
        return True
    normalized = normalize_path(path)
    for pattern in ignore_globs:
        pat = normalize_path(pattern)
        if _match(normalized, pat):
            return True
    return False


def _match(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        rest = pattern[3:]
        if fnmatch.fnmatch(path, rest):
            return True
        parts = path.split("/")
        for index in range(len(parts)):
            sub = "/".join(parts[index:])
            if fnmatch.fnmatch(sub, rest) or fnmatch.fnmatch(sub, pattern):
                return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False

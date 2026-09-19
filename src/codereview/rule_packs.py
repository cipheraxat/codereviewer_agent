from __future__ import annotations

from dataclasses import dataclass

from codereview.config import CustomRule, ReviewerConfig
from codereview.models import Severity
from codereview.path_utils import normalize_path


@dataclass(frozen=True)
class RulePackRule:
    id: str
    description: str
    pattern: str
    severity: Severity
    category: str  # security | quality


# Built-in OCR-inspired packs: path-matched, language-specific checklists.
BUILTIN_PACKS: dict[str, list[RulePackRule]] = {
    "python": [
        RulePackRule(
            id="py-no-eval",
            description="Avoid eval(); it enables code injection",
            pattern=r"eval\s*\(",
            severity=Severity.HIGH,
            category="security",
        ),
        RulePackRule(
            id="py-shell-true",
            description="Avoid subprocess with shell=True",
            pattern=r"subprocess\.(call|Popen|run)\([^)]*shell\s*=\s*True",
            severity=Severity.HIGH,
            category="security",
        ),
        RulePackRule(
            id="py-bare-except",
            description="Avoid bare except clauses; catch specific exceptions",
            pattern=r"except:\s*$",
            severity=Severity.LOW,
            category="quality",
        ),
        RulePackRule(
            id="py-debug-print",
            description="Debug print left in changed code",
            pattern=r"\bprint\(",
            severity=Severity.LOW,
            category="quality",
        ),
    ],
    "typescript": [
        RulePackRule(
            id="ts-console-log",
            description="Debug logging via console.log left in changed code",
            pattern=r"console\.log\(",
            severity=Severity.LOW,
            category="quality",
        ),
        RulePackRule(
            id="ts-eval",
            description="Avoid eval() in TypeScript/JavaScript",
            pattern=r"\beval\s*\(",
            severity=Severity.HIGH,
            category="security",
        ),
    ],
    "javascript": [
        RulePackRule(
            id="js-console-log",
            description="Debug logging via console.log left in changed code",
            pattern=r"console\.log\(",
            severity=Severity.LOW,
            category="quality",
        ),
        RulePackRule(
            id="js-eval",
            description="Avoid eval() in JavaScript",
            pattern=r"\beval\s*\(",
            severity=Severity.HIGH,
            category="security",
        ),
    ],
    "yaml": [
        RulePackRule(
            id="yaml-privileged",
            description="Avoid privileged containers in Kubernetes manifests",
            pattern=r"privileged:\s*true",
            severity=Severity.HIGH,
            category="security",
        ),
        RulePackRule(
            id="yaml-plaintext-password",
            description="Possible plaintext password in YAML",
            pattern=r"(?i)password:\s*['\"]?[^'\"\s]+",
            severity=Severity.HIGH,
            category="security",
        ),
    ],
}

EXTENSION_TO_PACK: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def pack_for_path(path: str) -> str | None:
    normalized = normalize_path(path)
    if "." not in normalized.rsplit("/", 1)[-1]:
        return None
    ext = "." + normalized.rsplit(".", 1)[-1].lower()
    return EXTENSION_TO_PACK.get(ext)


def enabled_packs(config: ReviewerConfig) -> set[str]:
    if config.review.rule_packs:
        return {name.lower() for name in config.review.rule_packs}
    packs = {lang.lower() for lang in config.languages}
    # Config/manifest packs are not always listed as "languages".
    packs.add("yaml")
    return packs


def rules_for_path(path: str, config: ReviewerConfig) -> list[CustomRule]:
    """Return built-in pack rules applicable to this path (as CustomRule)."""
    pack = pack_for_path(path)
    if pack is None or pack not in enabled_packs(config):
        return []
    return [
        CustomRule(
            id=rule.id,
            description=rule.description,
            pattern=rule.pattern,
            severity=rule.severity,
            category=rule.category,
        )
        for rule in BUILTIN_PACKS.get(pack, [])
    ]


def all_applicable_rules(path: str, config: ReviewerConfig) -> list[CustomRule]:
    """Built-in pack rules + user custom_rules for a path."""
    return rules_for_path(path, config) + list(config.custom_rules)

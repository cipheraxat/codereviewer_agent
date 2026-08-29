from __future__ import annotations

import logging
import re

from codereview.config import ReviewerConfig
from codereview.diff_utils import line_for_pattern
from codereview.finding_dedupe import dedupe_findings
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, FindingCategory, PullRequestContext, Severity

logger = logging.getLogger(__name__)

PATTERN_SYSTEM = """You are a senior software engineer reviewing code quality and team conventions.
Return JSON only with shape:
{"findings":[{"category":"quality|testing|documentation|performance","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"rationale":"...","suggestion":"...","confidence":0.0-1.0}]}
Focus on maintainability, missing tests, unclear APIs, error handling, and convention violations.
Only report issues grounded in the provided diff/context."""


def build_review_prompt(pr: PullRequestContext, context_block: str, config: ReviewerConfig) -> str:
    patches = "\n\n".join(f"### {path}\n```diff\n{patch}\n```" for path, patch in pr.patches.items())
    return (
        f"PR: {pr.owner}/{pr.repo}#{pr.number} - {pr.title}\n\n"
        f"Changed files: {', '.join(pr.changed_files)}\n\n"
        f"Diffs:\n{patches}\n\n"
        f"Relevant context:\n{context_block}\n\n"
        f"Team conventions:\n{config.team_conventions}"
    )


def merge_findings(left: list[Finding], right: list[Finding]) -> list[Finding]:
    return dedupe_findings(left + right)


class PatternAgent:
    name = "pattern"

    def review(
        self,
        pr: PullRequestContext,
        context_block: str,
        config: ReviewerConfig,
        llm: LLMClient | None,
    ) -> list[Finding]:
        heuristic = self._heuristic_scan(pr, config)
        if llm is None or not llm.available:
            return heuristic

        user = build_review_prompt(pr, context_block, config)
        try:
            payload = llm.complete_json(PATTERN_SYSTEM, user)
            llm_findings = findings_from_payload(payload, self.name)
            return merge_findings(heuristic, llm_findings)
        except Exception as exc:
            logger.warning("Pattern LLM review failed, using heuristics only: %s", exc)
            return heuristic

    def _heuristic_scan(self, pr: PullRequestContext, config: ReviewerConfig) -> list[Finding]:
        findings: list[Finding] = []
        quality_patterns = [
            (r"TODO|FIXME|HACK", "Unresolved TODO/FIXME left in changed code", Severity.LOW),
            (r"console\.log\(", "Debug logging left in changed code", Severity.LOW),
            (r"print\(", "Debug print left in changed code", Severity.LOW),
        ]
        for path, patch in pr.patches.items():
            for pattern, title, severity in quality_patterns:
                if re.search(pattern, patch, re.MULTILINE):
                    findings.append(
                        Finding(
                            category=FindingCategory.QUALITY,
                            severity=severity,
                            title=title,
                            file=path,
                            line=line_for_pattern(patch, pattern),
                            rationale=f"Pattern `{pattern}` matched in PR diff.",
                            suggestion="Remove debug statements or track follow-up work in an issue.",
                            confidence=0.7,
                            agent=self.name,
                        )
                    )
        for rule in config.custom_rules:
            if rule.category == "security":
                continue
            for path, patch in pr.patches.items():
                if re.search(rule.pattern, patch, re.MULTILINE):
                    findings.append(
                        Finding(
                            category=FindingCategory.QUALITY,
                            severity=rule.severity,
                            title=rule.description,
                            file=path,
                            line=line_for_pattern(patch, rule.pattern),
                            rationale=f"Matched custom rule `{rule.id}`.",
                            suggestion="Align implementation with team conventions.",
                            confidence=0.75,
                            agent=self.name,
                            rule_id=rule.id,
                        )
                    )
        return findings

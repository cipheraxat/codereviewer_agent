from __future__ import annotations

import logging

from codereview.agents.pattern import (
    anchor_findings,
    build_review_prompt,
    iter_reviewable_patches,
    match_rules,
    merge_findings,
)
from codereview.config import ReviewerConfig
from codereview.diff_utils import evidence_from_match, redact_evidence, resolve_finding_line
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, FindingCategory, PullRequestContext, Severity

logger = logging.getLogger(__name__)

SECURITY_SYSTEM = """You are a senior application security engineer reviewing a pull request.
Return JSON only with shape:
{"findings":[{"category":"security","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"evidence_snippet":"1-3 consecutive added lines from the diff","rationale":"...","suggestion":"...","confidence":0.0-1.0}]}
Focus on authz/authn flaws, injection, secrets, unsafe deserialization, SSRF, path traversal, and insecure defaults.
Only report issues grounded in the provided diff/context. Do not invent files or lines.
Always set evidence_snippet to the exact added code lines the issue refers to (no diff +/- prefixes)."""

# Language-agnostic heuristics. Pack-level secret rules were removed to avoid duplicates.
_SECRET_PATTERNS = [
    (
        r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]+['\"]",
        "Possible hardcoded secret",
    ),
    (
        r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",
        "Private key committed to repository",
    ),
]


class SecurityAgent:
    name = "security"

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
            payload = llm.complete_json(SECURITY_SYSTEM, user)
            llm_findings = anchor_findings(findings_from_payload(payload, self.name), pr)
            return merge_findings(heuristic, llm_findings)
        except Exception as exc:
            logger.warning("Security LLM review failed, using heuristics only: %s", exc)
            return heuristic

    def _heuristic_scan(self, pr: PullRequestContext, config: ReviewerConfig) -> list[Finding]:
        findings: list[Finding] = []
        for path, patch in iter_reviewable_patches(pr, config):
            for pattern, title in _SECRET_PATTERNS:
                evidence = evidence_from_match(patch, pattern)
                if evidence is None:
                    continue
                findings.append(
                    Finding(
                        category=FindingCategory.SECURITY,
                        severity=Severity.HIGH,
                        title=title,
                        file=path,
                        line=resolve_finding_line(patch, evidence=evidence, pattern=pattern),
                        rationale=f"Pattern `{pattern}` matched in PR diff.",
                        suggestion="Remove secrets from code and use environment variables or a secret manager.",
                        confidence=0.85,
                        agent=self.name,
                        evidence_snippet=redact_evidence(evidence),
                    )
                )
        findings.extend(match_rules(pr, config, agent=self.name, category_filter="security"))
        return findings

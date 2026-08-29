from __future__ import annotations

import logging
import re

from codereview.agents.pattern import build_review_prompt, merge_findings
from codereview.config import ReviewerConfig
from codereview.diff_utils import line_for_pattern
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, FindingCategory, PullRequestContext, Severity

logger = logging.getLogger(__name__)

SECURITY_SYSTEM = """You are a senior application security engineer reviewing a pull request.
Return JSON only with shape:
{"findings":[{"category":"security","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"rationale":"...","suggestion":"...","confidence":0.0-1.0}]}
Focus on authz/authn flaws, injection, secrets, unsafe deserialization, SSRF, path traversal, and insecure defaults.
Only report issues grounded in the provided diff/context. Do not invent files or lines."""


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
            llm_findings = findings_from_payload(payload, self.name)
            return merge_findings(heuristic, llm_findings)
        except Exception as exc:
            logger.warning("Security LLM review failed, using heuristics only: %s", exc)
            return heuristic

    def _heuristic_scan(self, pr: PullRequestContext, config: ReviewerConfig) -> list[Finding]:
        findings: list[Finding] = []
        secret_patterns = [
            (r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]+['\"]", "Possible hardcoded secret"),
            (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "Private key committed to repository"),
            (r"eval\s*\(", "Use of eval() can enable code injection"),
            (r"subprocess\.(call|Popen|run)\([^)]*shell\s*=\s*True", "Shell=True subprocess invocation"),
        ]
        for path, patch in pr.patches.items():
            for pattern, title in secret_patterns:
                if re.search(pattern, patch, re.MULTILINE):
                    findings.append(
                        Finding(
                            category=FindingCategory.SECURITY,
                            severity=Severity.HIGH,
                            title=title,
                            file=path,
                            line=line_for_pattern(patch, pattern),
                            rationale=f"Pattern `{pattern}` matched in PR diff.",
                            suggestion="Remove secrets from code and use environment variables or a secret manager.",
                            confidence=0.85,
                            agent=self.name,
                        )
                    )
        findings.extend(self._rule_matches(pr, config))
        return findings

    def _rule_matches(self, pr: PullRequestContext, config: ReviewerConfig) -> list[Finding]:
        findings: list[Finding] = []
        for rule in config.custom_rules:
            if rule.category != "security":
                continue
            for path, patch in pr.patches.items():
                if re.search(rule.pattern, patch, re.MULTILINE):
                    findings.append(
                        Finding(
                            category=FindingCategory.SECURITY,
                            severity=rule.severity,
                            title=rule.description,
                            file=path,
                            line=line_for_pattern(patch, rule.pattern),
                            rationale=f"Matched custom rule `{rule.id}`.",
                            suggestion="Refactor to satisfy team rule.",
                            confidence=0.8,
                            agent=self.name,
                            rule_id=rule.id,
                        )
                    )
        return findings

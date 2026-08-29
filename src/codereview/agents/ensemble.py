from __future__ import annotations

from typing import Optional

from codereview.config import ReviewerConfig
from codereview.llm import LLMClient
from codereview.models import Finding, PullRequestContext, SEVERITY_ORDER, Severity


class EnsembleAgent:
    name = "ensemble"

    def aggregate(
        self,
        findings: list[Finding],
        config: ReviewerConfig,
        pr: PullRequestContext,
        llm: LLMClient | None = None,
    ) -> tuple[list[Finding], str, float, str]:
        deduped = self._dedupe(findings)
        filtered = [
            f
            for f in deduped
            if f.confidence >= config.posting.min_confidence
            and self._meets_threshold(f.severity, config.severity_threshold)
        ]
        filtered.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.confidence), reverse=True)

        overall_confidence = self._overall_confidence(filtered)
        summary = self._build_summary(filtered, pr)
        verdict = self._verdict(filtered)
        return filtered, summary, overall_confidence, verdict

    def _dedupe(self, findings: list[Finding]) -> list[Finding]:
        best: dict[tuple[str, Optional[str], Optional[int]], Finding] = {}
        for finding in findings:
            key = finding.dedupe_key()
            current = best.get(key)
            if current is None or finding.confidence > current.confidence:
                best[key] = finding
        return list(best.values())

    def _meets_threshold(self, severity: Severity, threshold: Severity) -> bool:
        order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
        return order[severity] >= order[threshold]

    def _overall_confidence(self, findings: list[Finding]) -> float:
        if not findings:
            return 0.9
        return round(sum(f.confidence for f in findings) / len(findings), 3)

    def _build_summary(self, findings: list[Finding], pr: PullRequestContext) -> str:
        if not findings:
            return (
                f"Automated review for `{pr.owner}/{pr.repo}#{pr.number}` found no issues above the "
                f"configured threshold."
            )
        by_severity: dict[str, int] = {}
        for finding in findings:
            by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
        parts = [f"{count} {severity}" for severity, count in sorted(by_severity.items())]
        return (
            f"Automated multi-agent review for `{pr.owner}/{pr.repo}#{pr.number}` found "
            f"{len(findings)} issue(s): {', '.join(parts)}."
        )

    def _verdict(self, findings: list[Finding]) -> str:
        if any(f.severity in {Severity.CRITICAL, Severity.HIGH} for f in findings):
            return "request_changes"
        if findings:
            return "comment"
        return "approve"

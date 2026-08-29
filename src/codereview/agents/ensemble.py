from __future__ import annotations

import logging
from typing import Optional

from codereview.config import ReviewerConfig
from codereview.finding_dedupe import dedupe_findings
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, PullRequestContext, SEVERITY_ORDER, Severity

logger = logging.getLogger(__name__)

ENSEMBLE_SYSTEM = """You are the final verifier in a multi-agent PR review pipeline.
Given candidate findings from security and pattern agents, return JSON only:
{"findings":[{"category":"security|quality|testing|documentation|performance","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"rationale":"...","suggestion":"...","confidence":0.0-1.0}]}
Merge semantically duplicate findings into one stronger finding. Drop weak or unsupported items.
Do not invent new issues. Preserve the strongest severity and confidence for merged items."""


class EnsembleAgent:
    name = "ensemble"

    def aggregate(
        self,
        findings: list[Finding],
        config: ReviewerConfig,
        pr: PullRequestContext,
        llm: LLMClient | None = None,
    ) -> tuple[list[Finding], str, float, str, bool]:
        deduped = dedupe_findings(findings)
        llm_degraded = False

        if llm is not None and llm.available and config.ensemble.llm_verify and deduped:
            try:
                deduped = self._llm_verify(deduped, pr, llm)
            except Exception as exc:
                llm_degraded = True
                logger.warning("Ensemble LLM verification failed, using heuristic dedupe: %s", exc)

        filtered = [
            finding
            for finding in deduped
            if finding.confidence >= config.posting.min_confidence
            and self._meets_threshold(finding.severity, config.severity_threshold)
        ]
        filtered.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.confidence), reverse=True)

        overall_confidence = self._overall_confidence(filtered)
        summary = self._build_summary(filtered, pr, llm_degraded=llm_degraded)
        verdict = self._verdict(filtered)
        return filtered, summary, overall_confidence, verdict, llm_degraded

    def _llm_verify(self, findings: list[Finding], pr: PullRequestContext, llm: LLMClient) -> list[Finding]:
        payload_lines = []
        for idx, finding in enumerate(findings, start=1):
            payload_lines.append(
                f"{idx}. [{finding.severity.value}/{finding.category.value}] {finding.title} "
                f"file={finding.file} line={finding.line} confidence={finding.confidence:.2f} "
                f"agent={finding.agent}\n   rationale: {finding.rationale}"
            )
        user = (
            f"PR: {pr.owner}/{pr.repo}#{pr.number} - {pr.title}\n\n"
            f"Candidate findings:\n" + "\n".join(payload_lines)
        )
        payload = llm.complete_json(ENSEMBLE_SYSTEM, user)
        verified = findings_from_payload(payload, self.name)
        return dedupe_findings(verified) if verified else findings

    def _meets_threshold(self, severity: Severity, threshold: Severity) -> bool:
        return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[threshold]

    def _overall_confidence(self, findings: list[Finding]) -> float:
        if not findings:
            return 1.0
        return round(sum(finding.confidence for finding in findings) / len(findings), 3)

    def _build_summary(self, findings: list[Finding], pr: PullRequestContext, *, llm_degraded: bool) -> str:
        if not findings:
            summary = (
                f"Automated review for `{pr.owner}/{pr.repo}#{pr.number}` found no issues above the "
                f"configured threshold."
            )
        else:
            by_severity: dict[str, int] = {}
            for finding in findings:
                by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
            parts = [f"{count} {severity}" for severity, count in sorted(by_severity.items())]
            summary = (
                f"Automated multi-agent review for `{pr.owner}/{pr.repo}#{pr.number}` found "
                f"{len(findings)} issue(s): {', '.join(parts)}."
            )
        if llm_degraded:
            summary += " (LLM verification degraded; heuristic dedupe used.)"
        return summary

    def _verdict(self, findings: list[Finding]) -> str:
        if any(finding.severity in {Severity.CRITICAL, Severity.HIGH} for finding in findings):
            return "request_changes"
        if findings:
            return "comment"
        return "approve"

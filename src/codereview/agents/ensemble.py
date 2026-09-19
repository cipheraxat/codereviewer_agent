from __future__ import annotations

import logging

from codereview.agents.pattern import anchor_findings
from codereview.config import ReviewerConfig
from codereview.finding_dedupe import dedupe_findings
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import SEVERITY_ORDER, Finding, PullRequestContext, Severity

logger = logging.getLogger(__name__)

ENSEMBLE_SYSTEM = """You are the final verifier in a multi-agent PR review pipeline.
Given candidate findings from security and pattern agents, return JSON only:
{"findings":[{"category":"security|quality|testing|documentation|performance","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"evidence_snippet":"...","rationale":"...","suggestion":"...","confidence":0.0-1.0}]}
Merge semantically duplicate findings into one stronger finding. Drop weak or unsupported items.
Do not invent new issues. Preserve the strongest severity and confidence for merged items.
Keep evidence_snippet when present."""

FACT_CHECK_SYSTEM = """You are a fact-checker for code review comments.
You can see the PR diffs and a numbered list of findings.
Remove ONLY findings that the diff PROVES are factually wrong.
When unsure, keep the finding. Do not judge usefulness or style.
Return JSON only: {"keep_indices":[1,2,5]} using the 1-based indices provided.
An empty keep_indices list means every finding was proven wrong — that is allowed."""

FACT_CHECK_STRICT_SYSTEM = """You are a strict fact-checker for code review comments.
For each finding, verify the claim against the exact hunk and any tool context provided.
Remove findings that are:
- factually contradicted by the diff, OR
- referring to code that is not in an added (+) line, OR
- duplicating another kept finding with weaker evidence.
When unsure, keep the finding.
Return JSON only: {"keep_indices":[1,2,5]} using the 1-based indices provided.
An empty keep_indices list means every finding was proven wrong — that is allowed."""

MAX_FACT_CHECK_DIFF_CHARS = 24_000
HEURISTIC_FLOOR = 0.75


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
        rounds = self._effort_rounds(config.review.effort)

        if llm is not None and llm.available and config.ensemble.llm_verify and deduped:
            try:
                verified = anchor_findings(self._llm_verify(deduped, pr, llm), pr)
                deduped = self._union_merge(deduped, verified)
            except Exception as exc:
                llm_degraded = True
                logger.warning("Ensemble LLM verification failed, using heuristic dedupe: %s", exc)

        # Effort: low=1 fact-check, medium=1, high=2 (second pass uses strict prompt + tool context hint).
        fact_check_passes = 2 if rounds >= 3 else 1
        if llm is not None and llm.available and config.ensemble.fact_check and deduped:
            for pass_idx in range(fact_check_passes):
                try:
                    before = len(deduped)
                    deduped = self._fact_check(deduped, pr, llm, pass_idx=pass_idx, effort=config.review.effort)
                    if len(deduped) == before and pass_idx > 0:
                        break
                except Exception as exc:
                    llm_degraded = True
                    logger.warning("Fact-check pass %s failed, keeping candidates: %s", pass_idx + 1, exc)
                    break

        threshold = config.severity_threshold
        if config.review.precision_mode and config.review.effort == "low":
            if threshold == Severity.LOW:
                threshold = Severity.MEDIUM

        filtered = [
            finding
            for finding in deduped
            if finding.confidence >= config.posting.min_confidence
            and self._meets_threshold(finding.severity, threshold)
        ]
        filtered.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.confidence), reverse=True)

        overall_confidence = self._overall_confidence(filtered)
        summary = self._build_summary(filtered, pr, llm_degraded=llm_degraded, effort=config.review.effort)
        verdict = self._verdict(filtered)
        return filtered, summary, overall_confidence, verdict, llm_degraded

    def _effort_rounds(self, effort: str) -> int:
        mapping = {"low": 1, "medium": 2, "high": 3}
        return mapping.get(effort.lower(), 2)

    def _union_merge(self, originals: list[Finding], verified: list[Finding]) -> list[Finding]:
        """Keep high-confidence heuristics; fold in LLM merges without silent drops."""
        protected = [finding for finding in originals if finding.confidence >= HEURISTIC_FLOOR]
        return dedupe_findings(protected + verified)

    def _llm_verify(self, findings: list[Finding], pr: PullRequestContext, llm: LLMClient) -> list[Finding]:
        payload_lines = []
        for idx, finding in enumerate(findings, start=1):
            payload_lines.append(
                f"{idx}. [{finding.severity.value}/{finding.category.value}] {finding.title} "
                f"file={finding.file} line={finding.line} confidence={finding.confidence:.2f} "
                f"agent={finding.agent}\n   rationale: {finding.rationale}"
                f"\n   evidence: {finding.evidence_snippet or ''}"
            )
        user = f"PR: {pr.owner}/{pr.repo}#{pr.number} - {pr.title}\n\nCandidate findings:\n" + "\n".join(payload_lines)
        payload = llm.complete_json(ENSEMBLE_SYSTEM, user)
        verified = findings_from_payload(payload, self.name)
        return dedupe_findings(verified) if verified else findings

    def _fact_check(
        self,
        findings: list[Finding],
        pr: PullRequestContext,
        llm: LLMClient,
        *,
        pass_idx: int = 0,
        effort: str = "medium",
    ) -> list[Finding]:
        patches = self._capped_diffs(pr, findings=findings)
        lines = []
        for idx, finding in enumerate(findings, start=1):
            lines.append(
                f"{idx}. [{finding.severity.value}] {finding.title} @ {finding.file}:{finding.line}\n"
                f"   rationale: {finding.rationale}\n"
                f"   evidence: {finding.evidence_snippet or ''}"
            )
        system = FACT_CHECK_STRICT_SYSTEM if pass_idx > 0 or effort.lower() == "high" else FACT_CHECK_SYSTEM
        hint = ""
        if pass_idx > 0:
            hint = (
                f"\nThis is fact-check pass {pass_idx + 1} (effort={effort}). "
                "Re-examine remaining findings against added lines only.\n"
            )
        user = f"{hint}Diffs:\n{patches}\n\nFindings:\n" + "\n".join(lines)
        payload = llm.complete_json(system, user)
        if "keep_indices" not in payload and "keep" not in payload:
            return findings
        keep = payload.get("keep_indices", payload.get("keep"))
        if not isinstance(keep, list):
            return findings
        if len(keep) == 0:
            return []
        keep_set = {int(item) for item in keep if str(item).isdigit() or isinstance(item, int)}
        return [finding for idx, finding in enumerate(findings, start=1) if idx in keep_set]

    def _capped_diffs(self, pr: PullRequestContext, *, findings: list[Finding] | None = None) -> str:
        """Prefer patches cited by findings so fact-check can see the relevant hunks."""
        cited: list[str] = []
        other: list[str] = []
        cited_paths = {f.file for f in (findings or []) if f.file}
        for path in pr.patches:
            (cited if path in cited_paths else other).append(path)
        ordered = cited + other

        chunks: list[str] = []
        used = 0
        for path in ordered:
            patch = pr.patches[path]
            remaining = MAX_FACT_CHECK_DIFF_CHARS - used
            if remaining <= 0:
                chunks.append("… [diff truncated for fact-check]")
                break
            body = patch if len(patch) <= remaining else patch[:remaining] + "\n… [truncated]"
            block = f"### {path}\n```diff\n{body}\n```"
            chunks.append(block)
            used += len(block)
        return "\n\n".join(chunks)

    def _meets_threshold(self, severity: Severity, threshold: Severity) -> bool:
        return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[threshold]

    def _overall_confidence(self, findings: list[Finding]) -> float:
        if not findings:
            return 1.0
        return round(sum(finding.confidence for finding in findings) / len(findings), 3)

    def _build_summary(
        self,
        findings: list[Finding],
        pr: PullRequestContext,
        *,
        llm_degraded: bool,
        effort: str = "medium",
    ) -> str:
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
        summary += f" (effort={effort})"
        if llm_degraded:
            summary += " (LLM verification degraded; heuristic dedupe used.)"
        return summary

    def _verdict(self, findings: list[Finding]) -> str:
        if any(finding.severity in {Severity.CRITICAL, Severity.HIGH} for finding in findings):
            return "request_changes"
        if findings:
            return "comment"
        return "approve"

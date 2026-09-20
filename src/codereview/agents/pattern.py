from __future__ import annotations

import logging

from codereview.config import CustomRule, ReviewerConfig
from codereview.diff_utils import evidence_from_match, redact_evidence, resolve_finding_line
from codereview.finding_dedupe import dedupe_findings
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, FindingCategory, PullRequestContext, Severity
from codereview.path_utils import should_ignore_path
from codereview.prompt_utils import UNTRUSTED_SYSTEM_ADDENDUM, build_delimited_user_prompt
from codereview.rule_packs import all_applicable_rules

logger = logging.getLogger(__name__)

PATTERN_SYSTEM = f"""You are a senior software engineer reviewing code quality and team conventions.
{UNTRUSTED_SYSTEM_ADDENDUM}
Return JSON only with shape:
{{"findings":[{{"category":"quality|testing|documentation|performance","severity":"low|medium|high|critical","title":"...","file":"path or null","line":123,"evidence_snippet":"1-3 consecutive added lines from the diff","rationale":"...","suggestion":"...","confidence":0.0-1.0}}]}}
Focus on maintainability, missing tests, unclear APIs, error handling, and convention violations.
Only report issues grounded in the provided diff/context.
Always set evidence_snippet to the exact added code lines the issue refers to (no diff +/- prefixes)."""

_CATEGORY_MAP = {
    "security": FindingCategory.SECURITY,
    "quality": FindingCategory.QUALITY,
    "testing": FindingCategory.TESTING,
    "documentation": FindingCategory.DOCUMENTATION,
    "performance": FindingCategory.PERFORMANCE,
}


def build_review_prompt(pr: PullRequestContext, context_block: str, config: ReviewerConfig) -> str:
    patches = "\n\n".join(f"### {path}\n```diff\n{patch}\n```" for path, patch in pr.patches.items())
    return build_delimited_user_prompt(
        parts=[
            ("pr_metadata", f"{pr.owner}/{pr.repo}#{pr.number}\nTitle: {pr.title}\nBody:\n{pr.body or ''}"),
            ("changed_files", ", ".join(pr.changed_files)),
            ("diffs", patches),
            ("relevant_context", context_block),
        ],
        footer=f"Team conventions (trusted config):\n{config.team_conventions}",
    )


def merge_findings(left: list[Finding], right: list[Finding]) -> list[Finding]:
    return dedupe_findings(left + right)


def anchor_findings(findings: list[Finding], pr: PullRequestContext) -> list[Finding]:
    """Resolve line numbers from evidence snippets when possible; redact secrets."""
    anchored: list[Finding] = []
    for finding in findings:
        patch = pr.patches.get(finding.file or "", "") if finding.file else ""
        line = resolve_finding_line(patch, evidence=finding.evidence_snippet)
        updates: dict = {}
        if line is not None:
            updates["line"] = line
        if finding.evidence_snippet:
            updates["evidence_snippet"] = redact_evidence(finding.evidence_snippet)
        if updates:
            finding = finding.model_copy(update=updates)
        anchored.append(finding)
    return anchored


def iter_reviewable_patches(pr: PullRequestContext, config: ReviewerConfig):
    for path, patch in pr.patches.items():
        if should_ignore_path(path, config.ignore_globs):
            continue
        yield path, patch


def _category_for_rule(rule: CustomRule) -> FindingCategory:
    return _CATEGORY_MAP.get(rule.category.lower(), FindingCategory.QUALITY)


def match_rules(
    pr: PullRequestContext,
    config: ReviewerConfig,
    *,
    agent: str,
    category_filter: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    for path, patch in iter_reviewable_patches(pr, config):
        rules: list[CustomRule] = all_applicable_rules(path, config)
        for rule in rules:
            if category_filter == "security" and rule.category != "security":
                continue
            if category_filter == "quality" and rule.category == "security":
                continue
            # Only fire when the pattern hits an *added* line (OCR-style).
            evidence = evidence_from_match(patch, rule.pattern)
            if evidence is None:
                continue
            category = _category_for_rule(rule)
            evidence = redact_evidence(evidence) or evidence
            findings.append(
                Finding(
                    category=category,
                    severity=rule.severity,
                    title=rule.description,
                    file=path,
                    line=resolve_finding_line(patch, evidence=evidence, pattern=rule.pattern),
                    rationale=f"Matched rule `{rule.id}`.",
                    suggestion="Align implementation with team conventions / language pack rules.",
                    confidence=0.8 if rule.category == "security" else 0.75,
                    agent=agent,
                    rule_id=rule.id,
                    evidence_snippet=evidence,
                )
            )
    return findings


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
            llm_findings = anchor_findings(findings_from_payload(payload, self.name), pr)
            return merge_findings(heuristic, llm_findings)
        except Exception as exc:
            logger.warning("Pattern LLM review failed, using heuristics only: %s", exc)
            return heuristic

    def _heuristic_scan(self, pr: PullRequestContext, config: ReviewerConfig) -> list[Finding]:
        findings: list[Finding] = []
        quality_patterns = [
            (r"TODO|FIXME|HACK", "Unresolved TODO/FIXME left in changed code", Severity.LOW),
        ]
        for path, patch in iter_reviewable_patches(pr, config):
            for pattern, title, severity in quality_patterns:
                evidence = evidence_from_match(patch, pattern)
                if evidence is None:
                    continue
                findings.append(
                    Finding(
                        category=FindingCategory.QUALITY,
                        severity=severity,
                        title=title,
                        file=path,
                        line=resolve_finding_line(patch, evidence=evidence, pattern=pattern),
                        rationale=f"Pattern `{pattern}` matched in PR diff.",
                        suggestion="Remove debug statements or track follow-up work in an issue.",
                        confidence=0.7,
                        agent=self.name,
                        evidence_snippet=evidence,
                    )
                )
        findings.extend(match_rules(pr, config, agent=self.name, category_filter="quality"))
        return findings

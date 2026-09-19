from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codereview.config import ReviewerConfig, Settings
from codereview.github_client import synthetic_pr_from_diff
from codereview.graph import ReviewOrchestrator
from codereview.models import Finding, Severity


@dataclass
class ExpectedFinding:
    title_contains: str
    severity: str | None = None
    file: str | None = None
    category: str | None = None


@dataclass
class GoldenCase:
    name: str
    diff_path: Path
    repo_fixture: Path | None
    expected: list[ExpectedFinding]
    should_not_find: list[str]

    @classmethod
    def load(cls, case_dir: Path) -> GoldenCase:
        labels = json.loads((case_dir / "labels.json").read_text())
        repo_fixture = case_dir / "repo"
        return cls(
            name=case_dir.name,
            diff_path=case_dir / "diff.patch",
            repo_fixture=repo_fixture if repo_fixture.exists() else None,
            expected=[ExpectedFinding(**item) for item in labels.get("expected", [])],
            should_not_find=labels.get("should_not_find", []),
        )


@dataclass
class CaseResult:
    name: str
    precision: float
    recall: float
    true_positives: int
    false_positives: int
    false_negatives: int
    findings: list[Finding]


def _matches_expected(finding: Finding, expected: ExpectedFinding) -> bool:
    if expected.title_contains.lower() not in finding.title.lower():
        return False
    if expected.severity and finding.severity.value != expected.severity:
        return False
    if expected.file and finding.file != expected.file:
        return False
    if expected.category and finding.category.value != expected.category:
        return False
    return True


def _is_blocked_finding(finding: Finding, blocked: list[str]) -> bool:
    title = finding.title.lower()
    return any(fragment.lower() in title for fragment in blocked)


def _eval_config(config: ReviewerConfig, *, production: bool) -> ReviewerConfig:
    if production:
        # Posting-threshold gate: keep production severity/confidence/precision_mode.
        # LLM verify/fact-check stay off so the gate is deterministic without an API key.
        # Name reflects thresholds, not the full production LLM path.
        return config.model_copy(
            update={
                "ensemble": config.ensemble.model_copy(update={"llm_verify": False, "fact_check": False}),
                "review": config.review.model_copy(update={"use_tools": False}),
            }
        )
    return config.model_copy(
        update={
            "severity_threshold": Severity.LOW,
            "ensemble": config.ensemble.model_copy(update={"llm_verify": False, "fact_check": False}),
            "posting": config.posting.model_copy(update={"min_confidence": 0.55}),
            "review": config.review.model_copy(update={"use_tools": False, "precision_mode": False}),
        }
    )


def evaluate_case(
    case: GoldenCase,
    config: ReviewerConfig,
    settings: Settings,
    *,
    repo_root: Path,
    production: bool = False,
) -> CaseResult:
    eval_config = _eval_config(config, production=production)
    diff_text = case.diff_path.read_text()
    pr = synthetic_pr_from_diff(diff_text, title=f"Eval case {case.name}")
    fixture_root = case.repo_fixture or repo_root
    orchestrator = ReviewOrchestrator(repo_root=fixture_root, config=eval_config, settings=settings)
    report = orchestrator.run(pr)
    findings = report.findings

    matched_expected: set[int] = set()
    true_positives = 0
    false_positives = 0

    for finding in findings:
        if _is_blocked_finding(finding, case.should_not_find):
            false_positives += 1
            continue
        hit = False
        for idx, expected in enumerate(case.expected):
            if idx in matched_expected:
                continue
            if _matches_expected(finding, expected):
                matched_expected.add(idx)
                true_positives += 1
                hit = True
                break
        if not hit:
            false_positives += 1

    false_negatives = len(case.expected) - len(matched_expected)
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 1.0
    recall = true_positives / len(case.expected) if case.expected else 1.0

    return CaseResult(
        name=case.name,
        precision=round(precision, 3),
        recall=round(recall, 3),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        findings=findings,
    )


def _summarize(results: list[CaseResult]) -> dict[str, Any]:
    if not results:
        return {"precision": 0.0, "recall": 0.0, "case_count": 0}
    return {
        "precision": round(sum(r.precision for r in results) / len(results), 3),
        "recall": round(sum(r.recall for r in results) / len(results), 3),
        "case_count": len(results),
    }


def run_benchmark(
    benchmark_dir: Path,
    config: ReviewerConfig,
    settings: Settings,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    cases = sorted(
        [
            GoldenCase.load(path)
            for path in benchmark_dir.iterdir()
            if path.is_dir() and (path / "labels.json").exists()
        ],
        key=lambda c: c.name,
    )
    results = [evaluate_case(case, config, settings, repo_root=repo_root) for case in cases]
    # Production-config gate: measure the same cases under real posting thresholds.
    production_results = [evaluate_case(case, config, settings, repo_root=repo_root, production=True) for case in cases]

    return {
        "cases": [
            {
                "name": r.name,
                "precision": r.precision,
                "recall": r.recall,
                "true_positives": r.true_positives,
                "false_positives": r.false_positives,
                "false_negatives": r.false_negatives,
                "finding_count": len(r.findings),
            }
            for r in results
        ],
        "summary": _summarize(results),
        # Deterministic gate under production posting thresholds (not full LLM path).
        "posting_thresholds_summary": _summarize(production_results),
        "production_summary": _summarize(production_results),  # alias for backward compat
        "production_cases": [
            {
                "name": r.name,
                "precision": r.precision,
                "recall": r.recall,
                "true_positives": r.true_positives,
                "false_positives": r.false_positives,
                "false_negatives": r.false_negatives,
                "finding_count": len(r.findings),
            }
            for r in production_results
        ],
    }

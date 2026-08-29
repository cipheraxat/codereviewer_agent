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


def evaluate_case(
    case: GoldenCase,
    config: ReviewerConfig,
    settings: Settings,
    *,
    repo_root: Path,
) -> CaseResult:
    eval_config = config.model_copy(update={"severity_threshold": Severity.LOW})
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


def run_benchmark(
    benchmark_dir: Path,
    config: ReviewerConfig,
    settings: Settings,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    cases = sorted(
        [GoldenCase.load(path) for path in benchmark_dir.iterdir() if path.is_dir() and (path / "labels.json").exists()],
        key=lambda c: c.name,
    )
    results = [evaluate_case(case, config, settings, repo_root=repo_root) for case in cases]

    if not results:
        return {"cases": [], "summary": {"precision": 0.0, "recall": 0.0, "case_count": 0}}

    avg_precision = round(sum(r.precision for r in results) / len(results), 3)
    avg_recall = round(sum(r.recall for r in results) / len(results), 3)

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
        "summary": {
            "precision": avg_precision,
            "recall": avg_recall,
            "case_count": len(results),
        },
    }

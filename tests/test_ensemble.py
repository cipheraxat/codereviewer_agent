from codereview.agents.ensemble import EnsembleAgent
from codereview.config import ReviewerConfig
from codereview.github_client import synthetic_pr_from_diff
from codereview.llm import LLMClient
from codereview.models import Finding, FindingCategory, Severity


class _FakeLLM(LLMClient):
    def __init__(self, payloads: list[dict]) -> None:
        # Bypass Settings — only need complete_json + available.
        self.settings = type("S", (), {"llm_api_key": "x"})()
        self.provider = "fake"
        self.model = "fake"
        self.input_tokens = 0
        self.output_tokens = 0
        self._payloads = list(payloads)

    @property
    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str) -> dict:
        return self._payloads.pop(0)


def test_ensemble_dedupes_and_filters() -> None:
    agent = EnsembleAgent()
    config = ReviewerConfig(ensemble={"llm_verify": False, "fact_check": False})
    pr = synthetic_pr_from_diff("diff --git a/a.py b/a.py\n")

    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Duplicate",
            file="a.py",
            line=1,
            rationale="r1",
            suggestion="s1",
            confidence=0.9,
            agent="security",
        ),
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Duplicate",
            file="a.py",
            line=1,
            rationale="r2",
            suggestion="s2",
            confidence=0.6,
            agent="pattern",
        ),
        Finding(
            category=FindingCategory.QUALITY,
            severity=Severity.LOW,
            title="Noise",
            file="a.py",
            line=2,
            rationale="r3",
            suggestion="s3",
            confidence=0.2,
            agent="pattern",
        ),
    ]

    filtered, summary, confidence, verdict, llm_degraded = agent.aggregate(findings, config, pr)
    assert len(filtered) == 1
    assert filtered[0].confidence == 0.9
    assert confidence > 0
    assert verdict == "request_changes"
    assert "1 issue" in summary
    assert "effort=" in summary
    assert llm_degraded is False


def test_fact_check_empty_keep_drops_all() -> None:
    agent = EnsembleAgent()
    config = ReviewerConfig(ensemble={"llm_verify": False, "fact_check": True})
    pr = synthetic_pr_from_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n+x=1\n")
    findings = [
        Finding(
            category=FindingCategory.QUALITY,
            severity=Severity.HIGH,
            title="Invented issue",
            file="a.py",
            line=1,
            rationale="wrong",
            suggestion="n/a",
            confidence=0.9,
            agent="pattern",
        )
    ]
    llm = _FakeLLM([{"keep_indices": []}])
    filtered, _, _, verdict, _ = agent.aggregate(findings, config, pr, llm)
    assert filtered == []
    assert verdict == "approve"


def test_union_merge_keeps_heuristic_floor() -> None:
    agent = EnsembleAgent()
    config = ReviewerConfig(ensemble={"llm_verify": True, "fact_check": False})
    pr = synthetic_pr_from_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n+API_KEY='secret123'\n"
    )
    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Possible hardcoded secret",
            file="a.py",
            line=1,
            rationale="matched",
            suggestion="use env",
            confidence=0.85,
            agent="security",
        )
    ]
    # LLM verify returns empty — union merge must still keep the heuristic.
    llm = _FakeLLM([{"findings": []}])
    filtered, _, _, verdict, _ = agent.aggregate(findings, config, pr, llm)
    assert len(filtered) == 1
    assert verdict == "request_changes"

from pathlib import Path

from codereview.agents import EnsembleAgent
from codereview.config import ReviewerConfig
from codereview.context_engine import ContextEngine
from codereview.github_client import synthetic_pr_from_diff
from codereview.models import Finding, FindingCategory, Severity


FIXTURE = Path(__file__).parent / "fixtures" / "sample_diff.patch"


def test_context_engine_returns_changed_file_snippet(tmp_path: Path) -> None:
  repo = tmp_path
  auth_path = repo / "src" / "app"
  auth_path.mkdir(parents=True)
  (auth_path / "auth.py").write_text("def login():\n    return True\n")

  diff = FIXTURE.read_text()
  pr = synthetic_pr_from_diff(diff)
  engine = ContextEngine(repo, ReviewerConfig())
  snippets = engine.build_context(pr)

  assert any(s.path.endswith("auth.py") for s in snippets)


def test_ensemble_dedupes_and_filters() -> None:
  agent = EnsembleAgent()
  config = ReviewerConfig()
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
  assert llm_degraded is False

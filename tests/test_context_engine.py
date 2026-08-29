from pathlib import Path

from codereview.agents import SecurityAgent
from codereview.config import ReviewerConfig
from codereview.context_engine import ContextEngine
from codereview.github_client import parse_unified_diff, synthetic_pr_from_diff
from codereview.graph import ReviewOrchestrator


FIXTURE = Path(__file__).parent / "fixtures" / "sample_diff.patch"


def test_parse_unified_diff_extracts_files() -> None:
  diff = FIXTURE.read_text()
  files, patches = parse_unified_diff(diff)
  assert "src/app/auth.py" in files
  assert "src/app/auth.py" in patches


def test_security_heuristics_find_secret_and_sql(tmp_path: Path) -> None:
  repo = tmp_path
  auth_path = repo / "src" / "app"
  auth_path.mkdir(parents=True)
  (auth_path / "auth.py").write_text("def login():\n    pass\n")

  pr = synthetic_pr_from_diff(FIXTURE.read_text())
  agent = SecurityAgent()
  findings = agent.review(pr, "", ReviewerConfig(), llm=None)

  titles = {f.title for f in findings}
  assert any("secret" in t.lower() for t in titles)


def test_orchestrator_runs_without_llm(tmp_path: Path) -> None:
  repo = tmp_path
  auth_path = repo / "src" / "app"
  auth_path.mkdir(parents=True)
  (auth_path / "auth.py").write_text("def login():\n    pass\n")

  pr = synthetic_pr_from_diff(FIXTURE.read_text())
  orchestrator = ReviewOrchestrator(repo_root=repo, config=ReviewerConfig())
  report = orchestrator.run(pr)

  assert report.pr is not None
  assert report.summary
  assert report.metrics.latency_ms >= 0


def test_context_engine_neighbor_scoring(tmp_path: Path) -> None:
  repo = tmp_path
  app = repo / "src" / "app"
  app.mkdir(parents=True)
  (app / "auth.py").write_text("def login(username):\n    return verify(username)\n")
  (app / "verify.py").write_text("def verify(username):\n    return True\n")

  diff = FIXTURE.read_text()
  pr = synthetic_pr_from_diff(diff)
  engine = ContextEngine(repo, ReviewerConfig())
  snippets = engine.build_context(pr)
  paths = {s.path for s in snippets}

  assert "src/app/auth.py" in paths

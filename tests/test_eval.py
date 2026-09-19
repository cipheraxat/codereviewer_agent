from pathlib import Path

from codereview.config import ReviewerConfig, Settings
from codereview.eval import run_benchmark

MIN_PRECISION = 0.65
MIN_RECALL = 0.9
# Production defaults filter low-severity noise; keep a floor so regressions still fail CI.
MIN_PRODUCTION_RECALL = 0.5
MIN_PRODUCTION_PRECISION = 0.65


def test_benchmark_runs_on_golden_cases(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    root = Path(__file__).parent.parent
    config = ReviewerConfig.load(root / "reviewer.example.yaml")
    settings = Settings(llm_api_key=None)
    results = run_benchmark(
        root / "benchmarks" / "golden",
        config,
        settings,
        repo_root=root,
    )
    assert results["summary"]["case_count"] >= 5
    assert results["summary"]["precision"] >= MIN_PRECISION
    assert results["summary"]["recall"] >= MIN_RECALL
    assert results["production_summary"]["precision"] >= MIN_PRODUCTION_PRECISION
    assert results["production_summary"]["recall"] >= MIN_PRODUCTION_RECALL

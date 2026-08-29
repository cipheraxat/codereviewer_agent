from pathlib import Path

from codereview.config import ReviewerConfig, Settings
from codereview.eval import run_benchmark


def test_benchmark_runs_on_golden_cases() -> None:
    root = Path(__file__).parent.parent
    results = run_benchmark(
        root / "benchmarks" / "golden",
        ReviewerConfig(),
        Settings(),
        repo_root=root,
    )
    assert results["summary"]["case_count"] >= 2
    assert results["summary"]["precision"] > 0
    assert results["summary"]["recall"] > 0

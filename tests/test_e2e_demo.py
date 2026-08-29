from pathlib import Path

from codereview.demo_pipeline import run_demo_pipeline


def test_demo_pipeline_indexes_mock_knowledge_and_finds_security_issues() -> None:
    root = Path(__file__).resolve().parents[1]
    diff_file = root / "benchmarks/golden/case_001_secret_sql/diff.patch"
    repo_root = root / "benchmarks/golden/case_001_secret_sql/repo"
    fixtures_dir = root / "tests/fixtures/knowledge"

    result = run_demo_pipeline(
        diff_file=diff_file,
        repo_root=repo_root,
        fixtures_dir=fixtures_dir,
    )

    assert result.index_stats.documents >= 3
    assert result.index_stats.by_source.get("jira", 0) >= 1
    assert result.index_stats.by_source.get("confluence", 0) >= 1

    retrieved_paths = {snippet.path for snippet in result.context_snippets}
    assert "jira:CP-123" in retrieved_paths or "confluence:auth-security-policy" in retrieved_paths

    titles = {finding.title.lower() for finding in result.report.findings}
    assert any("secret" in title for title in titles)
    assert result.report.summary


def test_mock_knowledge_provider_loads_fixtures() -> None:
    from codereview.mock_knowledge import MockKnowledgeProvider

    fixtures_dir = Path(__file__).resolve().parents[1] / "tests/fixtures/knowledge"
    provider = MockKnowledgeProvider(fixtures_dir)
    docs = provider.fetch_for_indexing()

    sources = {doc.source for doc in docs}
    paths = {doc.path for doc in docs}
    assert "jira" in sources
    assert "confluence" in sources
    assert "jira:CP-123" in paths
    assert "confluence:auth-security-policy" in paths

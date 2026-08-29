from pathlib import Path
from unittest.mock import MagicMock, patch

from codereview.config import ExternalContextConfig, ReviewerConfig, VectorConfig
from codereview.context_engine import ContextEngine
from codereview.knowledge_indexer import KnowledgeIndexer
from codereview.models import KnowledgeDocument, PullRequestContext


def test_knowledge_indexer_collects_code_documents(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main():\n    return 1\n")
    (repo / "README.md").write_text("# App\n")

    config = ReviewerConfig(
        vector=VectorConfig(enabled=True, supabase={"enabled": True}),
    )
    indexer = KnowledgeIndexer(repo, config, settings=MagicMock())
    docs = indexer._collect_code_documents()

    paths = {doc.path for doc in docs}
    assert "src/app.py" in paths
    assert "README.md" in paths
    assert all(doc.source == "code" for doc in docs)


def test_unified_rag_skips_live_external_fetch(tmp_path: Path) -> None:
    pr = PullRequestContext(
        owner="org",
        repo="app",
        number=1,
        title="[CP-123] Auth change",
        body="Update login",
        head_sha="abc",
        base_ref="main",
        head_ref="feature/cp-123",
        changed_files=["src/auth.py"],
        patches={"src/auth.py": "+def login(): pass"},
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    pass\n")

    config = ReviewerConfig(
        vector=VectorConfig(enabled=True, unified_rag=True, supabase={"enabled": True}),
        external_context=ExternalContextConfig(enabled=True),
    )
    engine = ContextEngine(tmp_path, config, settings=MagicMock())
    with patch.object(engine, "_vector_enabled", return_value=True):
        with patch.object(engine.external_fetcher, "fetch") as live_fetch:
            with patch.object(engine, "_vector_search", return_value=[]):
                engine.build_context(pr)
                live_fetch.assert_not_called()


def test_legacy_mode_still_live_fetches_external(tmp_path: Path) -> None:
    pr = PullRequestContext(
        owner="org",
        repo="app",
        number=1,
        title="test",
        body=None,
        head_sha="abc",
        base_ref="main",
        head_ref="main",
        changed_files=[],
        patches={},
    )
    config = ReviewerConfig(
        vector=VectorConfig(enabled=False, unified_rag=False),
        external_context=ExternalContextConfig(enabled=True),
    )
    engine = ContextEngine(tmp_path, config, settings=MagicMock())

    with patch.object(engine.external_fetcher, "fetch", return_value=[]) as live_fetch:
        engine.build_context(pr)
        live_fetch.assert_called_once()


def test_external_fetch_for_indexing_returns_documents() -> None:
    from codereview.external_context import ExternalContextFetcher

    config = ExternalContextConfig(
        enabled=True,
        jira={"enabled": True, "projects": ["CP"]},
        confluence={"enabled": True, "spaces": ["ENG"]},
    )
    fetcher = ExternalContextFetcher(
        config,
        atlassian_email="user@example.com",
        atlassian_api_token="token",
        atlassian_domain="acme.atlassian.net",
    )

    mock_issue = {
        "key": "CP-1",
        "fields": {"summary": "Add auth", "description": "Details"},
    }
    mock_page = {
        "id": "123",
        "title": "Auth design",
        "body": {"storage": {"value": "<p>Policy</p>"}},
    }

    with patch("httpx.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        client.get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"issues": [mock_issue]}),
            MagicMock(status_code=200, json=lambda: {"results": [mock_page]}),
        ]
        docs = fetcher.fetch_for_indexing()

    assert len(docs) == 2
    assert any(doc.source == "jira" and doc.path == "jira:CP-1" for doc in docs)
    assert any(doc.source == "confluence" and doc.path == "confluence:123" for doc in docs)

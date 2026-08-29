from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codereview.chunking import chunk_text
from codereview.config import ReviewerConfig, Settings
from codereview.context_engine import ContextEngine
from codereview.embeddings import EmbeddingClient
from codereview.github_client import synthetic_pr_from_diff
from codereview.graph import ReviewOrchestrator
from codereview.in_memory_vector_store import InMemoryVectorStore
from codereview.knowledge_indexer import IndexStats, KnowledgeIndexer
from codereview.local_embeddings import LocalEmbeddingClient
from codereview.mock_knowledge import DEFAULT_FIXTURES_DIR, MockKnowledgeProvider
from codereview.models import CodeSnippet, PullRequestContext, ReviewReport


@dataclass
class DemoRunResult:
    report: ReviewReport
    index_stats: IndexStats
    context_snippets: list[CodeSnippet] = field(default_factory=list)
    context_block: str = ""


def run_demo_pipeline(
    *,
    diff_file: Path,
    repo_root: Path,
    repo_slug: str = "local/workspace",
    fixtures_dir: Path | None = None,
    config: ReviewerConfig | None = None,
    pr_title: str = "[CP-123] Harden auth login endpoint",
    pr_body: str = (
        "Implements CP-123 login changes.\n"
        "Design doc: https://acme.atlassian.net/wiki/spaces/ENG/pages/auth-security-policy/Auth+Policy"
    ),
) -> DemoRunResult:
    """Run index → retrieve → review using mock JIRA/Confluence and in-memory vectors."""
    repo_root = repo_root.resolve()
    fixtures_dir = (fixtures_dir or DEFAULT_FIXTURES_DIR).resolve()
    config = config or _demo_config()

    embeddings = LocalEmbeddingClient()
    vector_store = InMemoryVectorStore(config=config.vector.supabase)
    mock_provider = MockKnowledgeProvider(fixtures_dir)

    indexer = KnowledgeIndexer(
        repo_root=repo_root,
        config=config,
        settings=Settings(),
        embeddings=embeddings,
        vector_store=vector_store,
        external_fetcher=mock_provider,
    )
    index_stats = indexer.run(repo_slug, sources=["code", "jira", "confluence"])

    diff_text = diff_file.read_text(encoding="utf-8")
    pr = synthetic_pr_from_diff(diff_text, title=pr_title)
    pr.body = pr_body
    pr.head_ref = "feature/CP-123-auth-hardening"

    context_engine = ContextEngine(
        repo_root=repo_root,
        config=config,
        settings=Settings(),
        embeddings=embeddings,
        vector_store=vector_store,
        external_fetcher=mock_provider,
    )
    snippets = context_engine.build_context(pr)
    context_block = context_engine.format_context_block(snippets)

    orchestrator = ReviewOrchestrator(
        repo_root=repo_root,
        config=config,
        settings=Settings(),
        context_engine=context_engine,
    )
    report = orchestrator.run(pr)

    return DemoRunResult(
        report=report,
        index_stats=index_stats,
        context_snippets=snippets,
        context_block=context_block,
    )


def _demo_config() -> ReviewerConfig:
    return ReviewerConfig.model_validate(
        {
            "severity_threshold": "low",
            "external_context": {"enabled": True},
            "vector": {
                "enabled": True,
                "unified_rag": True,
                "supabase": {
                    "enabled": True,
                    "index_on_review": False,
                    "match_threshold": 0.05,
                    "vector_top_k": 8,
                },
            },
        }
    )

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from codereview.config import ReviewerConfig, Settings
from codereview.github_client import GitHubClient, parse_pr_ref, synthetic_pr_from_diff
from codereview.eval import run_benchmark
from codereview.graph import ReviewOrchestrator
from codereview.demo_pipeline import run_demo_pipeline
from codereview.knowledge_indexer import KnowledgeIndexer
from codereview.models import ReviewReport

app = typer.Typer(help="Multi-agent GitHub PR reviewer")
console = Console()


def _load_config(config_path: Path | None) -> ReviewerConfig:
    if config_path:
        return ReviewerConfig.load(config_path)
    for candidate in (Path("reviewer.yaml"), Path("reviewer.example.yaml")):
        if candidate.exists():
            return ReviewerConfig.load(candidate)
    return ReviewerConfig()


def _save_report(report: ReviewReport, output: Path) -> None:
    output.write_text(json.dumps(report.to_json_dict(), indent=2))


def _print_report(report: ReviewReport) -> None:
    console.print(f"[bold]{report.summary}[/bold]")
    console.print(
        f"Verdict: {report.verdict} | Confidence: {report.overall_confidence:.2f} | "
        f"Findings: {len(report.findings)} | Latency: {report.metrics.latency_ms} ms"
    )
    if not report.findings:
        return

    table = Table("Severity", "Category", "Title", "File", "Line", "Confidence")
    for finding in report.findings:
        table.add_row(
            finding.severity.value,
            finding.category.value,
            finding.title,
            finding.file or "-",
            str(finding.line or "-"),
            f"{finding.confidence:.2f}",
        )
    console.print(table)


@app.command("review-pr")
def review_pr(
    pr_ref: str = typer.Argument(..., help="Pull request reference like owner/repo#123"),
    repo_root: Path = typer.Option(Path("."), "--repo-root", help="Local checkout of the repository"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to reviewer.yaml"),
    output: Path = typer.Option(Path("review-report.json"), "--output", help="Where to write JSON report"),
    post: bool = typer.Option(True, "--post/--no-post", help="Post review comments to GitHub"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not post to GitHub"),
) -> None:
    """Review a GitHub pull request and optionally post structured comments."""
    settings = Settings()
    if dry_run:
        settings.dry_run = True

    config = _load_config(config_path)
    owner, repo, number = parse_pr_ref(pr_ref)

    token = settings.github_token
    if post and not dry_run and not token:
        raise typer.BadParameter("GITHUB_TOKEN is required to post reviews")

    pr_context = None
    if token:
        gh = GitHubClient(token)
        pr_context = gh.fetch_pull_request(owner, repo, number)
    else:
        raise typer.BadParameter("GITHUB_TOKEN is required to fetch PR data")

    orchestrator = ReviewOrchestrator(repo_root=repo_root.resolve(), config=config, settings=settings)
    report = orchestrator.run(pr_context)
    _save_report(report, output)
    _print_report(report)

    if post and token and not dry_run:
        posted = gh.post_review(
            report,
            dry_run=False,
            max_inline_comments=config.posting.max_inline_comments,
        )
        if posted:
            action = "updated" if posted.updated else "created"
            console.print(f"[green]Review {action} on commit {posted.commit_sha}[/green]")


@app.command("review-diff")
def review_diff(
    diff_file: Path = typer.Argument(..., help="Unified diff file to review"),
    repo_root: Path = typer.Option(Path("."), "--repo-root", help="Local checkout used for context retrieval"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to reviewer.yaml"),
    output: Path = typer.Option(Path("review-report.json"), "--output", help="Where to write JSON report"),
    title: str = typer.Option("Local diff review", "--title", help="Synthetic PR title"),
) -> None:
    """Review a local unified diff without contacting GitHub."""
    config = _load_config(config_path)
    diff_text = diff_file.read_text(encoding="utf-8")
    pr_context = synthetic_pr_from_diff(diff_text, title=title)

    orchestrator = ReviewOrchestrator(repo_root=repo_root.resolve(), config=config, settings=Settings())
    report = orchestrator.run(pr_context)
    _save_report(report, output)
    _print_report(report)


@app.command("eval")
def eval_benchmark(
    benchmark_dir: Path = typer.Option(Path("benchmarks/golden"), "--benchmark-dir"),
    config_path: Path | None = typer.Option(None, "--config"),
    output: Path = typer.Option(Path("benchmarks/results.json"), "--output"),
    repo_root: Path = typer.Option(Path("."), "--repo-root"),
) -> None:
    """Run precision/recall evaluation against golden labeled PR diffs."""
    config = _load_config(config_path)
    settings = Settings()
    results = run_benchmark(benchmark_dir.resolve(), config, settings, repo_root=repo_root.resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))

    summary = results["summary"]
    console.print(
        f"[bold]Benchmark complete[/bold] — cases: {summary['case_count']} | "
        f"avg precision: {summary['precision']} | avg recall: {summary['recall']}"
    )
    table = Table("Case", "Precision", "Recall", "TP", "FP", "FN")
    for case in results["cases"]:
        table.add_row(
            case["name"],
            str(case["precision"]),
            str(case["recall"]),
            str(case["true_positives"]),
            str(case["false_positives"]),
            str(case["false_negatives"]),
        )
    console.print(table)


@app.command("demo")
def demo(
    diff_file: Path = typer.Option(
        Path("benchmarks/golden/case_001_secret_sql/diff.patch"),
        "--diff",
        help="Unified diff to review in the demo pipeline",
    ),
    repo_root: Path = typer.Option(
        Path("benchmarks/golden/case_001_secret_sql/repo"),
        "--repo-root",
        help="Local repo checkout used for code indexing",
    ),
    fixtures_dir: Path = typer.Option(
        Path("tests/fixtures/knowledge"),
        "--fixtures-dir",
        help="Mock JIRA/Confluence JSON fixtures",
    ),
    output: Path = typer.Option(Path("demo-report.json"), "--output", help="Where to write JSON report"),
    show_context: bool = typer.Option(True, "--show-context/--no-show-context"),
) -> None:
    """Run end-to-end demo: mock JIRA/Confluence → in-memory index → PR review (no APIs)."""
    if not diff_file.exists():
        raise typer.BadParameter(f"Diff file not found: {diff_file}")
    if not repo_root.exists():
        raise typer.BadParameter(f"Repo root not found: {repo_root}")

    result = run_demo_pipeline(
        diff_file=diff_file,
        repo_root=repo_root,
        fixtures_dir=fixtures_dir,
    )

    _save_report(result.report, output)
    console.print("[bold cyan]Demo pipeline complete[/bold cyan] (mock JIRA/Confluence, in-memory vectors)")
    console.print(
        f"Indexed {result.index_stats.chunks} chunks from {result.index_stats.documents} documents "
        f"({', '.join(f'{k}={v}' for k, v in sorted(result.index_stats.by_source.items()))})"
    )

    if show_context:
        console.print("\n[bold]Retrieved context[/bold]")
        context_table = Table("Path", "Source", "Score", "Reason")
        for snippet in result.context_snippets:
            source = snippet.reason.split(":", 1)[-1] if ":" in snippet.reason else "-"
            context_table.add_row(snippet.path, source, f"{snippet.score:.2f}", snippet.reason)
        console.print(context_table)

    _print_report(result.report)


@app.command("index-knowledge")
def index_knowledge(
    repo_slug: str = typer.Option(..., "--repo", help="Knowledge partition slug, e.g. owner/repo"),
    repo_root: Path = typer.Option(Path("."), "--repo-root", help="Local checkout to index"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to reviewer.yaml"),
    sources: str = typer.Option(
        "",
        "--sources",
        help="Comma-separated sources: code,jira,confluence (defaults to reviewer.yaml vector.indexing.sources)",
    ),
) -> None:
    """Index repo code + JIRA + Confluence into Supabase for unified RAG retrieval."""
    config = _load_config(config_path)
    settings = Settings()
    source_list = [s.strip() for s in sources.split(",") if s.strip()] or None

    indexer = KnowledgeIndexer(repo_root=repo_root.resolve(), config=config, settings=settings)
    try:
        stats = indexer.run(repo_slug, sources=source_list)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(
        f"[bold green]Indexed {stats.chunks} chunks[/bold green] from {stats.documents} documents "
        f"into [cyan]{stats.repo}[/cyan]"
    )
    if stats.by_source:
        table = Table("Source", "Chunks")
        for source, count in sorted(stats.by_source.items()):
            table.add_row(source, str(count))
        console.print(table)
    if stats.skipped_sources:
        console.print(f"[yellow]Skipped sources:[/yellow] {', '.join(stats.skipped_sources)}")


@app.command("version")
def version() -> None:
    from codereview import __version__

    console.print(__version__)


if __name__ == "__main__":
    app()

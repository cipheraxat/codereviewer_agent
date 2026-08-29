from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from codereview.config import ReviewerConfig, Settings
from codereview.github_client import GitHubClient, parse_pr_ref, synthetic_pr_from_diff
from codereview.graph import ReviewOrchestrator
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
        posted = gh.post_review(report, dry_run=False)
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

    orchestrator = ReviewOrchestrator(repo_root=repo_root.resolve(), config=config)
    report = orchestrator.run(pr_context)
    _save_report(report, output)
    _print_report(report)


@app.command("version")
def version() -> None:
    from codereview import __version__

    console.print(__version__)


if __name__ == "__main__":
    app()

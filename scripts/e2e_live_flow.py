#!/usr/bin/env python3
"""
Live end-to-end flow for codereviewer_agent.

Uses real Supabase + GitHub + LLM. JIRA/Confluence keys are left empty.

Steps:
  1. Create branch + commit intentional issues in e2e_sandbox/
  2. Push branch and open a GitHub PR
  3. Index repo code into Supabase (code source only)
  4. Run unified RAG review on the PR and post comments
  5. Run benchmark eval

Required environment variables:
  GITHUB_TOKEN
  LLM_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Optional:
  LLM_PROVIDER (default: openrouter)
  LLM_MODEL

Usage:
  python scripts/e2e_live_flow.py
  python scripts/e2e_live_flow.py --no-post   # skip posting review to GitHub
  python scripts/e2e_live_flow.py --skip-pr   # skip PR creation; use --pr-ref owner/repo#123
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codereview.config import ReviewerConfig, Settings
from codereview.eval import run_benchmark
from codereview.github_client import GitHubClient, parse_pr_ref
from codereview.graph import ReviewOrchestrator
from codereview.knowledge_indexer import KnowledgeIndexer

SANDBOX_FILE = REPO_ROOT / "e2e_sandbox" / "auth_service.py"
CONFIG_PATH = REPO_ROOT / "reviewer.e2e.yaml"
REPORT_PATH = REPO_ROOT / "e2e-live-report.json"

INTENTIONAL_BAD_CODE = '''"""E2E probe — intentional security and quality issues for agent review."""

def login(username: str, password: str) -> bool:
    if not username or not password:
        return False

    api_key = "sk-live-super-secret-key"
    query = f"SELECT * FROM users WHERE name = '{username}'"
    result = db.execute(query)
    print("debug login", username)
    return verify_credentials(username, password)


def verify_credentials(username: str, password: str) -> bool:
    return bool(username and password)
'''


def _load_env_file() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _clear_atlassian_env() -> None:
    for key in ("ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN", "ATLASSIAN_DOMAIN"):
        os.environ.pop(key, None)


def _require_env() -> Settings:
    _load_env_file()
    _clear_atlassian_env()
    settings = Settings()
    missing = []
    if not settings.github_token:
        missing.append("GITHUB_TOKEN")
    if not settings.llm_api_key:
        missing.append("LLM_API_KEY")
    if not settings.supabase_url:
        missing.append("SUPABASE_URL")
    if not settings.supabase_key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in values."
        )
    return settings


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _git_remote_owner_repo() -> tuple[str, str]:
    result = _run(["git", "remote", "get-url", "origin"])
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)", url)
    if not match:
        raise SystemExit(f"Could not parse GitHub owner/repo from remote URL: {url}")
    return match.group("owner"), match.group("repo")


def _authenticated_remote() -> str:
    owner, repo = _git_remote_owner_repo()
    token = os.environ["GITHUB_TOKEN"]
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def _create_branch_and_pr(settings: Settings, *, branch: str, base: str) -> tuple[int, str]:
    owner, repo = _git_remote_owner_repo()
    gh = GitHubClient(settings.github_token or "")

    _run(["git", "fetch", "origin", base])
    _run(["git", "checkout", base])
    _run(["git", "pull", "origin", base])
    _run(["git", "checkout", "-B", branch])

    SANDBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    SANDBOX_FILE.write_text(INTENTIONAL_BAD_CODE, encoding="utf-8")

    _run(["git", "add", str(SANDBOX_FILE.relative_to(REPO_ROOT))])
    status = _run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        raise SystemExit("No changes to commit for E2E sandbox file")

    commit_msg = "test(e2e): add live flow probe with intentional review issues"
    _run(["git", "commit", "-m", commit_msg])
    push_url = _authenticated_remote()
    _run(["git", "push", push_url, f"HEAD:{branch}"])

    pr_body = (
        "Automated E2E live flow probe for codereviewer_agent.\n\n"
        "- Intentional hardcoded secret\n"
        "- String-interpolated SQL\n"
        "- Debug print in auth path\n\n"
        "This PR is created by `scripts/e2e_live_flow.py`."
    )
    pr_number = gh.create_pull_request(
        owner,
        repo,
        title="E2E live flow: auth probe (intentional issues)",
        body=pr_body,
        head=branch,
        base=base,
    )
    pr_ref = f"{owner}/{repo}#{pr_number}"
    print(f"Opened PR: https://github.com/{owner}/{repo}/pull/{pr_number}")
    return pr_number, pr_ref


def _index_code(settings: Settings, repo_slug: str, config: ReviewerConfig) -> dict:
    indexer = KnowledgeIndexer(repo_root=REPO_ROOT, config=config, settings=settings)
    stats = indexer.run(repo_slug, sources=["code"])
    return {
        "documents": stats.documents,
        "chunks": stats.chunks,
        "by_source": stats.by_source,
        "skipped_sources": stats.skipped_sources,
    }


def _review_pr(
    settings: Settings,
    config: ReviewerConfig,
    pr_ref: str,
    *,
    post: bool,
) -> dict:
    owner, repo, number = parse_pr_ref(pr_ref)
    gh = GitHubClient(settings.github_token or "")
    pr_context = gh.fetch_pull_request(owner, repo, number)

    orchestrator = ReviewOrchestrator(repo_root=REPO_ROOT, config=config, settings=settings)
    report = orchestrator.run(pr_context)

    posted = False
    if post:
        result = gh.post_review(
            report,
            dry_run=False,
            max_inline_comments=config.posting.max_inline_comments,
        )
        posted = result is not None

    return {
        "summary": report.summary,
        "verdict": report.verdict,
        "confidence": report.overall_confidence,
        "findings_count": len(report.findings),
        "findings": [f.model_dump(mode="json") for f in report.findings],
        "latency_ms": report.metrics.latency_ms,
        "posted_to_github": posted,
    }


def _run_eval(settings: Settings, config: ReviewerConfig) -> dict:
    return run_benchmark(
        (REPO_ROOT / "benchmarks" / "golden").resolve(),
        config,
        settings,
        repo_root=REPO_ROOT,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run live E2E codereviewer flow")
    parser.add_argument("--no-post", action="store_true", help="Do not post review to GitHub")
    parser.add_argument("--skip-pr", action="store_true", help="Skip branch/PR creation")
    parser.add_argument("--pr-ref", default="", help="Existing PR ref like owner/repo#123")
    parser.add_argument("--branch", default="", help="Branch name for new PR")
    parser.add_argument("--base", default="", help="Base branch (default: repo default branch)")
    args = parser.parse_args()

    settings = _require_env()
    config = ReviewerConfig.load(CONFIG_PATH)
    owner, repo = _git_remote_owner_repo()
    repo_slug = f"{owner}/{repo}"

    gh = GitHubClient(settings.github_token or "")
    base = args.base or gh.get_default_branch(owner, repo)
    branch = args.branch or f"e2e/live-flow-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo_slug": repo_slug,
        "components": {
            "supabase_indexing": False,
            "github_pr": False,
            "unified_rag_review": False,
            "github_post_review": False,
            "benchmark_eval": False,
            "jira_confluence": "skipped (no credentials)",
        },
    }

    print("\n=== 1/4 Create PR with intentional issues ===")
    if args.skip_pr:
        if not args.pr_ref:
            raise SystemExit("--skip-pr requires --pr-ref owner/repo#123")
        pr_ref = args.pr_ref
        summary["pr_ref"] = pr_ref
        print(f"Using existing PR: {pr_ref}")
    else:
        _, pr_ref = _create_branch_and_pr(settings, branch=branch, base=base)
        summary["pr_ref"] = pr_ref
        summary["branch"] = branch
        summary["components"]["github_pr"] = True
        time.sleep(3)

    print("\n=== 2/4 Index repo code into Supabase (no JIRA/Confluence) ===")
    index_result = _index_code(settings, repo_slug, config)
    summary["indexing"] = index_result
    summary["components"]["supabase_indexing"] = index_result["chunks"] > 0
    print(
        f"Indexed {index_result['chunks']} chunks from {index_result['documents']} documents "
        f"({index_result['by_source']})"
    )

    print("\n=== 3/4 Run unified RAG review on PR ===")
    review_result = _review_pr(settings, config, pr_ref, post=not args.no_post)
    summary["review"] = review_result
    summary["components"]["unified_rag_review"] = review_result["findings_count"] > 0
    summary["components"]["github_post_review"] = review_result["posted_to_github"]
    print(review_result["summary"])
    print(
        f"Verdict: {review_result['verdict']} | "
        f"Findings: {review_result['findings_count']} | "
        f"Posted: {review_result['posted_to_github']}"
    )

    print("\n=== 4/4 Run benchmark eval ===")
    eval_result = _run_eval(settings, config)
    summary["eval"] = eval_result["summary"]
    summary["components"]["benchmark_eval"] = True
    print(
        f"Benchmark — cases: {eval_result['summary']['case_count']} | "
        f"precision: {eval_result['summary']['precision']} | "
        f"recall: {eval_result['summary']['recall']}"
    )

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    REPORT_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote report: {REPORT_PATH}")
    print(json.dumps(summary["components"], indent=2))


if __name__ == "__main__":
    main()

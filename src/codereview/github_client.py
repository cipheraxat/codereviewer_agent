from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from github import Github
from github.GithubException import GithubException
from github.PullRequest import PullRequest

from codereview.models import Finding, PullRequestContext, ReviewReport


@dataclass
class PostedReview:
    review_id: int
    commit_sha: str
    updated: bool


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._gh = Github(token)

    def fetch_pull_request(self, owner: str, repo: str, number: int) -> PullRequestContext:
        repository = self._gh.get_repo(f"{owner}/{repo}")
        pr: PullRequest = repository.get_pull(number)
        files = pr.get_files()
        changed_files: list[str] = []
        patches: dict[str, str] = {}
        for file in files:
            if file.filename:
                changed_files.append(file.filename)
                if file.patch:
                    patches[file.filename] = file.patch

        return PullRequestContext(
            owner=owner,
            repo=repo,
            number=number,
            title=pr.title,
            body=pr.body,
            head_sha=pr.head.sha,
            base_ref=pr.base.ref,
            head_ref=pr.head.ref,
            changed_files=changed_files,
            patches=patches,
        )

    def post_review(
        self,
        report: ReviewReport,
        *,
        dry_run: bool = False,
        bot_marker: str = "<!-- codereview-agent -->",
    ) -> PostedReview | None:
        if report.pr is None:
            raise ValueError("Review report is missing PR context")

        owner = report.pr.owner
        repo = report.pr.repo
        number = report.pr.number
        commit_sha = report.commit_sha or report.pr.head_sha

        body = self._format_review_body(report, bot_marker)
        comments = self._format_inline_comments(report)

        if dry_run:
            return None

        repository = self._gh.get_repo(f"{owner}/{repo}")
        pr = repository.get_pull(number)

        existing = self._find_existing_review(pr, commit_sha, bot_marker)
        if existing is not None:
            existing.edit(body=body)
            return PostedReview(review_id=existing.id, commit_sha=commit_sha, updated=True)

        event = self._map_verdict(report.verdict)
        review = pr.create_review(
            commit=repository.get_commit(commit_sha),
            body=body,
            event=event,
            comments=comments,
        )
        return PostedReview(review_id=review.id, commit_sha=commit_sha, updated=False)

    def _find_existing_review(self, pr: PullRequest, commit_sha: str, bot_marker: str):
        for review in pr.get_reviews():
            if review.commit_id == commit_sha and review.body and bot_marker in review.body:
                return review
        return None

    def _map_verdict(self, verdict: str) -> str:
        mapping = {
            "approve": "APPROVE",
            "request_changes": "REQUEST_CHANGES",
            "comment": "COMMENT",
        }
        return mapping.get(verdict, "COMMENT")

    def _format_review_body(self, report: ReviewReport, bot_marker: str) -> str:
        lines = [
            bot_marker,
            report.summary,
            "",
            f"Overall confidence: **{report.overall_confidence:.2f}**",
            f"Findings: **{len(report.findings)}**",
        ]
        if report.metrics.latency_ms:
            lines.append(f"Latency: {report.metrics.latency_ms} ms")
        if report.metrics.estimated_cost_usd:
            lines.append(f"Estimated LLM cost: ${report.metrics.estimated_cost_usd:.4f}")

        if report.findings:
            lines.append("")
            lines.append("## Structured findings")
            for idx, finding in enumerate(report.findings, start=1):
                location = ""
                if finding.file:
                    location = f" (`{finding.file}`"
                    if finding.line:
                        location += f":{finding.line}"
                    location += ")"
                lines.append(
                    f"{idx}. **[{finding.severity.value.upper()} / {finding.category.value}]** "
                    f"{finding.title}{location} — confidence {finding.confidence:.2f}"
                )
                lines.append(f"   - Rationale: {finding.rationale}")
                lines.append(f"   - Suggestion: {finding.suggestion}")
        return "\n".join(lines)

    def _format_inline_comments(self, report: ReviewReport) -> list[dict[str, str | int]]:
        comments: list[dict[str, str | int]] = []
        max_comments = 25
        for finding in report.findings:
            if not finding.file or not finding.line:
                continue
            if len(comments) >= max_comments:
                break
            comments.append(
                {
                    "path": finding.file,
                    "line": finding.line,
                    "body": (
                        f"**[{finding.severity.value}] {finding.title}**\n\n"
                        f"{finding.rationale}\n\n"
                        f"Suggestion: {finding.suggestion}"
                    ),
                }
            )
        return comments


def parse_pr_ref(ref: str) -> tuple[str, str, int]:
    match = re.match(r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<number>\d+)$", ref.strip())
    if not match:
        raise ValueError("PR ref must look like owner/repo#123")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_unified_diff(diff_text: str) -> tuple[list[str], dict[str, str]]:
    changed_files: list[str] = []
    patches: dict[str, str] = {}
    current_file: str | None = None
    current_lines: list[str] = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_file and current_lines:
                patches[current_file] = "\n".join(current_lines)
            match = re.search(r"b/(.+)$", line)
            current_file = match.group(1) if match else None
            if current_file and current_file not in changed_files:
                changed_files.append(current_file)
            current_lines = [line]
        elif current_file is not None:
            current_lines.append(line)

    if current_file and current_lines:
        patches[current_file] = "\n".join(current_lines)

    return changed_files, patches


def synthetic_pr_from_diff(diff_text: str, title: str = "Local diff review") -> PullRequestContext:
    changed_files, patches = parse_unified_diff(diff_text)
    digest = hashlib.sha1(diff_text.encode("utf-8")).hexdigest()[:12]
    return PullRequestContext(
        owner="local",
        repo="workspace",
        number=0,
        title=title,
        body=None,
        head_sha=digest,
        base_ref="main",
        head_ref="feature",
        changed_files=changed_files,
        patches=patches,
    )

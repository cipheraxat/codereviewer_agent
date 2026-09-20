from unittest.mock import MagicMock, patch

from codereview.github_client import GitHubClient
from codereview.llm import LLMClient, findings_from_payload
from codereview.models import Finding, FindingCategory, PullRequestContext, ReviewReport, Severity


def test_findings_from_payload_skips_malformed_rows() -> None:
    payload = {
        "findings": [
            {"title": "Valid", "severity": "high", "category": "security", "rationale": "x", "suggestion": "y"},
            {"severity": "high"},
        ]
    }
    findings = findings_from_payload(payload, "security")
    assert len(findings) == 1
    assert findings[0].title == "Valid"


def test_llm_retries_then_raises() -> None:
    settings = MagicMock()
    settings.llm_api_key = "key"
    settings.llm_provider = "openai"
    settings.resolved_model.return_value = "gpt-4o-mini"
    client = LLMClient(settings)

    with patch.object(client, "_openai_json", side_effect=RuntimeError("boom")):
        with patch("codereview.llm.time.sleep"):
            try:
                client.complete_json("system", "user")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as exc:
                assert "after retries" in str(exc)


def test_post_review_respects_max_inline_comments() -> None:
    pr = PullRequestContext(
        owner="o",
        repo="r",
        number=1,
        title="t",
        body=None,
        head_sha="abc",
        base_ref="main",
        head_ref="feature",
        changed_files=["a.py"],
        patches={},
    )
    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title=f"Issue {idx}",
            file="a.py",
            line=idx,
            rationale="r",
            suggestion="s",
            confidence=0.9,
            agent="security",
        )
        for idx in range(1, 6)
    ]
    report = ReviewReport(pr=pr, findings=findings, summary="s", overall_confidence=0.9, verdict="comment")

    gh = GitHubClient("token")
    comments = gh._format_inline_comments(report, max_inline_comments=2)
    assert len(comments) == 2


def test_post_review_falls_back_when_actions_cannot_approve() -> None:
    from github import GithubException

    pr = PullRequestContext(
        owner="o",
        repo="r",
        number=1,
        title="t",
        body=None,
        head_sha="abc123",
        base_ref="main",
        head_ref="feature",
        changed_files=["a.py"],
        patches={},
    )
    report = ReviewReport(pr=pr, findings=[], summary="clean", overall_confidence=1.0, verdict="approve")

    gh = GitHubClient("token")
    mock_pr = MagicMock()
    mock_pr.get_reviews.return_value = []
    mock_pr.create_review.side_effect = [
        GithubException(
            422,
            {
                "message": "Unprocessable Entity",
                "errors": ["GitHub Actions is not permitted to approve pull requests."],
            },
            None,
        ),
        MagicMock(id=42),
    ]
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    mock_repo.get_commit.return_value = MagicMock()

    with patch.object(gh, "_gh") as mock_gh:
        mock_gh.get_repo.return_value = mock_repo
        posted = gh.post_review(report, dry_run=False)

    assert posted is not None
    assert posted.review_id == 42
    assert mock_pr.create_review.call_count == 2
    assert mock_pr.create_review.call_args_list[1].kwargs["event"] == "COMMENT"

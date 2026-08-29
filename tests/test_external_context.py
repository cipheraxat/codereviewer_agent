from codereview.models import PullRequestContext

from codereview.external_context import extract_ticket_keys, extract_confluence_page_ids


def test_extract_ticket_keys_from_title_and_branch() -> None:
    pr = PullRequestContext(
        owner="org",
        repo="app",
        number=1,
        title="[CP-123] Add export",
        body="See also ENG-9",
        head_sha="abc",
        base_ref="main",
        head_ref="feature/CP-123-export",
        changed_files=[],
        patches={},
    )
    keys = extract_ticket_keys(pr)
    assert "CP-123" in keys
    assert "ENG-9" in keys


def test_extract_confluence_page_id() -> None:
    pr = PullRequestContext(
        owner="org",
        repo="app",
        number=1,
        title="Design update",
        body="Doc: https://acme.atlassian.net/wiki/spaces/ENG/pages/123456/Export+Design",
        head_sha="abc",
        base_ref="main",
        head_ref="feature/x",
        changed_files=[],
        patches={},
    )
    ids = extract_confluence_page_ids(pr)
    assert "123456" in ids


def test_external_context_disabled_returns_empty() -> None:
    from codereview.config import ExternalContextConfig
    from codereview.external_context import ExternalContextFetcher

    fetcher = ExternalContextFetcher(ExternalContextConfig(enabled=False))
    pr = PullRequestContext(
        owner="o",
        repo="r",
        number=1,
        title="[CP-1] x",
        body=None,
        head_sha="a",
        base_ref="main",
        head_ref="main",
        changed_files=[],
        patches={},
    )
    assert fetcher.fetch(pr) == []

from __future__ import annotations

import logging
from typing import Any

import httpx

from codereview.config import Settings
from codereview.models import Finding

logger = logging.getLogger(__name__)

"""Optional feedback loop helpers for review_finding_outcomes (migration 003)."""


def record_finding_outcome(
    *,
    repo: str,
    pr_number: int,
    finding: Finding,
    outcome: str,
    settings: Settings | None = None,
    commit_sha: str | None = None,
    notes: str | None = None,
) -> bool:
    """Insert one outcome row. Returns False if skipped or failed.

    Outcomes (resolved / dismissed / thumbs_up / thumbs_down) can tune
    ``posting.min_confidence`` over time. Fail-open without Supabase credentials.
    """
    if outcome not in {"resolved", "dismissed", "thumbs_up", "thumbs_down"}:
        raise ValueError(f"Invalid outcome: {outcome}")
    settings = settings or Settings()
    if not settings.supabase_url or not settings.supabase_key:
        logger.debug("Outcome not recorded — Supabase credentials missing")
        return False

    row: dict[str, Any] = {
        "repo": repo,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "finding_title": finding.title,
        "finding_file": finding.file,
        "finding_line": finding.line,
        "severity": finding.severity.value,
        "category": finding.category.value,
        "confidence": finding.confidence,
        "agent": finding.agent,
        "outcome": outcome,
        "notes": notes,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                f"{settings.supabase_url.rstrip('/')}/rest/v1/review_finding_outcomes",
                headers={
                    "apikey": settings.supabase_key,
                    "Authorization": f"Bearer {settings.supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=row,
            )
            if response.status_code in {200, 201}:
                return True
            logger.warning("Outcome insert failed: %s %s", response.status_code, response.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("Outcome insert failed: %s", exc)
    return False

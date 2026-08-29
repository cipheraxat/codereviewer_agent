from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from codereview.config import ConfluenceConfig, ExternalContextConfig, JiraConfig
from codereview.models import CodeSnippet, PullRequestContext

logger = logging.getLogger(__name__)

TICKET_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
CONFLUENCE_URL_PATTERN = re.compile(
    r"https?://[^/\s]+/wiki/(?:spaces/[^/]+/pages/|pages/viewpage\.action\?pageId=)(\d+)",
    re.IGNORECASE,
)


def extract_ticket_keys(pr: PullRequestContext) -> list[str]:
    text_parts = [pr.title, pr.body or "", pr.head_ref, pr.base_ref]
    keys: list[str] = []
    for part in text_parts:
        keys.extend(TICKET_KEY_PATTERN.findall(part))
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def extract_confluence_page_ids(pr: PullRequestContext) -> list[str]:
    if not pr.body:
        return []
    ids = CONFLUENCE_URL_PATTERN.findall(pr.body)
    for match in re.finditer(r"pageId=(\d+)", pr.body):
        ids.append(match.group(1))
    return list(dict.fromkeys(ids))


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class ExternalContextFetcher:
    """Fetch JIRA/Confluence context. Always fail-open."""

    def __init__(
        self,
        config: ExternalContextConfig,
        *,
        atlassian_email: Optional[str] = None,
        atlassian_api_token: Optional[str] = None,
        atlassian_domain: Optional[str] = None,
    ) -> None:
        self.config = config
        self.email = atlassian_email
        self.api_token = atlassian_api_token
        self.domain = (atlassian_domain or config.jira.base_url or "").replace("https://", "").rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.email and self.api_token and self.domain)

    def fetch(self, pr: PullRequestContext) -> list[CodeSnippet]:
        if not self.config.enabled:
            return []

        snippets: list[CodeSnippet] = []
        try:
            if self.config.jira.enabled:
                snippets.extend(self._fetch_jira(pr))
            if self.config.confluence.enabled:
                snippets.extend(self._fetch_confluence(pr))
        except Exception as exc:
            logger.warning("External context fetch failed (continuing without it): %s", exc)
        return snippets[: self.config.max_snippets]

    def _auth(self) -> tuple[str, str]:
        if not self.available:
            raise RuntimeError("Atlassian credentials not configured")
        return self.email or "", self.api_token or ""

    def _fetch_jira(self, pr: PullRequestContext) -> list[CodeSnippet]:
        if not self.available:
            logger.info("JIRA enabled but Atlassian credentials missing; skipping external context")
            return []

        keys = extract_ticket_keys(pr)
        if self.config.jira.projects:
            keys = [k for k in keys if any(k.startswith(f"{p}-") for p in self.config.jira.projects)]
        if not keys:
            return []

        snippets: list[CodeSnippet] = []
        for key in keys[: self.config.jira.max_issues]:
            issue = self._get_jira_issue(key)
            if issue:
                snippets.append(issue)
        return snippets

    def _get_jira_issue(self, key: str) -> Optional[CodeSnippet]:
        url = f"https://{self.domain}/rest/api/3/issue/{key}"
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(url, auth=self._auth())
                if response.status_code != 200:
                    logger.warning("JIRA %s returned %s", key, response.status_code)
                    return None
                data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("JIRA request failed for %s: %s", key, exc)
            return None

        fields = data.get("fields", {})
        summary = fields.get("summary", "")
        description = fields.get("description")
        desc_text = self._jira_description_to_text(description)
        acceptance = ""
        for field_key in self.config.jira.acceptance_fields:
            value = fields.get(field_key)
            if value:
                acceptance = self._jira_description_to_text(value) if isinstance(value, dict) else str(value)
                break

        body = f"Summary: {summary}\n\nDescription:\n{desc_text}"
        if acceptance:
            body += f"\n\nAcceptance criteria:\n{acceptance}"

        max_chars = self.config.max_chars_per_source
        if len(body) > max_chars:
            body = body[:max_chars] + "\n... [truncated]"

        return CodeSnippet(
            path=f"jira:{key}",
            content=body,
            score=10.0,
            reason="jira_ticket",
        )

    def _jira_description_to_text(self, description: object) -> str:
        if description is None:
            return ""
        if isinstance(description, str):
            return description
        if isinstance(description, dict):
            return self._adf_to_text(description)
        return str(description)

    def _adf_to_text(self, node: dict) -> str:
        """Best-effort Atlassian Document Format to plain text."""
        parts: list[str] = []
        node_type = node.get("type")
        if node_type == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            if isinstance(child, dict):
                parts.append(self._adf_to_text(child))
        if node_type in {"paragraph", "heading", "listItem"}:
            parts.append("\n")
        return "".join(parts)

    def _fetch_confluence(self, pr: PullRequestContext) -> list[CodeSnippet]:
        if not self.available:
            return []

        page_ids = extract_confluence_page_ids(pr)
        if not page_ids and self.config.confluence.follow_jira_remote_links:
            page_ids = self._confluence_ids_from_jira_links(pr)

        snippets: list[CodeSnippet] = []
        for page_id in page_ids[: self.config.confluence.max_pages]:
            page = self._get_confluence_page(page_id)
            if page:
                snippets.append(page)
        return snippets

    def _confluence_ids_from_jira_links(self, pr: PullRequestContext) -> list[str]:
        ids: list[str] = []
        for key in extract_ticket_keys(pr):
            url = f"https://{self.domain}/rest/api/3/issue/{key}/remotelink"
            try:
                with httpx.Client(timeout=15.0) as client:
                    response = client.get(url, auth=self._auth())
                    if response.status_code != 200:
                        continue
                    for link in response.json():
                        object_url = (link.get("object") or {}).get("url", "")
                        match = CONFLUENCE_URL_PATTERN.search(object_url)
                        if match:
                            ids.append(match.group(1))
            except httpx.HTTPError:
                continue
        return list(dict.fromkeys(ids))

    def _get_confluence_page(self, page_id: str) -> Optional[CodeSnippet]:
        url = f"https://{self.domain}/wiki/rest/api/content/{page_id}"
        params = {"expand": "body.storage,title"}
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(url, params=params, auth=self._auth())
                if response.status_code != 200:
                    return None
                data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Confluence page %s failed: %s", page_id, exc)
            return None

        title = data.get("title", page_id)
        html = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        text = _html_to_text(html)
        max_chars = self.config.max_chars_per_source
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"

        return CodeSnippet(
            path=f"confluence:{page_id}",
            content=f"Title: {title}\n\n{text}",
            score=9.5,
            reason="confluence_page",
        )


def repo_slug(pr: PullRequestContext) -> str:
    return f"{pr.owner}/{pr.repo}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

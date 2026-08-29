from __future__ import annotations

import json
import logging
from pathlib import Path

from codereview.models import CodeSnippet, KnowledgeDocument, PullRequestContext

logger = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "knowledge"


class MockKnowledgeProvider:
    """Load JIRA/Confluence documents from local JSON fixtures (no Atlassian API)."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.fixtures_dir = (fixtures_dir or DEFAULT_FIXTURES_DIR).resolve()

    def fetch_for_indexing(self) -> list[KnowledgeDocument]:
        documents: list[KnowledgeDocument] = []
        documents.extend(self._load_jira_fixtures())
        documents.extend(self._load_confluence_fixtures())
        return documents

    def fetch(self, pr: PullRequestContext) -> list[CodeSnippet]:
        """PR-linked snippets for legacy (non-unified) retrieval."""
        from codereview.external_context import extract_ticket_keys

        keys = {key.upper() for key in extract_ticket_keys(pr)}
        snippets: list[CodeSnippet] = []
        for doc in self.fetch_for_indexing():
            if doc.source == "jira":
                ticket_key = doc.path.removeprefix("jira:")
                if ticket_key.upper() in keys:
                    snippets.append(
                        CodeSnippet(path=doc.path, content=doc.content, score=10.0, reason="jira_ticket_mock")
                    )
            elif doc.source == "confluence":
                snippets.append(
                    CodeSnippet(path=doc.path, content=doc.content, score=9.5, reason="confluence_page_mock")
                )
        return snippets

    def _load_jira_fixtures(self) -> list[KnowledgeDocument]:
        jira_dir = self.fixtures_dir / "jira"
        if not jira_dir.exists():
            return []

        documents: list[KnowledgeDocument] = []
        for path in sorted(jira_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            key = data.get("key", path.stem)
            body = self._format_jira_body(data)
            documents.append(KnowledgeDocument(path=f"jira:{key}", content=body, source="jira"))
        return documents

    def _load_confluence_fixtures(self) -> list[KnowledgeDocument]:
        conf_dir = self.fixtures_dir / "confluence"
        if not conf_dir.exists():
            return []

        documents: list[KnowledgeDocument] = []
        for path in sorted(conf_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            page_id = data.get("page_id", path.stem)
            title = data.get("title", page_id)
            content = data.get("content", "")
            body = f"Title: {title}\n\n{content}"
            documents.append(KnowledgeDocument(path=f"confluence:{page_id}", content=body, source="confluence"))
        return documents

    @staticmethod
    def _format_jira_body(data: dict) -> str:
        summary = data.get("summary", "")
        description = data.get("description", "")
        acceptance = data.get("acceptance_criteria", "")
        body = f"Summary: {summary}\n\nDescription:\n{description}"
        if acceptance:
            body += f"\n\nAcceptance criteria:\n{acceptance}"
        return body

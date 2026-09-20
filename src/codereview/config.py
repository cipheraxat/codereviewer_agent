from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from codereview.models import Severity

logger = logging.getLogger(__name__)


class CustomRule(BaseModel):
    id: str
    description: str
    pattern: str
    severity: Severity = Severity.MEDIUM
    category: str = "quality"


class ContextConfig(BaseModel):
    max_snippets: int = 12
    max_snippet_chars: int = 2000
    neighbor_depth: int = 1
    min_vector_snippets_before_fallback: int = 2


class EnsembleConfig(BaseModel):
    llm_verify: bool = True
    # OCR-style fact-check: drop only findings the diff proves wrong.
    fact_check: bool = True


class ReviewHarnessConfig(BaseModel):
    """OCR-inspired deterministic harness around the LLM agents."""

    effort: Literal["low", "medium", "high"] = "medium"
    use_tools: bool = True
    max_files_per_bundle: int = 8
    max_bundles: int = 4
    max_diff_chars_per_file: int = 40_000
    # Empty → use `languages` list to enable built-in packs.
    rule_packs: list[str] = Field(default_factory=list)
    # When True, post only findings at/above severity_threshold with min_confidence.
    precision_mode: bool = True


class JiraConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    projects: list[str] = Field(default_factory=list)
    max_issues: int = 3
    max_index_issues: int = 100
    index_jql: str | None = None
    acceptance_fields: list[str] = Field(default_factory=lambda: ["customfield_10016", "customfield_10026"])


class ConfluenceConfig(BaseModel):
    enabled: bool = False
    spaces: list[str] = Field(default_factory=list)
    max_pages: int = 2
    max_index_pages: int = 50
    follow_jira_remote_links: bool = True


class ExternalContextConfig(BaseModel):
    enabled: bool = False
    max_snippets: int = 4
    max_chars_per_source: int = 3000
    jira: JiraConfig = Field(default_factory=JiraConfig)
    confluence: ConfluenceConfig = Field(default_factory=ConfluenceConfig)


class SupabaseConfig(BaseModel):
    enabled: bool = False
    table: str = "code_embeddings"
    max_chunk_chars: int = 1500
    chunk_overlap: int = 200
    index_on_review: bool = False
    match_threshold: float = 0.55
    vector_top_k: int = 12


class IndexingConfig(BaseModel):
    # Atlassian only — repo code is never embedded (OCR harness uses checkout + tools).
    sources: list[str] = Field(default_factory=lambda: ["jira", "confluence"])
    # Retained for backwards-compatible YAML; unused now that code indexing is disabled.
    code_globs: list[str] = Field(
        default_factory=lambda: [
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.go",
            "**/*.java",
            "**/*.rb",
            "**/*.rs",
            "**/*.md",
            "**/*.yaml",
            "**/*.yml",
        ]
    )


class VectorConfig(BaseModel):
    enabled: bool = False
    unified_rag: bool = True
    embedding_model: str = "openai/text-embedding-3-small"
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)


class PostingConfig(BaseModel):
    # Precision-first default (OCR stance): fewer, higher-confidence comments.
    min_confidence: float = 0.70
    max_inline_comments: int = 25


class ReviewerConfig(BaseModel):
    languages: list[str] = Field(default_factory=lambda: ["python", "typescript", "javascript"])
    ignore_globs: list[str] = Field(default_factory=list)
    severity_threshold: Severity = Severity.MEDIUM
    custom_rules: list[CustomRule] = Field(default_factory=list)
    team_conventions: str = ""
    context: ContextConfig = Field(default_factory=ContextConfig)
    external_context: ExternalContextConfig = Field(default_factory=ExternalContextConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    ensemble: EnsembleConfig = Field(default_factory=EnsembleConfig)
    posting: PostingConfig = Field(default_factory=PostingConfig)
    review: ReviewHarnessConfig = Field(default_factory=ReviewHarnessConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> ReviewerConfig:
        if path is None:
            path = Path("reviewer.yaml")
        if not path.exists():
            example = Path("reviewer.example.yaml")
            if example.exists():
                logger.warning(
                    "No %s found; falling back to %s. Copy it to reviewer.yaml and customize "
                    "so example rules are not applied to your repo unintentionally.",
                    path,
                    example,
                )
                path = example
            else:
                return cls()
        data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_provider: str = Field(default="openrouter", alias="LLM_PROVIDER")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")
    embedding_model_override: str | None = Field(default=None, alias="EMBEDDING_MODEL")
    embedding_api_key: str | None = Field(default=None, alias="EMBEDDING_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="OPENROUTER_BASE_URL",
    )
    openrouter_site_url: str | None = Field(default=None, alias="OPENROUTER_SITE_URL")
    openrouter_app_name: str = Field(default="codereview-agent", alias="OPENROUTER_APP_NAME")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    atlassian_email: str | None = Field(default=None, alias="ATLASSIAN_EMAIL")
    atlassian_api_token: str | None = Field(default=None, alias="ATLASSIAN_API_TOKEN")
    atlassian_domain: str | None = Field(default=None, alias="ATLASSIAN_DOMAIN")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")

    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        provider = self.llm_provider.lower()
        if provider == "anthropic":
            return "claude-sonnet-4-20250514"
        if provider == "openrouter":
            return "openai/gpt-4o-mini"
        return "gpt-4o-mini"

    def resolved_embedding_model(self) -> str:
        if self.embedding_model_override:
            return self.embedding_model_override
        if self.llm_provider.lower() == "openrouter":
            return "openai/text-embedding-3-small"
        return "text-embedding-3-small"

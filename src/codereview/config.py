from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from codereview.models import Severity


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


class PostingConfig(BaseModel):
    min_confidence: float = 0.55
    max_inline_comments: int = 25


class ReviewerConfig(BaseModel):
    languages: list[str] = Field(default_factory=lambda: ["python", "typescript", "javascript"])
    ignore_globs: list[str] = Field(default_factory=list)
    severity_threshold: Severity = Severity.MEDIUM
    custom_rules: list[CustomRule] = Field(default_factory=list)
    team_conventions: str = ""
    context: ContextConfig = Field(default_factory=ContextConfig)
    posting: PostingConfig = Field(default_factory=PostingConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> ReviewerConfig:
        if path is None:
            path = Path("reviewer.yaml")
        if not path.exists():
            example = Path("reviewer.example.yaml")
            if example.exists():
                path = example
            else:
                return cls()
        data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    dry_run: bool = Field(default=False, alias="DRY_RUN")

    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        if self.llm_provider == "anthropic":
            return "claude-sonnet-4-20250514"
        return "gpt-4o-mini"

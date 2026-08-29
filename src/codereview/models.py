from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class FindingCategory(str, Enum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    PERFORMANCE = "performance"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class Finding(BaseModel):
    category: FindingCategory
    severity: Severity
    title: str
    file: Optional[str] = None
    line: Optional[int] = None
    rationale: str
    suggestion: str
    confidence: float = Field(ge=0.0, le=1.0)
    agent: str = "unknown"
    rule_id: Optional[str] = None

    def location_key(self) -> tuple[str, Optional[str], Optional[int]]:
        return (self.category.value, self.file, self.line)

    def dedupe_key(self) -> tuple[str, Optional[str], Optional[int]]:
        return (self.title.lower().strip(), self.file, self.line)


class CodeSnippet(BaseModel):
    path: str
    content: str
    score: float = 0.0
    reason: str = ""


class KnowledgeDocument(BaseModel):
    """A document to embed into the unified knowledge index."""

    path: str
    content: str
    source: str = "code"  # code | jira | confluence


class PullRequestContext(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: Optional[str] = None
    head_sha: str
    base_ref: str
    head_ref: str
    changed_files: list[str] = Field(default_factory=list)
    patches: dict[str, str] = Field(default_factory=dict)


class ReviewMetrics(BaseModel):
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_degraded: bool = False


class ReviewReport(BaseModel):
    pr: Optional[PullRequestContext] = None
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verdict: str = "comment"  # approve | request_changes | comment
    metrics: ReviewMetrics = Field(default_factory=ReviewMetrics)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    commit_sha: Optional[str] = None

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def findings_above_threshold(self, min_severity: Severity) -> list[Finding]:
        threshold = SEVERITY_ORDER[min_severity]
        return [f for f in self.findings if SEVERITY_ORDER[f.severity] >= threshold]

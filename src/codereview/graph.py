from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from codereview.agents import EnsembleAgent, PatternAgent, SecurityAgent
from codereview.config import ReviewerConfig, Settings
from codereview.context_engine import ContextEngine
from codereview.llm import LLMClient
from codereview.models import Finding, PullRequestContext, ReviewMetrics, ReviewReport


class ReviewState(TypedDict):
    pr: PullRequestContext
    config: ReviewerConfig
    repo_root: str
    context_block: str
    security_findings: list[Finding]
    pattern_findings: list[Finding]
    all_findings: list[Finding]
    report: ReviewReport | None
    llm_input_tokens: int
    llm_output_tokens: int
    llm_cost_usd: float
    llm_provider: str | None
    llm_model: str | None


class ReviewOrchestrator:
    def __init__(
        self,
        repo_root: Path,
        config: ReviewerConfig,
        settings: Settings | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.settings = settings or Settings()
        self.llm = LLMClient(self.settings)
        self.context_engine = ContextEngine(repo_root, config)
        self.security_agent = SecurityAgent()
        self.pattern_agent = PatternAgent()
        self.ensemble_agent = EnsembleAgent()
        self.graph = self._build_graph()

    def run(self, pr: PullRequestContext) -> ReviewReport:
        start = time.perf_counter()
        final_state = self.graph.invoke(
            {
                "pr": pr,
                "config": self.config,
                "repo_root": str(self.repo_root),
                "context_block": "",
                "security_findings": [],
                "pattern_findings": [],
                "all_findings": [],
                "report": None,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "llm_cost_usd": 0.0,
                "llm_provider": self.settings.llm_provider if self.llm.available else None,
                "llm_model": self.settings.resolved_model() if self.llm.available else None,
            }
        )
        report = final_state["report"]
        if report is None:
            raise RuntimeError("Review graph did not produce a report")
        report.metrics.latency_ms = int((time.perf_counter() - start) * 1000)
        return report

    def _build_graph(self):
        graph = StateGraph(ReviewState)

        graph.add_node("build_context", self._build_context)
        graph.add_node("security_review", self._security_review)
        graph.add_node("pattern_review", self._pattern_review)
        graph.add_node("ensemble", self._ensemble)

        graph.add_edge(START, "build_context")
        graph.add_edge("build_context", "security_review")
        graph.add_edge("build_context", "pattern_review")
        graph.add_edge("security_review", "ensemble")
        graph.add_edge("pattern_review", "ensemble")
        graph.add_edge("ensemble", END)

        return graph.compile()

    def _build_context(self, state: ReviewState) -> dict:
        snippets = self.context_engine.build_context(state["pr"])
        context_block = self.context_engine.format_context_block(snippets)
        return {"context_block": context_block}

    def _security_review(self, state: ReviewState) -> dict:
        findings = self.security_agent.review(
            state["pr"],
            state["context_block"],
            state["config"],
            self.llm if self.llm.available else None,
        )
        return {"security_findings": findings}

    def _pattern_review(self, state: ReviewState) -> dict:
        findings = self.pattern_agent.review(
            state["pr"],
            state["context_block"],
            state["config"],
            self.llm if self.llm.available else None,
        )
        return {"pattern_findings": findings}

    def _ensemble(self, state: ReviewState) -> dict:
        combined = state["security_findings"] + state["pattern_findings"]
        findings, summary, confidence, verdict = self.ensemble_agent.aggregate(
            combined,
            state["config"],
            state["pr"],
            self.llm if self.llm.available else None,
        )
        report = ReviewReport(
            pr=state["pr"],
            findings=findings,
            summary=summary,
            overall_confidence=confidence,
            verdict=verdict,
            commit_sha=state["pr"].head_sha,
            metrics=ReviewMetrics(
                input_tokens=self.llm.input_tokens,
                output_tokens=self.llm.output_tokens,
                estimated_cost_usd=self.llm.estimate_cost_usd(),
                llm_provider=state["llm_provider"],
                llm_model=state["llm_model"],
            ),
        )
        return {"all_findings": findings, "report": report}

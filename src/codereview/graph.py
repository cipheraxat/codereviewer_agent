from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from codereview.agents import EnsembleAgent, PatternAgent, SecurityAgent
from codereview.config import ReviewerConfig, Settings
from codereview.context_engine import ContextEngine
from codereview.file_selection import (
    SelectionResult,
    filter_pr_context,
    format_bundle_summary,
    pr_from_bundle,
)
from codereview.llm import LLMClient
from codereview.models import Finding, PullRequestContext, ReviewMetrics, ReviewReport
from codereview.review_tools import enrich_context_with_tools


class ReviewState(TypedDict):
    pr: PullRequestContext
    config: ReviewerConfig
    repo_root: str
    context_block: str
    bundle_summary: str
    selection: SelectionResult | None
    security_findings: list[Finding]
    pattern_findings: list[Finding]
    all_findings: list[Finding]
    report: ReviewReport | None
    llm_provider: str | None
    llm_model: str | None


class ReviewOrchestrator:
    def __init__(
        self,
        repo_root: Path,
        config: ReviewerConfig,
        settings: Settings | None = None,
        *,
        context_engine: ContextEngine | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self.settings = settings or Settings()
        self.llm = LLMClient(self.settings)
        self.context_engine = context_engine or ContextEngine(repo_root, config, settings=self.settings)
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
                "bundle_summary": "",
                "selection": None,
                "security_findings": [],
                "pattern_findings": [],
                "all_findings": [],
                "report": None,
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

        graph.add_node("select_and_bundle", self._select_and_bundle)
        graph.add_node("build_context", self._build_context)
        graph.add_node("security_review", self._security_review)
        graph.add_node("pattern_review", self._pattern_review)
        graph.add_node("ensemble", self._ensemble)

        graph.add_edge(START, "select_and_bundle")
        graph.add_edge("select_and_bundle", "build_context")
        graph.add_edge("build_context", "security_review")
        graph.add_edge("build_context", "pattern_review")
        graph.add_edge("security_review", "ensemble")
        graph.add_edge("pattern_review", "ensemble")
        graph.add_edge("ensemble", END)

        return graph.compile()

    def _select_and_bundle(self, state: ReviewState) -> dict:
        filtered_pr, selection = filter_pr_context(state["pr"], state["config"])
        return {
            "pr": filtered_pr,
            "selection": selection,
            "bundle_summary": format_bundle_summary(selection),
        }

    def _build_context(self, state: ReviewState) -> dict:
        snippets = self.context_engine.build_context(state["pr"])
        context_block = self.context_engine.format_context_block(snippets)
        if state.get("bundle_summary"):
            context_block = f"{state['bundle_summary']}\n\n{context_block}"
        # Tool enrichment only when an LLM can consume it.
        if state["config"].review.use_tools and self.llm.available:
            tool_ctx = enrich_context_with_tools(
                Path(state["repo_root"]),
                state["pr"],
                ignore_globs=state["config"].ignore_globs,
            )
            if tool_ctx:
                context_block = f"{context_block}\n\n{tool_ctx}"
        return {"context_block": context_block}

    def _bundle_context(self, full_context: str, *, bundle_index: int) -> str:
        """Full context on first bundle; subsequent bundles get a slim summary only."""
        if bundle_index == 0:
            return full_context
        # Drop tool: blocks and large code fences to avoid re-sending identical RAG/tool context.
        lines: list[str] = []
        skip = False
        for line in full_context.splitlines():
            if line.startswith("### tool:"):
                skip = True
                continue
            if skip:
                if line.startswith("### ") and not line.startswith("### tool:"):
                    skip = False
                else:
                    continue
            lines.append(line)
        slim = "\n".join(lines)
        # Keep selection summary + short lead-in only.
        if len(slim) > 2500:
            slim = slim[:2500] + "\n... [shared context omitted for subsequent bundles]"
        return slim

    def _review_bundles(self, state: ReviewState, agent) -> list[Finding]:
        selection = state.get("selection")
        bundles = selection.bundles if selection and selection.bundles else []
        llm = self.llm if self.llm.available else None
        if not bundles:
            return agent.review(state["pr"], state["context_block"], state["config"], llm)

        findings: list[Finding] = []
        for index, bundle in enumerate(bundles):
            bundle_pr = pr_from_bundle(state["pr"], bundle)
            context = self._bundle_context(state["context_block"], bundle_index=index)
            findings.extend(agent.review(bundle_pr, context, state["config"], llm))
        return findings

    def _security_review(self, state: ReviewState) -> dict:
        return {"security_findings": self._review_bundles(state, self.security_agent)}

    def _pattern_review(self, state: ReviewState) -> dict:
        return {"pattern_findings": self._review_bundles(state, self.pattern_agent)}

    def _ensemble(self, state: ReviewState) -> dict:
        combined = state["security_findings"] + state["pattern_findings"]
        findings, summary, confidence, verdict, llm_degraded = self.ensemble_agent.aggregate(
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
                llm_degraded=llm_degraded,
            ),
        )
        return {"all_findings": findings, "report": report}

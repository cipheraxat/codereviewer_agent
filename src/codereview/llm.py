from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from codereview.config import Settings
from codereview.models import Finding, FindingCategory, Severity

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096

# Rough $/1M token rates for estimate_cost_usd (labeled as estimate in reports).
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-sonnet-latest": (3.0, 15.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "openai/gpt-4o-mini": (0.15, 0.6),
    "openai/gpt-4o": (2.5, 10.0),
}


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.llm_provider.lower()
        self.model = settings.resolved_model()
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("LLM_API_KEY is not set")

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if self.provider == "anthropic":
                    return self._anthropic_json(system, user)
                if self.provider == "openrouter":
                    return self._openrouter_json(system, user)
                return self._openai_json(system, user)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    logger.warning("LLM request failed (attempt %s/3): %s", attempt + 1, exc)
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"LLM request failed after retries: {last_error}") from last_error

    def _anthropic_json(self, system: str, user: str) -> dict[str, Any]:
        import anthropic

        client = anthropic.Anthropic(api_key=self.settings.llm_api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        text = "".join(block.text for block in response.content if block.type == "text")
        return self._parse_json(text)

    def _openai_json(self, system: str, user: str) -> dict[str, Any]:
        from openai import OpenAI

        client = OpenAI(api_key=self.settings.llm_api_key)
        return self._chat_json(client, system, user)

    def _openrouter_json(self, system: str, user: str) -> dict[str, Any]:
        from openai import OpenAI

        extra_headers: dict[str, str] = {"X-Title": self.settings.openrouter_app_name}
        if self.settings.openrouter_site_url:
            extra_headers["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.openrouter_base_url,
            default_headers=extra_headers,
        )
        return self._chat_json(client, system, user)

    def _chat_json(self, client: Any, system: str, user: str) -> dict[str, Any]:
        response = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = response.usage
        if usage:
            self.input_tokens += usage.prompt_tokens
            self.output_tokens += usage.completion_tokens
        text = response.choices[0].message.content or "{}"
        return self._parse_json(text)

    def _parse_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        return json.loads(text)

    def estimate_cost_usd(self) -> float:
        """Rough cost estimate — label as estimate in posted reviews."""
        rates = _MODEL_RATES.get(self.model)
        if rates is None:
            if self.provider == "anthropic":
                rates = (3.0, 15.0)
            else:
                rates = (0.15, 0.6)
        input_rate, output_rate = rates
        return (self.input_tokens * input_rate + self.output_tokens * output_rate) / 1_000_000


def findings_from_payload(payload: dict[str, Any], agent: str) -> list[Finding]:
    findings: list[Finding] = []
    skipped = 0
    for item in payload.get("findings", []):
        try:
            evidence = item.get("evidence_snippet") or item.get("existing_code")
            findings.append(
                Finding(
                    category=FindingCategory(item.get("category", "quality")),
                    severity=Severity(item.get("severity", "medium")),
                    title=item["title"],
                    file=item.get("file"),
                    line=item.get("line"),
                    rationale=item.get("rationale", ""),
                    suggestion=item.get("suggestion", ""),
                    confidence=float(item.get("confidence", 0.6)),
                    agent=agent,
                    rule_id=item.get("rule_id"),
                    evidence_snippet=evidence,
                )
            )
        except (KeyError, ValueError) as exc:
            skipped += 1
            logger.debug("Skipped malformed LLM finding from %s: %s", agent, exc)
    if skipped:
        logger.warning("Skipped %s malformed finding(s) from %s agent", skipped, agent)
    return findings

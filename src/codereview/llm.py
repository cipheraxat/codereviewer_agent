from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from codereview.config import Settings
from codereview.models import Finding, FindingCategory, Severity

logger = logging.getLogger(__name__)


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
            max_tokens=4096,
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
        if self.provider == "anthropic":
            return (self.input_tokens * 3.0 + self.output_tokens * 15.0) / 1_000_000
        if self.provider == "openrouter":
            # Rough blended estimate; actual cost depends on routed model.
            return (self.input_tokens * 0.15 + self.output_tokens * 0.6) / 1_000_000
        return (self.input_tokens * 0.15 + self.output_tokens * 0.6) / 1_000_000


def findings_from_payload(payload: dict[str, Any], agent: str) -> list[Finding]:
    findings: list[Finding] = []
    skipped = 0
    for item in payload.get("findings", []):
        try:
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
                )
            )
        except (KeyError, ValueError) as exc:
            skipped += 1
            logger.debug("Skipped malformed LLM finding from %s: %s", agent, exc)
    if skipped:
        logger.warning("Skipped %s malformed finding(s) from %s agent", skipped, agent)
    return findings

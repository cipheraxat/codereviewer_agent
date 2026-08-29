from __future__ import annotations

import logging
from typing import Optional

import httpx

from codereview.config import Settings

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1536


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.resolved_embedding_model()

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.available:
            raise RuntimeError("LLM_API_KEY is required for embeddings")

        provider = self.settings.llm_provider.lower()
        if provider == "anthropic":
            return self._embed_openai_compatible(texts, base_url=None)
        if provider == "openrouter":
            return self._embed_openai_compatible(texts, base_url=self.settings.openrouter_base_url)
        return self._embed_openai_compatible(texts, base_url=None)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def _embed_openai_compatible(self, texts: list[str], base_url: Optional[str]) -> list[list[float]]:
        from openai import OpenAI

        kwargs: dict = {"api_key": self.settings.llm_api_key}
        if base_url:
            kwargs["base_url"] = base_url
            kwargs["default_headers"] = {"X-Title": self.settings.openrouter_app_name}
            if self.settings.openrouter_site_url:
                kwargs["default_headers"]["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(**kwargs)
        response = client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in response.data]

from __future__ import annotations

import logging

from codereview.config import Settings

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1536


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.resolved_embedding_model()

    @property
    def _embedding_key(self) -> str | None:
        return self.settings.embedding_api_key or self.settings.llm_api_key

    @property
    def available(self) -> bool:
        if not self._embedding_key:
            return False
        # Anthropic has no embeddings API — require a dedicated OpenAI-compatible key.
        if self.settings.llm_provider.lower() == "anthropic" and not self.settings.embedding_api_key:
            return False
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.settings.llm_provider.lower() == "anthropic" and not self.settings.embedding_api_key:
            raise RuntimeError(
                "Anthropic has no embeddings API. Set EMBEDDING_API_KEY to an OpenAI-compatible key "
                "(and optionally EMBEDDING_MODEL), or set LLM_PROVIDER=openrouter|openai."
            )
        if not self.available:
            raise RuntimeError("LLM_API_KEY or EMBEDDING_API_KEY is required for embeddings")

        base_url = self._resolve_base_url()
        return self._embed_openai_compatible(texts, base_url=base_url, api_key=self._embedding_key)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def _resolve_base_url(self) -> str | None:
        # Dedicated embedding key → OpenAI-compatible default endpoint.
        if self.settings.embedding_api_key:
            return None
        if self.settings.llm_provider.lower() == "openrouter":
            return self.settings.openrouter_base_url
        return None

    def _embed_openai_compatible(
        self,
        texts: list[str],
        *,
        base_url: str | None,
        api_key: str | None,
    ) -> list[list[float]]:
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
            kwargs["default_headers"] = {"X-Title": self.settings.openrouter_app_name}
            if self.settings.openrouter_site_url:
                kwargs["default_headers"]["HTTP-Referer"] = self.settings.openrouter_site_url

        client = OpenAI(**kwargs)
        response = client.embeddings.create(model=self.model, input=texts)
        vectors = [item.embedding for item in response.data]
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"Embedding dimension {len(vector)} != {EMBEDDING_DIM} required by "
                    f"code_embeddings schema. Use text-embedding-3-small (or another 1536-dim model), "
                    f"not {self.model}."
                )
        return vectors

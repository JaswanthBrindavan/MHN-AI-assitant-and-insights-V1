"""LLM provider abstraction. Nothing here is required by the test suite."""

from __future__ import annotations

from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.fake import FakeProvider


def get_provider() -> LLMProvider:
    """Select the configured provider. Defaults to the deterministic fake."""
    settings = get_settings()
    if settings.llm_provider == "ollama":
        # Imported lazily so tests never require httpx/network.
        from app.llm.ollama import OllamaProvider

        return OllamaProvider(
            base_url=settings.ollama_base_url, model=settings.ollama_model
        )
    return FakeProvider()


__all__ = ["LLMProvider", "FakeProvider", "get_provider"]

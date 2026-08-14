"""LLM provider abstraction — model- and cloud-agnostic.

Nothing here is required by the test suite (tests use FakeProvider).
"""

from __future__ import annotations

from app.config import get_settings
from app.llm.base import LLMProvider
from app.llm.fake import FakeProvider


def get_provider() -> LLMProvider:
    """Select the configured provider. Defaults to the deterministic fake.

    LLM_PROVIDER: fake | openai_compatible | anthropic | ollama
    (``ollama`` is an alias of openai_compatible using the OLLAMA_* settings.)
    """
    settings = get_settings()
    kind = settings.llm_provider

    if kind in ("openai_compatible", "openai"):
        from app.llm.providers import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
    if kind == "anthropic":
        from app.llm.providers import AnthropicProvider

        return AnthropicProvider(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or "https://api.anthropic.com",
        )
    if kind == "ollama":
        from app.llm.providers import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.ollama_base_url, model=settings.ollama_model
        )
    return FakeProvider()


__all__ = ["LLMProvider", "FakeProvider", "get_provider"]

"""LLM provider abstraction — model- and cloud-agnostic.

``LLM_PROVIDER`` selects the adapter:

  fake              deterministic, for tests and offline dev
  anthropic         Anthropic Messages API (official SDK)
  openai_compatible any OpenAI /chat/completions endpoint — OpenAI, Groq,
                    Together, vLLM, LM Studio, llama.cpp, self-hosted
  ollama            legacy alias of openai_compatible using the OLLAMA_* env

Every adapter satisfies ToolCallingProvider, so the agent loop is identical
whichever one is live. Nothing in the test suite needs a network or a key.
"""

from __future__ import annotations

from app.config import get_settings
from app.llm.base import LLMProvider, ToolCallingProvider
from app.llm.fake import FakeProvider


def get_provider() -> ToolCallingProvider:
    """Select the configured provider. Defaults to the deterministic fake."""
    settings = get_settings()
    kind = settings.llm_provider

    if kind == "anthropic":
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
            thinking=settings.llm_thinking,
        )
    if kind in ("openai_compatible", "openai"):
        from app.llm.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
    if kind == "ollama":
        from app.llm.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.ollama_base_url, model=settings.ollama_model
        )
    return FakeProvider()


__all__ = ["FakeProvider", "LLMProvider", "ToolCallingProvider", "get_provider"]

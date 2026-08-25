"""LLM provider protocols."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.llm.tools import LLMTurn, Message, ToolSpec


@runtime_checkable
class LLMProvider(Protocol):
    """A minimal chat-completion provider.

    Implementations must be side-effect free apart from the network call and
    must never raise for ordinary completions (callers treat exceptions as a
    guardrail failure and degrade to a safe reply).
    """

    model_name: str

    async def generate(self, *, system: str | Sequence[str], user: str) -> str: ...


@runtime_checkable
class ToolCallingProvider(LLMProvider, Protocol):
    """An :class:`LLMProvider` that can also be offered tools.

    Inherits ``model_name`` and ``generate`` — a tool-calling provider is a
    superset, so nothing annotated ``LLMProvider`` needs re-annotating.

    An isinstance check against this is no stronger than
    ``hasattr(p, "generate_turn")``: a runtime_checkable Protocol verifies
    method presence, never signatures. Nothing checks it today.
    """

    async def generate_turn(
        self,
        *,
        system: str | Sequence[str],
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMTurn: ...

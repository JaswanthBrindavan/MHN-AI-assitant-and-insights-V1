"""Deterministic fake provider for tests and offline dev.

``responses`` scripts the text path; ``turns`` scripts the tool-calling path.
Both fall back to :attr:`FakeProvider.DEFAULT` once exhausted, so an
under-scripted test degrades instead of raising IndexError. ``raises`` records
the call and then fails, for provider-outage tests.

``calls`` is ONE log for both paths: ``generate`` appends
``{"system", "user"}``; ``generate_turn`` appends
``{"system", "messages", "tools"}``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.llm.tools import LLMTurn, Message, ToolSpec


class FakeProvider:
    model_name = "fake"

    # A default that carries no clinical numbers (so grounding passes trivially)
    # and no diagnostic phrasing (so validation passes). It deliberately does
    # NOT contain the word "clinician": that is how the outage evals and the
    # orchestrator tests tell a degraded safe_reply apart from a model answer.
    DEFAULT = (
        "Thanks for your question. In general, steady habits — a balanced diet, "
        "regular activity, and routine check-ups — support wellbeing. For "
        "anything specific to you, it's best to talk with your doctor."
    )

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        turns: Sequence[LLMTurn] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._turns = list(turns or [])
        # Exception, not BaseException: the orchestrator's fail-open path
        # catches `except Exception`, so a BaseException would escape it and
        # the outage tests would stop exercising the degrade.
        self._raises = raises
        self.calls: list[dict] = []

    async def generate(self, *, system: str, user: str) -> str:
        # Record BEFORE raising: `provider.calls == []` must keep meaning
        # "the model was never reached", so an outage still records that it was.
        self.calls.append({"system": system, "user": user})
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return self.DEFAULT

    async def generate_turn(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> LLMTurn:
        self.calls.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": [t.name for t in tools],
            }
        )
        if self._raises is not None:
            raise self._raises
        if self._turns:
            return self._turns.pop(0)
        return LLMTurn(text=self.DEFAULT, stop_reason="end_turn")

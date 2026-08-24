"""TEMP review probe — delete."""
from __future__ import annotations

import uuid

import pytest

from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_hallucinated_value_echo(db_session):
    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="check_value_against_range",
                        arguments={"metric": "blood sugar", "value": 250},
                    ),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(
                text=(
                    "A blood sugar of 250 mg/dL is above the typical range "
                    "(70-140 mg/dL). This is not a diagnosis - a single reading "
                    "can't confirm any condition. Please consult your doctor, who "
                    "can confirm the reading and advise on next steps. [P]"
                )
            ),
        ]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "my sugar felt high this morning", provider
    )
    print("PATH:", result.provenance)
    print("RISK:", result.risk_level)
    print("REPLY:", result.response_message)
    print("TRACE:", [s for s in (result.trace or [])])
    assert False, "dump"

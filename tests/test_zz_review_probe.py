"""TEMPORARY review probe — delete after the review."""
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


async def test_probe_parallel_tools_on_one_session(db_session, caplog):
    """TWO tool calls in one turn — the real executor, one AsyncSession."""
    provider = FakeProvider(turns=[
        LLMTurn(
            tool_calls=(
                ToolCall(id="c1", name="get_latest_metric",
                         arguments={"metric": "blood_pressure"}),
                ToolCall(id="c2", name="get_family_members", arguments={}),
            ),
            stop_reason="tool_use",
        ),
        LLMTurn(text="Here is a combined answer with no numbers in it."),
    ])
    with caplog.at_level("WARNING"):
        result = await handle_chat(
            db_session, uuid.uuid4(), "how am I doing overall?", provider
        )
    print("\nPROBE-PARALLEL provenance:", result.provenance)
    print("PROBE-PARALLEL reply:", result.response_message[:120])
    print("PROBE-PARALLEL warnings:", [r.message for r in caplog.records][:6])


async def test_probe_language_directive(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="General guidance about sleep.")])
    # Telugu, no translator sidecar configured.
    await handle_chat(db_session, uuid.uuid4(), "నాకు నిద్ర పట్టడం లేదు ఎందుకు", provider)
    sys_prompt = provider.calls[0]["system"]
    tail = sys_prompt[-400:]
    print("\nPROBE-LANG tail:", tail)
    assert "Reply in" in sys_prompt


async def test_probe_value_laundering(db_session):
    """The model invents 250 mg/dL, feeds it to the tool, quotes it back."""
    provider = FakeProvider(turns=[
        LLMTurn(
            tool_calls=(ToolCall(id="c1", name="check_value_against_range",
                                 arguments={"metric": "blood sugar", "value": 250}),),
            stop_reason="tool_use",
        ),
        LLMTurn(text="A blood sugar of 250 mg/dL is above the typical range "
                     "(70-140 mg/dL). Please consult your doctor to review it."),
    ])
    result = await handle_chat(
        db_session, uuid.uuid4(), "my sugar felt high this morning", provider
    )
    print("\nPROBE-LAUNDER reply:", result.response_message)
    print("PROBE-LAUNDER provenance:", result.provenance)
    print("PROBE-LAUNDER action:", result.recommended_action)


async def test_probe_action_downgrade(db_session):
    """A danger-severity reading: legacy returns seek_care_promptly."""
    provider = FakeProvider(turns=[
        LLMTurn(
            tool_calls=(ToolCall(id="c1", name="check_value_against_range",
                                 arguments={"metric": "blood pressure",
                                            "value": 195, "secondary": 125}),),
            stop_reason="tool_use",
        ),
        LLMTurn(text="A blood pressure of 195/125 mmHg is well above the "
                     "typical range. Please seek medical care promptly."),
    ])
    result = await handle_chat(
        db_session, uuid.uuid4(), "my bp reading came out 195 over 125", provider
    )
    print("\nPROBE-ACTION agentic action:", result.recommended_action)
    print("PROBE-ACTION reply:", result.response_message[:160])
    print("PROBE-ACTION risk:", result.risk_level)


async def test_probe_action_downgrade_legacy(db_session, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CHAT_ENGINE", "legacy")
    get_settings.cache_clear()
    provider = FakeProvider(turns=[LLMTurn(text="x")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "my bp reading came out 195 over 125", provider
    )
    print("\nPROBE-ACTION legacy action:", result.recommended_action)
    print("PROBE-ACTION legacy reply:", result.response_message[:160])

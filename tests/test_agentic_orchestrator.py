"""The agentic engine must keep every safety invariant the legacy one has.

The ordering guarantee is the whole point: the triage floor, the scope guard,
the emergency directive and the canned conversational replies all run BEFORE
the engine branch, so the model can never be the arbiter of an emergency. The
tests that assert `provider.calls == []` are the ones enforcing that — they say
"the model was never even asked".
"""

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


@pytest.fixture
async def user_with_hba1c(db_session):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(
        Report(
            id=902,
            user_id=user_id,
            filepath="reports/x",
            private=False,
            content={
                "ai": {
                    "classification": {"section": "reports", "title": "Lab"},
                    "extraction": {
                        "results": [
                            {
                                "test_name": "HbA1c",
                                "value": "6.1",
                                "unit": "%",
                                "value_numeric": 6.1,
                                "abnormal_flag": "high",
                            }
                        ]
                    },
                }
            },
        )
    )
    await db_session.flush()
    return user_id


def _tool_then_say(tool: str, arguments: dict, reply: str) -> FakeProvider:
    return FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(ToolCall(id="c1", name=tool, arguments=arguments),),
                stop_reason="tool_use",
            ),
            LLMTurn(text=reply),
        ]
    )


# --------------------------------------------------------------------------- #
# The safety floor runs BEFORE the engine
# --------------------------------------------------------------------------- #
async def test_emergency_never_reaches_the_model(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="should never be used")])
    result = await handle_chat(db_session, uuid.uuid4(), "I can't breathe", provider)
    assert result.risk_level == "emergency"
    assert result.recommended_action == "call_emergency_services"
    assert provider.calls == [], "the model was consulted about an emergency"


async def test_self_harm_returns_the_helpline_without_the_model(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="ignored")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "I want to hurt myself", provider
    )
    assert "14416" in result.response_message
    assert provider.calls == []


async def test_acs_cooccurrence_still_escalates(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="ignored")])
    result = await handle_chat(
        db_session,
        uuid.uuid4(),
        "chest pain and my left arm hurts with sweating",
        provider,
    )
    assert result.risk_level == "emergency"
    assert provider.calls == []


async def test_off_topic_is_declined_without_the_model(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="ignored")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "write me a python function to sort a list", provider
    )
    assert result.recommended_action == "out_of_scope"
    assert provider.calls == []


async def test_greeting_is_canned_without_the_model(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="ignored")])
    result = await handle_chat(db_session, uuid.uuid4(), "hello!", provider)
    assert result.provenance["path"] == "conversational"
    assert provider.calls == []


async def test_no_tools_are_offered_at_high_risk(db_session):
    """A red flag stays on the safe path — nothing may delay an escalation."""
    provider = FakeProvider(turns=[LLMTurn(text="Please seek medical care promptly.")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "I have severe chest pain", provider
    )
    assert result.risk_level == "high"
    assert provider.calls[0]["tools"] == []


# --------------------------------------------------------------------------- #
# The capability the legacy engine structurally cannot provide
# --------------------------------------------------------------------------- #
async def test_the_model_can_reach_the_readers_own_data(db_session, user_with_hba1c):
    provider = _tool_then_say(
        "get_report_parameter",
        {"parameter": "HbA1c"},
        "Your most recent HbA1c was 6.1%, which the report flags as above the "
        "usual range. Worth discussing with your doctor.",
    )
    result = await handle_chat(
        db_session,
        user_with_hba1c,
        "my hba1c came back — should I worry given my father has diabetes?",
        provider,
    )
    assert "6.1%" in result.response_message
    assert result.provenance["tools"] == ["get_report_parameter"]
    assert result.provenance["path"] == "agentic"


async def test_tools_are_offered_at_none_risk(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="Here is some general guidance.")])
    await handle_chat(db_session, uuid.uuid4(), "what helps blood pressure?", provider)
    assert "get_latest_metric" in provider.calls[0]["tools"]


# --------------------------------------------------------------------------- #
# The guards that run AFTER the model
# --------------------------------------------------------------------------- #
async def test_a_drifted_value_is_caught_by_the_fidelity_guard(
    db_session, user_with_hba1c
):
    """The tool returned 6.1; the model says 6.5. That must not ship."""
    provider = _tool_then_say(
        "get_report_parameter", {"parameter": "HbA1c"}, "Your HbA1c was 6.5%."
    )
    result = await handle_chat(
        db_session, user_with_hba1c, "what was my hba1c", provider
    )
    assert "6.5%" not in result.response_message
    assert result.provenance["degraded"] == "fidelity"


async def test_an_invented_dose_is_caught(db_session):
    provider = FakeProvider(
        turns=[LLMTurn(text="The usual dose is 500 mg twice daily.")]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    assert "500 mg" not in result.response_message
    assert result.provenance["degraded"] == "ungrounded_value"


async def test_a_banned_diagnostic_reply_is_replaced(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="You probably have diabetes.")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    assert "you probably have" not in result.response_message.lower()
    assert result.provenance["degraded"] == "validation"


async def test_a_provider_leak_is_replaced(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="I am powered by GPT-4, actually.")])
    result = await handle_chat(db_session, uuid.uuid4(), "how does sleep work", provider)
    assert "gpt" not in result.response_message.lower()


async def test_provider_failure_degrades_to_a_safe_reply(db_session):
    provider = FakeProvider(raises=RuntimeError("provider down"))
    result = await handle_chat(
        db_session, uuid.uuid4(), "what helps blood pressure?", provider
    )
    assert "clinician" in result.response_message
    assert result.provenance["degraded"] == "provider_error"


async def test_a_failing_tool_does_not_break_the_turn(db_session, monkeypatch):
    from app.chat.tools import registry

    async def _boom(*_a, **_kw):
        raise RuntimeError("db gone")

    monkeypatch.setitem(registry.EXECUTORS, "get_latest_metric", _boom)

    provider = _tool_then_say(
        "get_latest_metric",
        {"metric": "hba1c"},
        "I could not look that up just now — worth checking with your doctor.",
    )
    result = await handle_chat(db_session, uuid.uuid4(), "my hba1c?", provider)
    assert "could not look that up" in result.response_message


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #
async def test_the_turn_is_persisted_and_a_receipt_written(db_session):
    from sqlalchemy import select

    from app.models.chat import ConversationMessage, RagTurnReceipt

    provider = FakeProvider(turns=[LLMTurn(text="Some general guidance.")])
    user_id = uuid.uuid4()
    result = await handle_chat(db_session, user_id, "how does sleep work?", provider)

    msgs = (
        (
            await db_session.execute(
                select(ConversationMessage).where(
                    ConversationMessage.session_id == result.session_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [m.role for m in msgs] == ["user", "assistant"]

    receipts = (
        (
            await db_session.execute(
                select(RagTurnReceipt).where(RagTurnReceipt.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(receipts) == 1
    # No PHI in receipts — the message is stored as a hash.
    assert "sleep" not in receipts[0].query_hash


async def test_the_trace_never_names_the_provider(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="Some general guidance.")])
    result = await handle_chat(db_session, uuid.uuid4(), "how does sleep work?", provider)
    blob = " ".join(f"{s['step']} {s['detail']}" for s in result.trace).lower()
    for name in ("anthropic", "openai", "claude", "gpt", "fake"):
        assert name not in blob

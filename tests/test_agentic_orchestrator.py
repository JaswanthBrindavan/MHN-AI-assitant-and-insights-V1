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
    """The tool returned 6.1; the model says 6.5. That must not ship.

    Recovery gets one corrective retry, so the drifted figure is gone either
    way — the invariant is that 6.5% never reaches the reader.
    """
    provider = _tool_then_say(
        "get_report_parameter", {"parameter": "HbA1c"}, "Your HbA1c was 6.5%."
    )
    result = await handle_chat(
        db_session, user_with_hba1c, "what was my hba1c", provider
    )
    assert "6.5%" not in result.response_message


async def test_a_drift_that_survives_the_retry_falls_back_to_the_safe_reply(
    db_session, user_with_hba1c
):
    """When the rewrite drifts too, the floor still catches it."""
    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="get_report_parameter",
                        arguments={"parameter": "HbA1c"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(text="Your HbA1c was 6.5%."),
            LLMTurn(text="Sorry — I meant your HbA1c was 6.7%."),
        ]
    )
    result = await handle_chat(
        db_session, user_with_hba1c, "what was my hba1c", provider
    )
    assert "6.5%" not in result.response_message
    assert "6.7%" not in result.response_message
    assert result.provenance["degraded"] == "fidelity"


async def test_an_invented_dose_is_caught(db_session):
    provider = FakeProvider(
        turns=[LLMTurn(text="The usual dose is 500 mg twice daily.")]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    assert "500 mg" not in result.response_message


async def test_an_invented_dose_that_survives_the_retry_falls_back(db_session):
    provider = FakeProvider(
        turns=[
            LLMTurn(text="The usual dose is 500 mg twice daily."),
            LLMTurn(text="Actually the usual dose is 850 mg twice daily."),
        ]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    assert "500 mg" not in result.response_message
    assert "850 mg" not in result.response_message
    assert result.provenance["degraded"] == "ungrounded_value"


async def test_a_banned_diagnostic_reply_is_replaced(db_session):
    provider = FakeProvider(turns=[LLMTurn(text="You probably have diabetes.")])
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    assert "you probably have" not in result.response_message.lower()


async def test_a_reply_that_stays_banned_after_the_retry_falls_back(db_session):
    """Exactly one corrective retry — a model that keeps failing the guards
    must not keep spending the reader's time."""
    provider = FakeProvider(
        turns=[
            LLMTurn(text="You probably have diabetes."),
            LLMTurn(text="To be clear, you definitely have diabetes."),
        ]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "tell me about blood sugar", provider
    )
    low = result.response_message.lower()
    assert "you probably have" not in low
    assert "you definitely have" not in low
    assert result.provenance["degraded"] == "validation"
    # Two model calls, not three: the retry is capped at one.
    assert len(provider.calls) == 2


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
                select(ConversationMessage)
                .where(ConversationMessage.session_id == result.session_id)
                # The codebase's ordering contract. Without it this asserted on
                # whatever physical order the planner happened to return: it
                # passed for a long time, then a new composite index on
                # (session_id, created_at DESC, id DESC) changed the plan and it
                # read assistant-then-user. Under pytest-randomly it would have
                # been flaky rather than reliably broken, which is worse.
                .order_by(
                    ConversationMessage.created_at, ConversationMessage.id
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


async def test_a_thousands_separator_does_not_degrade_a_correct_figure(db_session):
    """The reported bug: "How's my water intake?" answered with the safe reply.

    The model quoted the tool's own weekly total back as "14,000 ml". The comma
    is a word boundary, so the fidelity guard tokenised the FRAGMENT "000 ml",
    found it in no source, threw the whole verbatim-correct reply away and fed
    the corrective retry a token the model had never written.
    """
    from app.coredata.service import add_lifestyle_log

    user_id = uuid.uuid4()
    await add_lifestyle_log(db_session, user_id, "water", 14000, "ml")
    await db_session.flush()

    provider = _tool_then_say(
        "get_tracker_total",
        {"metric": "water", "period": "week"},
        "You have logged 14,000 ml of water in the past 7 days. That is what "
        "is on record here, not a complete picture of your intake.",
    )
    result = await handle_chat(
        db_session, user_id, "How's my water intake?", provider
    )

    assert "14,000 ml" in result.response_message
    assert result.provenance.get("degraded") is None


async def test_a_per_day_average_of_the_readers_own_total_is_not_degraded(
    db_session,
):
    """The other half of the same failure. The comma fix landed; a figure the
    model DERIVED from the traced total was still untraceable, and dividing a
    weekly total by seven is the single most likely thing a helpful model does
    with a weekly total. Both halves here are verbatim-correct and the reader
    got the safe reply."""
    from app.coredata.service import add_lifestyle_log

    user_id = uuid.uuid4()
    await add_lifestyle_log(db_session, user_id, "water", 14000, "ml")
    await db_session.flush()

    provider = _tool_then_say(
        "get_tracker_total",
        {"metric": "water", "period": "week"},
        "You logged 14,000 ml of water in the past 7 days - roughly 2,000 ml "
        "per day.",
    )
    result = await handle_chat(
        db_session, user_id, "how much water this week", provider
    )

    assert result.provenance.get("degraded") is None
    assert "2,000 ml" in result.response_message


async def test_a_drifted_figure_is_still_degraded(db_session):
    """Non-vacuity for the test above: the tolerance is same-unit arithmetic
    over a value that EXISTS, not a licence to state a number."""
    from app.coredata.service import add_lifestyle_log

    user_id = uuid.uuid4()
    await add_lifestyle_log(db_session, user_id, "water", 14000, "ml")
    await db_session.flush()

    provider = _tool_then_say(
        "get_tracker_total",
        {"metric": "water", "period": "week"},
        "You logged 14,000 ml of water in the past 7 days - roughly 1,950 ml "
        "per day.",
    )
    result = await handle_chat(
        db_session, user_id, "how much water this week", provider
    )

    assert "1,950 ml" not in result.response_message


# --------------------------------------------------------------------------- #
# "Discuss with your clinician" under every answer
# --------------------------------------------------------------------------- #
# Reported from a phone, and the reason these are INTEGRATION tests rather than
# unit tests of the rule: the first attempt at this fix passed its own unit
# tests and changed nothing on the device, because the flaw was in which
# variable the rule was given — `chunks`, which retrieval fills in ahead of the
# engine branch for every question — rather than in the logic around it. Only a
# test that goes through `handle_chat` can see that.

async def test_a_tracker_answer_does_not_tell_the_reader_to_see_a_doctor(
    db_session, monkeypatch
):
    """The exact turn that shipped wrong: a tool answer, no risk, no citation.

    The trace on the device read `Engine — agentic`, `Records — looked up: get
    tracker total`, and the reply still carried the red line.

    **Retrieval is forced to return something**, which is the whole point of
    this test rather than a decoration on it. Production retrieves corpus
    blocks for EVERY question — `retrieve_chunks` runs ahead of the engine
    branch — and the first attempt at this fix keyed on that variable. With an
    empty test corpus that attempt passed a version of this test while the
    device stayed broken. A chunk is seeded here so the two cannot agree by
    accident: retrieval returns a block, the answer cites none of it, and the
    reader gets no clinician line.
    """
    from app.rag.retrieval import RetrievedChunk

    async def _one_chunk(db, codes, message):
        return [
            RetrievedChunk(
                id="chunk-1",
                condition_code="E11",
                chunk_type="overview",
                content="Type 2 diabetes is a long-term condition.",
                score=0.9,
            )
        ]

    monkeypatch.setattr("app.chat.orchestrator.retrieve_chunks", _one_chunk)

    provider = _tool_then_say(
        "get_tracker_total",
        {"metric": "steps", "period": "this_week"},
        "You've walked 12,236 steps so far this week.",
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "how many steps did I walk this week", provider
    )

    assert result.provenance["path"] == "agentic"
    assert result.provenance["chunks"], "retrieval must have returned something"
    assert result.recommended_action == "none", (
        "a step count is not a reason to see a doctor"
    )


async def test_a_cited_answer_still_points_at_a_clinician(db_session):
    """The other half. A reply that cites the corpus keeps the line — that is
    the case it was written for, and this fix must not take it away."""
    provider = FakeProvider(
        turns=[LLMTurn(text="Blood pressure is usually reported as two numbers [1].")]
    )
    result = await handle_chat(
        db_session, uuid.uuid4(), "what is blood pressure?", provider
    )

    if result.provenance.get("path") == "agentic" and not result.provenance.get(
        "degraded"
    ):
        assert result.recommended_action == "discuss_with_clinician"

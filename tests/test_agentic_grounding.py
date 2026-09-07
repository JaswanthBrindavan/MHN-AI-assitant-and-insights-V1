"""GROUNDING_MODE on the engine production actually runs.

`_apply_grounding` had one caller, below the engine branch in `_dispatch`, so
on the agentic engine `GROUNDING_MODE=enforce` was live in production and did
nothing: the terminal wrote `grounding_status="agentic"` with no report. These
pin that enforce rejects, log keeps, off skips, the one-retry-then-safe-reply
ladder runs, and the stream gate holds back what the buffered path rejects.

The source model is the one the fidelity guard already uses: retrieved blocks
are [n], the patient block plus every trusted tool result is [P], and a
sentence stating nothing but values a tool returned needs no marker at all —
a tool result has no marker in the prompt's vocabulary, and every number in
it has already been traced.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.chat.streaming import AnswerSink
from app.grounding.claims import analyze_grounding
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall
from app.models.chat import RagTurnReceipt

QUESTION = "what was my glucose"
# A directive with no number in it: the class the fidelity guard cannot see and
# validate_reply lets through (checked: ok=True). Only grounding catches it.
DIRECTIVE = "You can stop taking metformin once you feel better."
DIRECTIVE_AGAIN = "You should stop it once you feel fine."
# Verbatim what the tool returned, no marker.
VALUE = "Your most recent Glucose was 140 mg/dL."
CITED = "Your most recent Glucose was 140 mg/dL [P]."
# The value traces to the tool; the threshold is a claim no tool made.
VALUE_PLUS_THRESHOLD = "Your Glucose was 140 mg/dL, which is above 126."


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def user(db_session):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(
        Report(
            id=903,
            user_id=user_id,
            filepath="reports/g",
            private=False,
            content={
                "ai": {
                    "classification": {"section": "reports", "title": "Lab"},
                    "extraction": {
                        "results": [
                            {
                                "test_name": "Glucose",
                                "value": "140",
                                "unit": "mg/dL",
                                "value_numeric": 140,
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


def _tool_then_say(*replies: str) -> FakeProvider:
    """One records lookup, then the scripted answers in order. Once they run
    out the fake answers with its DEFAULT, which carries no claim at all."""
    return FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(
                        id="c1", name="get_report_parameter",
                        arguments={"parameter": "Glucose"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            *(LLMTurn(text=reply) for reply in replies),
        ]
    )


async def _receipt(db_session) -> RagTurnReceipt:
    rows = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert len(rows) == 1
    return rows[0]


# --------------------------------------------------------------------------- #
# enforce
# --------------------------------------------------------------------------- #
async def test_enforce_rejects_an_ungrounded_directive_and_retries(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    provider = _tool_then_say(DIRECTIVE)

    result = await handle_chat(db_session, user, QUESTION, provider)

    # Tool round, answer, ONE corrective retry.
    assert len(provider.calls) == 3
    assert "stop taking" not in result.response_message.lower()
    assert result.provenance.get("degraded") is None
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"
    receipt = await _receipt(db_session)
    assert receipt.grounding_mode == "enforce"
    assert receipt.grounding_status == "grounded"


async def test_enforce_falls_back_when_the_retry_is_still_ungrounded(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    provider = _tool_then_say(DIRECTIVE, DIRECTIVE_AGAIN)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 3
    assert "stop" not in result.response_message.lower()
    assert "clinician" in result.response_message.lower()
    assert result.provenance["degraded"] == "grounding"
    assert result.grounding is not None
    assert result.grounding["status"] == "violations"
    assert result.grounding["violations"][0]["type"] == "ungrounded_claim"
    receipt = await _receipt(db_session)
    assert receipt.grounding_status == "violations"
    assert receipt.grounding is not None


async def test_a_value_the_tool_returned_needs_no_marker(
    db_session, user, set_grounding_mode
):
    """The fidelity guard traced it; a marker would add nothing checkable."""
    set_grounding_mode("enforce")
    provider = _tool_then_say(VALUE)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 2
    assert "140 mg/dL" in result.response_message
    assert result.provenance.get("degraded") is None
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"
    assert (await _receipt(db_session)).grounding_status == "grounded"


async def test_a_tool_value_cited_as_the_readers_records_passes(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    provider = _tool_then_say(CITED)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 2
    assert "140 mg/dL" in result.response_message
    assert "[P]" not in result.response_message
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"
    assert result.grounding["cited"] == ["P"]


async def test_a_fidelity_degrade_still_records_what_the_model_said(
    db_session, user, set_grounding_mode
):
    """The receipt's grounding column is about the model's text, not about
    which rung replaced it — "off" means the mode, nothing else."""
    set_grounding_mode("enforce")
    # 150 drifts from the tool's 140: the fidelity rung degrades (the retry is
    # the DEFAULT, which the guard accepts, so only if it too fails).
    provider = _tool_then_say(
        "Your Glucose was 150 mg/dL [P].", "It was 155 mg/dL [P]."
    )

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert result.provenance["degraded"] == "fidelity"
    receipt = await _receipt(db_session)
    assert receipt.grounding_status == "violations"
    assert receipt.grounding is not None
    assert receipt.grounding["violations"][0]["type"] == "unsupported_value"


async def test_a_threshold_no_tool_returned_still_needs_a_marker(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    provider = _tool_then_say(VALUE_PLUS_THRESHOLD)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 3
    assert "above 126" not in result.response_message


# --------------------------------------------------------------------------- #
# log and off keep their meanings
# --------------------------------------------------------------------------- #
async def test_log_mode_keeps_the_answer_and_records_the_violation(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("log")
    provider = _tool_then_say(DIRECTIVE)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 2
    assert "stop taking" in result.response_message.lower()
    assert result.provenance.get("degraded") is None
    assert result.grounding is not None
    assert result.grounding["status"] == "violations"
    receipt = await _receipt(db_session)
    assert receipt.grounding_mode == "log"
    assert receipt.grounding_status == "violations"


async def test_off_mode_runs_no_analysis(db_session, user, set_grounding_mode):
    set_grounding_mode("off")
    provider = _tool_then_say(DIRECTIVE)

    result = await handle_chat(db_session, user, QUESTION, provider)

    assert len(provider.calls) == 2
    assert result.grounding is None
    assert (await _receipt(db_session)).grounding_status == "off"


# --------------------------------------------------------------------------- #
# streamed replies
# --------------------------------------------------------------------------- #
def _shown(events: list[dict]) -> str:
    shown = ""
    for event in events:
        if event["type"] == "delta":
            shown += event["text"]
        elif event["type"] == "replace":
            shown = event["text"]
    return shown


async def test_the_stream_never_shows_a_claim_enforce_would_reject(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    events: list[dict] = []
    provider = _tool_then_say(DIRECTIVE + " Rest helps too.")

    result = await handle_chat(
        db_session, user, QUESTION, provider, stream=AnswerSink(events.append)
    )

    assert "stop taking" not in " ".join(e["text"] for e in events).lower()
    assert _shown(events) == result.response_message
    assert "stop taking" not in result.response_message.lower()


async def test_the_stream_still_releases_a_tool_backed_value(
    db_session, user, set_grounding_mode
):
    set_grounding_mode("enforce")
    events: list[dict] = []
    provider = _tool_then_say(VALUE + " Keep tracking it.")

    result = await handle_chat(
        db_session, user, QUESTION, provider, stream=AnswerSink(events.append)
    )

    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert any("140 mg/dL" in d for d in deltas)
    assert not [e for e in events if e.get("reason") == "final_check"]
    assert _shown(events) == result.response_message


# --------------------------------------------------------------------------- #
# The analysis itself
# --------------------------------------------------------------------------- #
TOOL = ['{"parameter": "Glucose", "deterministic_reply": "Glucose 140 mg/dL (high)"}']


def _status_with_tool(answer: str) -> str:
    return analyze_grounding(
        answer, num_chunks=0, has_patient_context=True, retrieval_happened=False,
        chunk_texts=[], patient_text=TOOL[0], tool_texts=TOOL,
    ).status


def test_a_tool_value_is_grounded_by_provenance_but_a_directive_is_not():
    assert _status_with_tool(VALUE) == "grounded"
    assert _status_with_tool(DIRECTIVE) == "violations"
    assert _status_with_tool(VALUE_PLUS_THRESHOLD) == "violations"
    # A number no tool returned is not provenance.
    assert _status_with_tool("Your Glucose was 150 mg/dL.") == "violations"
    # A duration is factual here but never verified by the fidelity guard, so
    # a traced value beside it does not carry it.
    assert _status_with_tool(
        "You slept 7 hours and your Glucose was 140 mg/dL."
    ) == "violations"


def test_without_tool_texts_a_marker_free_value_is_still_a_violation():
    report = analyze_grounding(
        VALUE, num_chunks=0, has_patient_context=True, retrieval_happened=False,
        chunk_texts=[], patient_text=TOOL[0],
    )
    assert report.status == "violations"


def test_a_cited_value_is_matched_the_way_the_fidelity_guard_matches_it():
    """'6.1%' against a record that says '6.1 %' was an unsupported_value
    under the old literal-substring check; the guard that decides whether the
    reply ships already normalises, and so must the citation check."""
    report = analyze_grounding(
        "Your HbA1c was 6.1% [P].", num_chunks=0, has_patient_context=True,
        retrieval_happened=False, chunk_texts=[], patient_text='"value": "6.1 %"',
    )
    assert report.status == "grounded"
    wrong = analyze_grounding(
        "Your HbA1c was 6.5% [P].", num_chunks=0, has_patient_context=True,
        retrieval_happened=False, chunk_texts=[], patient_text='"value": "6.1 %"',
    )
    assert [v["type"] for v in wrong.violations] == ["unsupported_value"]

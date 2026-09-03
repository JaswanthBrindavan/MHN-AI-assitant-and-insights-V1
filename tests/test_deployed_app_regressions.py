"""Four defects found by driving the DEPLOYED app on ``CHAT_ENGINE=agentic``.

Every one of them is the repo's recurring shape: a deterministic handler whose
output the agentic engine either recomposed, routed around, or dropped on the
way out. The suite was green through all four.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.chat.orchestrator import handle_chat
from app.chat.replies import CARRIED_ESCALATION
from app.config import get_settings
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall
from app.models.common import utcnow
from app.triage.red_flags import EMERGENCY, HIGH, NONE


@pytest.fixture
def use_engine(monkeypatch):
    """Run one turn on a named engine."""
    def _set(name: str) -> None:
        monkeypatch.setenv("CHAT_ENGINE", name)
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


def _tool_then_say(tool: str, arguments: dict, reply: str) -> FakeProvider:
    return FakeProvider(turns=[
        LLMTurn(tool_calls=(ToolCall(id="c1", name=tool, arguments=arguments),),
                stop_reason="tool_use"),
        LLMTurn(text=reply),
    ])


# --------------------------------------------------------------------------- #
# 1. The health summary is served verbatim, on BOTH engines
# --------------------------------------------------------------------------- #
async def _seed_summary_data(db, user_id):
    from app.models.coredata import LifestyleLog, VitalReading

    db.add(LifestyleLog(user_id=user_id, log_type="coffee", quantity=3,
                        unit="cup", logged_at=utcnow() - timedelta(days=1)))
    db.add(VitalReading(user_id=user_id, vital_type="blood_pressure",
                        value_primary=128, value_secondary=84, unit="mmHg",
                        recorded_at=utcnow() - timedelta(days=2)))
    await db.flush()


async def test_a_summary_is_never_recomposed_by_the_model(db_session, use_engine):
    """The defect: the model recomposed the summary, normalised a course
    recorded as "Dolo 650 Tablet" to "Dolo 650 mg" -- inventing a unit on a
    medication dose -- and the numeric-fidelity guard threw the whole answer
    away for the safe reply. A handler whose value is being exact should not
    be recomposed at all.
    """
    user_id = uuid.uuid4()
    await _seed_summary_data(db_session, user_id)

    use_engine("agentic")
    provider = FakeProvider(turns=[LLMTurn(text="a recomposed summary")])
    result = await handle_chat(
        db_session, user_id, "give me a summary of my health", provider
    )

    assert provider.calls == [], "the model was asked to compose the summary"
    assert result.provenance["path"] == "health_summary"
    assert "128/84" in result.response_message


async def test_both_engines_return_the_same_summary_text(db_session, use_engine):
    user_id = uuid.uuid4()
    await _seed_summary_data(db_session, user_id)

    use_engine("legacy")
    legacy = await handle_chat(
        db_session, user_id, "summarise my health", FakeProvider()
    )
    use_engine("agentic")
    agentic = await handle_chat(
        db_session, user_id, "summarise my health", FakeProvider()
    )
    assert legacy.response_message == agentic.response_message
    assert legacy.provenance["path"] == "health_summary"
    assert agentic.provenance["path"] == "health_summary"


def test_the_fidelity_guard_that_caught_it_is_not_weakened():
    """The guard was RIGHT: a unit invented on a medication dose is exactly
    what it exists to catch. The fix was to stop composing, not to relax this.
    """
    from app.grounding.fidelity import values_traceable

    source = ["Your medications: Dolo 650 Tablet, metformin 500 mg."]
    assert values_traceable(
        "You take Dolo 650 Tablet and metformin 500 mg.", source
    )[0]
    ok, stray = values_traceable(
        "You take Dolo 650 mg and metformin 500 mg.", source
    )
    assert not ok and "650 mg" in stray


# --------------------------------------------------------------------------- #
# 2. A carried episode must not disable a handler, or replace the answer
# --------------------------------------------------------------------------- #
async def _seed_coffee_and_sleep(db, user_id):
    from app.models.coredata import LifestyleDailyTotal, SahhaDailyTotal

    today = utcnow().date()
    for i in range(1, 25):
        db.add(SahhaDailyTotal(
            user_id=user_id, metric="sleep_duration",
            bucket_start=today - timedelta(days=i),
            total=360.0 if i <= 9 else 402.0, entries=1, days_counted=1,
        ))
        if i <= 9:
            db.add(LifestyleDailyTotal(
                user_id=user_id, metric="coffee",
                bucket_start=today - timedelta(days=i),
                total=2.0, entries=2, days_counted=1,
            ))
    await db.flush()


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_carried_episode_prefixes_the_correlation_answer(
    db_session, use_engine, name
):
    """Reported: "is my coffee the reason i sleep badly" returned ONLY the
    carried-escalation banner. `handle_correlation_query`'s slot opened with
    `if risk != NONE: return None`, which is right for a red flag in THIS
    message and wrong for a level carried from an earlier turn.
    """
    from app.chat.episodes import open_or_touch as record_episode

    user_id = uuid.uuid4()
    await _seed_coffee_and_sleep(db_session, user_id)
    await record_episode(
        db_session, user_id, "chest pain and left arm discomfort", EMERGENCY
    )
    await db_session.flush()

    use_engine(name)
    result = await handle_chat(
        db_session, user_id, "is my coffee the reason i sleep badly",
        FakeProvider(),
    )

    assert result.provenance["path"] == "correlation_query"
    # THE INVARIANT: the question is answered. It used to come back as the
    # banner and nothing else.
    body = result.response_message
    if body.startswith(CARRIED_ESCALATION):
        body = body[len(CARRIED_ESCALATION):].strip()
    assert len(body) > 40, f"the question was never answered: {body!r}"

    # And the banner itself stays quiet here: a coffee-and-sleep lookup is not
    # about the open episode, and repeating the warning on every turn is what
    # the owner asked to stop. It returns the moment the turn IS about their
    # health -- pinned by the next test.
    assert not result.response_message.startswith(CARRIED_ESCALATION)


async def test_a_carried_episode_still_lets_the_model_read_the_records(
    db_session, use_engine
):
    """`offered = TOOL_SPECS if risk == NONE else ()` left the model with no
    records access on an ordinary question, purely because of an old episode.
    """
    from app.chat.episodes import open_or_touch as record_episode

    user_id = uuid.uuid4()
    await record_episode(
        db_session, user_id, "chest pain and left arm discomfort", EMERGENCY
    )
    await db_session.flush()

    use_engine("agentic")
    provider = FakeProvider(
        turns=[LLMTurn(text="Here is what your records show.")]
    )
    result = await handle_chat(
        db_session, user_id, "what did my last report say about me", provider
    )
    assert provider.calls and provider.calls[0]["tools"], (
        "a carried floor left the model with no tools on a records question"
    )
    assert result is not None


async def test_a_red_flag_in_this_message_still_takes_the_tools_away(
    db_session, use_engine
):
    """The release is for a CARRIED level only. A red flag described now keeps
    the safe path, so nothing can delay or dilute the escalation.
    """
    use_engine("agentic")
    provider = FakeProvider(turns=[LLMTurn(text="Please get seen.")])
    result = await handle_chat(
        db_session, uuid.uuid4(),
        "i have been coughing up blood", provider,
    )
    assert result.risk_level == HIGH
    assert provider.calls and provider.calls[0]["tools"] == []


async def test_an_empty_model_turn_at_high_is_not_a_banner_only_reply(
    db_session, use_engine
):
    """Prefixing the banner to "" produced a reply that PASSED validation --
    non-empty, carries an escalation -- so the `empty` rule never fired and
    the degradation was never counted.
    """
    use_engine("agentic")
    # Both the answer and the corrective retry come back empty, so there is
    # nothing to prefix and the turn must degrade rather than pass.
    provider = FakeProvider(turns=[LLMTurn(text=""), LLMTurn(text="")])
    result = await handle_chat(
        db_session, uuid.uuid4(),
        "i have been coughing up blood", provider,
    )
    assert result.provenance["degraded"] == "validation"


# --------------------------------------------------------------------------- #
# 3. An empty window still names the latest reading -- with its FIGURE
# --------------------------------------------------------------------------- #
async def test_an_empty_lifestyle_window_names_the_last_day_and_its_amount(
    db_session,
):
    from app.chat.data_handlers import handle_tracker_query
    from app.models.coredata import LifestyleDailyTotal

    user_id = uuid.uuid4()
    old = date.today() - timedelta(days=40)
    db_session.add(LifestyleDailyTotal(
        user_id=user_id, metric="water", bucket_start=old,
        total=6.0, entries=3, days_counted=1,
    ))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, user_id, "how much water did i drink last week"
    )
    assert out is not None
    assert "not logged any water" in out["reply"]
    assert f"most recent is 6 ml of water on {old:%d %b %Y}" in out["reply"]


async def test_a_reading_inside_the_asked_window_is_never_repeated_as_stale(
    db_session,
):
    """The stale line is for readings OUTSIDE the window asked about."""
    from app.chat.data_handlers import handle_tracker_query
    from app.models.coredata import LifestyleDailyTotal

    user_id = uuid.uuid4()
    db_session.add(LifestyleDailyTotal(
        user_id=user_id, metric="water", bucket_start=date.today(),
        total=6.0, entries=3, days_counted=1,
    ))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, user_id, "how much water did i drink yesterday"
    )
    assert out is not None
    assert "most recent is" not in out["reply"]


# --------------------------------------------------------------------------- #
# 4. Document cards reach the client on BOTH engines
# --------------------------------------------------------------------------- #
async def _seed_documents(db, user_id):
    from app.models.coredata import Report

    db.add(Report(user_id=user_id, filepath="reports/lab.pdf",
                  private=False, created_at=utcnow()))
    await db.flush()


async def test_both_engines_return_the_same_document_cards(db_session, use_engine):
    """`documents=` was set on the LEGACY terminal only, so the agentic engine
    built the cards, paid for them in prompt tokens as a `_PASSTHROUGH`, and
    then dropped them -- the reader saw their reports with no button to open
    one. Same defect already fixed for `visual`, one field over.
    """
    user_id = uuid.uuid4()
    await _seed_documents(db_session, user_id)
    question = "show me my lab reports"

    use_engine("legacy")
    legacy = await handle_chat(db_session, user_id, question, FakeProvider())

    use_engine("agentic")
    agentic = await handle_chat(
        db_session, user_id, question,
        _tool_then_say("get_documents", {"kinds": ["report"]},
                       "Here is the report on your record."),
    )

    assert legacy.documents, "premise: the legacy engine returns cards"
    assert agentic.documents == legacy.documents


async def test_the_cards_are_not_sent_into_the_prompt(db_session):
    """Client plumbing (`resource_type`, the row id) the model cannot act on.
    The reply already names every title and date -- same finding as the SVG.
    """
    from app.chat.tools.executors import OUT_OF_BAND_DOCUMENTS, get_documents

    user_id = uuid.uuid4()
    await _seed_documents(db_session, user_id)
    payload = await get_documents(
        db_session, user_id, {"kinds": ["report"]}, None
    )
    assert payload is not None
    assert payload[OUT_OF_BAND_DOCUMENTS], "the cards must still be produced"
    assert "documents" not in payload, "cards must not reach the model"
    assert "lab.pdf" in payload["deterministic_reply"]


async def test_a_degraded_agentic_reply_carries_no_cards(db_session, use_engine):
    """A reply that was replaced shows none of that content, so it offers no
    button to open a file it never mentioned -- the same rule as `visual`.
    """
    user_id = uuid.uuid4()
    await _seed_documents(db_session, user_id)

    use_engine("agentic")
    # Both the answer and the corrective retry state a figure no record holds,
    # so the fidelity guard degrades the turn instead of recovering it.
    provider = FakeProvider(turns=[
        LLMTurn(tool_calls=(ToolCall(id="c1", name="get_documents",
                                     arguments={"kinds": ["report"]}),),
                stop_reason="tool_use"),
        LLMTurn(text="Your report shows a reading of 987 mg."),
        LLMTurn(text="It still shows 987 mg."),
    ])
    result = await handle_chat(
        db_session, user_id, "show me my lab reports", provider
    )
    assert result.provenance["degraded"] == "fidelity"
    assert result.documents is None
    assert result.risk_level == NONE


# --------------------------------------------------------------------------- #
# Precedence, after the summary was hoisted into the shared prologue
# --------------------------------------------------------------------------- #
def test_a_specific_ask_is_not_swallowed_by_the_whole_health_summary():
    """Hoisting `handle_summary_query` moved it ABOVE every handler that used
    to run first, and `_SUMMARY_RE` matches a bare "summary".

    Measured before this guard: "summary of my blood pressure" answered with a
    week of everything instead of the reading; "log 2 glasses of water for my
    health summary" stopped WRITING; and worst, a stated reading outranked
    `handle_value_check`, the deterministic reference-range check that is
    deliberately first in the legacy chain.
    """
    from app.chat.abilities import (
        parse_document_query_fuzzy,
        parse_metric_query,
        parse_report_param_ask,
        parse_section_detail_query,
        parse_stated_value,
        parse_summary_query,
        parse_tracker_add,
        parse_tracker_query,
    )

    specific = (
        parse_stated_value, parse_tracker_add, parse_tracker_query,
        parse_metric_query, parse_document_query_fuzzy,
        parse_report_param_ask, parse_section_detail_query,
    )

    def yields_to_specific(m: str) -> bool:
        return any(f(m) is not None for f in specific)

    for m in (
        "summary of my blood pressure",
        "summary of my sleep last night",
        "summary of my water intake this week",
        "summary of my last blood test",
        "show me my health records summary",
        "log 2 glasses of water for my health summary",
        "my hba1c is 7.2, summary please",
    ):
        assert yields_to_specific(m), f"{m!r} would be swallowed by the summary"

    # ...and the whole-health ask must still reach it.
    for m in (
        "summarise my health",
        "summarize my health",
        "give me a summary of my health",
        "health summary",
        "give me my health summary",
    ):
        assert parse_summary_query(m) is not None, m
        assert not yields_to_specific(m), (
            f"{m!r} no longer reaches the summary — the precedence list is too broad"
        )


def test_the_ai_result_parser_is_kept_out_of_the_precedence_list():
    """It claims the bare word "summary", so consulting it here would hand
    every whole-health ask to a handler that then declines. Its own handler
    gates on a document reference, which is the real precedence."""
    from app.chat.abilities import parse_ai_result_query

    assert parse_ai_result_query("summarise my health") is not None, (
        "if this parser stops claiming a bare summary, it can join the list"
    )


def test_a_carried_banner_and_the_action_field_cannot_disagree():
    """The prose and the machine-readable field are one payload.

    When the escalation leads the reply, the reader is told to seek care
    promptly while `recommended_action` still carried the handler's own
    verdict — `self_care` for a correlation readout. The mobile clients render
    that field, so the two halves of the same answer said different things.
    """
    from app.chat.orchestrator import _lead, _led_action

    banner = "Before that — you mentioned something earlier that can be serious."
    answer = "You logged coffee on 9 of the past 28 days."

    # Banner shown -> the action matches it.
    assert _lead(banner, "high", answer).startswith("Before that")
    assert _led_action("high", answer, "self_care") == "seek_care_promptly"

    # No banner -> the handler's own verdict stands.
    assert _lead(banner, "none", answer) == answer
    assert _led_action("none", answer, "self_care") == "self_care"

    # Empty reply -> no banner, and no escalation invented for it either.
    assert _lead(banner, "high", "") == ""
    assert _led_action("high", "", "self_care") == "self_care"


# --------------------------------------------------------------------------- #
# The carried banner must not lead EVERY reply
# --------------------------------------------------------------------------- #
def test_an_unrelated_question_does_not_get_the_carried_banner():
    """Reported twice by the owner: "its not necessary to keep on repeating the
    same thing again and again... for every message its not good".

    An open episode led every reply with "you mentioned something earlier that
    can be serious" — including "how much water this week" and "show my latest
    lab reports". Repeated on every turn the sentence stops being a warning and
    becomes noise, which is the opposite of what a safety banner is for.
    """
    from app.chat.context import is_personal_health_query
    from app.triage.red_flags import triage

    def banner_shown(message: str) -> bool:
        tr = triage(message)
        return bool(tr.matched_terms) or is_personal_health_query(message)

    # Nothing to do with the open episode — answer the question asked.
    for quiet in (
        "how much water this week",
        "show my latest lab reports",
        "summarise my health",
        "how did i sleep last night",
    ):
        assert not banner_shown(quiet), quiet

    # Still about their health — the banner is the point.
    for loud in (
        "i feel dizzy",
        "my chest still hurts",
        "is my chest pain serious",
    ):
        assert banner_shown(loud), loud


def test_symptoms_reported_one_after_another_still_escalate():
    """The case the banner exists for. Suppressing it on unrelated questions
    must not suppress it on a second symptom."""
    from app.triage.red_flags import triage

    for second in ("and my left arm hurts", "i am also sweating a lot",
                   "now i feel breathless too"):
        assert triage(second).matched_terms or triage(
            "chest pain " + second
        ).matched_terms, second


def test_a_recovery_worded_message_closes_the_episode():
    """"the chest pain HAS SETTLED, i am fine now" was answered with "some of
    what you describe can be serious" — for a message reporting the opposite.

    Naming the symptom is how you say it is over, so the message re-matched
    triage and `has_red_flag` disabled the soft table. "is gone" worked;
    "has settled" did not. These phrasings state a resolution about the
    symptom they name, so they belong in the strict table.
    """
    from app.chat.episodes import is_recovery_message
    from app.triage.red_flags import triage

    def recovery(m: str) -> bool:
        return is_recovery_message(m, has_red_flag=bool(triage(m).matched_terms))

    for m in (
        "the chest pain has settled, i am fine now",
        "the pain has eased",
        "it has passed",
        "the chest pain settled down",
        "the headache went away",
        "the chest pain is gone, i am feeling fine now",
    ):
        assert recovery(m), m

    # Bare "stopped" is deliberately absent from the table.
    for m in (
        "my heart stopped",
        "i am not fine, the chest pain is worse",
        "fine, but the chest pain is worse",
    ):
        assert not recovery(m), m


async def test_the_banner_returns_when_the_turn_is_about_their_health(
    db_session, use_engine
):
    """Suppressing the banner on unrelated questions must not silence it.

    The moment the reader talks about their own symptoms again, the open
    episode leads the reply — that is the case it exists for.
    """
    from app.chat.episodes import open_or_touch as record_episode

    user_id = uuid.uuid4()
    await record_episode(
        db_session, user_id, "chest pain and left arm discomfort", EMERGENCY
    )
    await db_session.flush()

    use_engine("agentic")
    result = await handle_chat(
        db_session, user_id, "i feel dizzy as well", FakeProvider()
    )
    assert result.risk_level in (HIGH, EMERGENCY), (
        "a second symptom after an open episode must still escalate"
    )


# --------------------------------------------------------------------------- #
# "Discuss with your clinician" on every single answer
# --------------------------------------------------------------------------- #
# Reported from the phone: reading back a step count, a sleep total or a water
# figure all carried the same red line under the reply as a question about a
# symptom would. The deterministic handlers had already been calibrated —
# `none`, `self_care`, `review_with_clinician` — but the agentic engine, which
# answers most real questions, returned `discuss_with_clinician` for everything
# that was not HIGH risk.
#
# A warning that appears on every answer is not a warning. It is furniture, and
# on the day it matters it is furniture too.

def test_a_plain_data_readout_asks_nobody_to_see_a_doctor():
    """Their own step count, read back. The answer cited nothing."""
    from app.chat.orchestrator import _answer_action

    assert _answer_action("none", cited=False, degraded=None) == "none"


def test_retrieval_alone_is_not_a_reason_to_see_a_doctor():
    """The trap, and the one this fix was first written wrong for.

    Retrieval runs AHEAD of the engine branch, so a tracker total leaves it
    with the same `chunks` in scope as a condition lookup — the `Used`
    docstring records that reading `chunks` is what made "how much water did I
    drink" cite four unrelated condition profiles. `cited` is what the ANSWER
    used, and a data readout uses nothing.
    """
    from app.chat.orchestrator import _answer_action

    # Chunks were fetched; the reply cited none of them.
    assert _answer_action("none", cited=False, degraded=None) == "none"


def test_an_answer_that_cited_the_corpus_still_points_at_a_clinician():
    """Educational content ABOUT a condition keeps the pointer — that is the
    case the line was written for."""
    from app.chat.orchestrator import _answer_action

    assert _answer_action(
        "none", cited=True, degraded=None
    ) == "discuss_with_clinician"


def test_high_risk_still_escalates():
    """Nothing here may weaken the escalation. It runs before this and wins."""
    from app.chat.orchestrator import _answer_action
    from app.triage.red_flags import HIGH

    assert _answer_action(HIGH, cited=False, degraded=None) == "seek_care_promptly"


def test_a_degraded_turn_keeps_the_pointer_because_the_reply_says_it():
    """`safe_reply` literally says "speak with a clinician". The field and the
    prose have to agree, or the payload contradicts itself."""
    from app.chat.orchestrator import _answer_action

    assert _answer_action(
        "none", cited=False, degraded="validation"
    ) == "discuss_with_clinician"

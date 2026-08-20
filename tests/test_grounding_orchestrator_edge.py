"""Edge cases for app/grounding/claims.py and app/chat/orchestrator.py.

Pure-function batteries for the mechanical grounding verifier (marker parsing,
factual-sentence detection, marker stripping) plus fail-open orchestrator paths
driven through handle_chat with deterministic fake providers. No live LLM.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.chat.replies import HIGH_ESCALATION, SCOPE_DECLINE, safe_reply
from app.grounding.claims import analyze_grounding, is_factual, strip_markers
from app.llm.fake import FakeProvider
from app.models.chat import (
    ConversationMessage,
    ConversationSession,
    RagTurnReceipt,
)
from app.triage.red_flags import EMERGENCY, EMERGENCY_DIRECTIVE, HIGH, NONE

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")

SYMPTOM_Q = "tell me about diabetes and blood sugar"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RaisingProvider(FakeProvider):
    """Raises on every generate() call — simulates a total provider outage."""

    async def generate(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        raise RuntimeError("provider down")


class ScriptThenRaiseProvider(FakeProvider):
    """Returns scripted responses, then raises once the script is exhausted."""

    async def generate(self, *, system: str, user: str) -> str:
        if not self._responses:
            self.calls.append((system, user))
            raise RuntimeError("provider down mid-conversation")
        return await super().generate(system=system, user=user)


# --------------------------------------------------------------------------- #
# analyze_grounding — marker validity
# --------------------------------------------------------------------------- #
def test_marker_zero_and_out_of_range_invalid_with_three_chunks():
    report = analyze_grounding(
        "The dose is 5 mg [0]. Keep readings below 140 [99].",
        num_chunks=3,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "violations"
    assert len(report.violations) == 2
    assert {v["type"] for v in report.violations} == {"invalid_marker"}
    assert {v["marker"] for v in report.violations} == {"0", "99"}
    assert report.factual_count == 2
    assert report.cited == ["0", "99"]


def test_boundary_markers_one_and_n_valid_n_plus_one_invalid():
    ok = analyze_grounding(
        "Start at 5 mg [1]. Do not exceed 20 mg [3].",
        num_chunks=3,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert ok.status == "grounded"
    assert ok.cited == ["1", "3"]

    bad = analyze_grounding(
        "Start at 5 mg [4].",
        num_chunks=3,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert bad.status == "violations"
    assert bad.violations[0]["type"] == "invalid_marker"
    assert bad.violations[0]["marker"] == "4"


def test_two_markers_in_one_sentence_both_counted():
    report = analyze_grounding(
        "An HbA1c above 48 is worth discussing [1][2].",
        num_chunks=2,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"
    assert report.cited == ["1", "2"]
    assert report.factual_count == 1
    assert report.violations == []


def test_gk_flagged_when_retrieval_happened():
    report = analyze_grounding(
        "Adults often need 7 hours of sleep [GK].",
        num_chunks=2,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "violations"
    assert report.violations == [
        {
            "type": "gk_not_allowed",
            "sentence": "Adults often need 7 hours of sleep [GK].",
        }
    ]
    assert report.cited == ["GK"]


def test_gk_allowed_when_nothing_retrieved():
    report = analyze_grounding(
        "Adults often need 7 hours of sleep [GK].",
        num_chunks=0,
        has_patient_context=False,
        retrieval_happened=False,
    )
    assert report.status == "grounded"
    assert report.factual_count == 1
    assert report.cited == ["GK"]


def test_marker_inside_terminator_grounded():
    report = analyze_grounding(
        "The value is 5 mg [1].",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"
    assert report.factual_count == 1


def test_normalizer_pulls_trailing_marker_back_inside_sentence():
    # "... 5 mg. [1]" — the marker trails the period; the normalizer must
    # attach it to the sentence so the claim still counts as cited.
    report = analyze_grounding(
        "The value is 5 mg. [1]",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"
    assert report.factual_count == 1
    assert report.cited == ["1"]


def test_marker_in_prior_sentence_does_not_ground_the_next():
    report = analyze_grounding(
        "The details are in the guideline [1]. The dose is 5 mg.",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "violations"
    assert report.violations == [
        {"type": "ungrounded_claim", "sentence": "The dose is 5 mg."}
    ]
    assert report.factual_count == 1


def test_marker_at_start_of_sentence_still_counts_as_cited():
    # Markers are found anywhere in the sentence, not only at its end.
    report = analyze_grounding(
        "[1] The dose is 5 mg.",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"


def test_patient_marker_invalid_without_patient_context():
    report = analyze_grounding(
        "Your log shows 150 mmHg [P].",
        num_chunks=0,
        has_patient_context=False,
        retrieval_happened=False,
    )
    assert report.status == "violations"
    assert report.violations[0]["type"] == "invalid_marker"
    assert report.violations[0]["marker"] == "P"


def test_patient_marker_valid_with_patient_context():
    report = analyze_grounding(
        "Your log shows 150 mmHg [P].",
        num_chunks=0,
        has_patient_context=True,
        retrieval_happened=False,
    )
    assert report.status == "grounded"
    assert report.cited == ["P"]


def test_lowercase_p_is_not_a_marker():
    # MARKER_RE is case-sensitive: "[p]" is not a citation, so the factual
    # sentence counts as unmarked.
    report = analyze_grounding(
        "The dose is 5 mg [p].",
        num_chunks=1,
        has_patient_context=True,
        retrieval_happened=True,
    )
    assert report.status == "violations"
    assert report.violations[0]["type"] == "ungrounded_claim"
    assert report.cited == []


def test_empty_and_whitespace_answers_are_grounded_noops():
    for answer in ("", "   ", " \n\t "):
        report = analyze_grounding(
            answer,
            num_chunks=0,
            has_patient_context=False,
            retrieval_happened=False,
        )
        assert report.status == "grounded"
        assert report.violations == []
        assert report.factual_count == 0
        assert report.cited == []


def test_answer_of_only_a_marker():
    ok = analyze_grounding(
        "[1]", num_chunks=3, has_patient_context=False, retrieval_happened=True
    )
    assert ok.status == "grounded"
    assert ok.cited == ["1"]
    assert ok.factual_count == 0

    bad = analyze_grounding(
        "[1]", num_chunks=0, has_patient_context=False, retrieval_happened=False
    )
    assert bad.status == "violations"
    assert bad.violations[0]["type"] == "invalid_marker"


def test_multi_sentence_mixed_answer():
    answer = (
        "Here is some background. "
        "The usual dose is 5 mg [1]. "
        "Readings above 140 need review. "
        "General advice applies [GK]."
    )
    report = analyze_grounding(
        answer, num_chunks=2, has_patient_context=False, retrieval_happened=True
    )
    assert report.status == "violations"
    types = [v["type"] for v in report.violations]
    assert types == ["ungrounded_claim", "gk_not_allowed"]
    assert report.violations[0]["sentence"] == "Readings above 140 need review."
    assert report.factual_count == 2
    assert report.cited == ["1", "GK"]


def test_to_dict_round_trip():
    report = analyze_grounding(
        "The dose is 5 mg [1].",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    d = report.to_dict()
    assert d == {
        "status": "grounded",
        "violations": [],
        "factual_count": 1,
        "cited": ["1"],
    }


def test_unicode_answer_does_not_break_analysis():
    report = analyze_grounding(
        "Piña coladas aside — aim for 30 minutes of walking. Dose: 5 mg [1]. ✨",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"
    assert report.factual_count == 1


# --------------------------------------------------------------------------- #
# is_factual — units and thresholds
# --------------------------------------------------------------------------- #
def test_is_factual_decimal_dose():
    assert is_factual("The dose is 1.5 mg daily.") is True


def test_is_factual_range_matches_on_second_number():
    # "10-20 mg": the hyphen is a word boundary, so "20 mg" matches the unit
    # regex — ranges ARE treated as factual.
    assert is_factual("Take 10-20 mg with food.") is True


def test_is_factual_condition_name_with_number_is_not_factual():
    # "Type 2 diabetes" has a digit but no unit or threshold word.
    assert is_factual("Type 2 diabetes is a long-term condition.") is False


def test_is_factual_threshold_word_plus_number():
    assert is_factual("Readings above 180 should be discussed.") is True
    assert is_factual("Do at least 30 minutes of activity.") is True


def test_is_factual_threshold_word_without_number_is_not_factual():
    assert is_factual("Readings above normal should be discussed.") is False


def test_is_factual_percent_with_space():
    # Factual via the THRESHOLD regex ("less than 7"), spacing before % included.
    assert is_factual("Aim for less than 7 % if your doctor agrees.") is True


def test_is_factual_bare_percent_value_not_detected():
    # NOTE(potential-bug): _UNIT_RE ends with \b after the unit alternation, but
    # "%" is a non-word character, so "97%" followed by a space or punctuation
    # has no word boundary and never matches. A bare percent clinical value
    # ("an HbA1c of 6.5% ...") is therefore NOT treated as factual unless a
    # threshold word accompanies it. Asserting current actual behavior.
    assert is_factual("Oxygen at 97% is typical.") is False
    assert is_factual("An HbA1c of 6.5% is discussed.") is False


def test_is_factual_bare_metres_is_not_a_clinical_unit():
    # 'm' alone is not in the unit list (ml/mg/g/... are), so plain distances
    # are not treated as clinical claims.
    assert is_factual("I walked 500 m today.") is False


def test_is_factual_common_units_and_no_space_variant():
    assert is_factual("Blood pressure of 120 mmHg is typical.") is True
    assert is_factual("It can last 2 weeks.") is True
    assert is_factual("Take it 3 times a day.") is True
    assert is_factual("Take 5mg now.") is True
    assert is_factual("Drink 500 ml of water.") is True


def test_is_factual_plain_text_and_empty():
    assert is_factual("") is False
    assert is_factual("Stay hydrated and rest well.") is False


# --------------------------------------------------------------------------- #
# strip_markers
# --------------------------------------------------------------------------- #
def test_strip_markers_basic_and_punctuation_spacing():
    assert strip_markers("The value is 5 mg [1].") == "The value is 5 mg."
    assert strip_markers("Really [1]? Yes [2]!") == "Really? Yes!"


def test_strip_markers_at_start():
    assert strip_markers("[1] Keep hydrated.") == "Keep hydrated."


def test_strip_markers_multiple_adjacent_and_before_comma():
    assert (
        strip_markers("Dose 5 mg [1][2], then rest [P].") == "Dose 5 mg, then rest."
    )


def test_strip_markers_only_markers_becomes_empty():
    assert strip_markers("[1][2][GK]") == ""


def test_strip_markers_preserves_non_marker_brackets():
    assert strip_markers("Bracketed [note] and [p] stay.") == "Bracketed [note] and [p] stay."


def test_strip_markers_collapses_interior_runs_of_spaces():
    assert strip_markers("5 mg  [1]  works") == "5 mg works"


def test_strip_markers_idempotent():
    samples = (
        "The value is 5 mg [1].",
        "[GK] General advice.",
        "No markers at all.",
        "Dose 5 mg [1][2], then rest [P].",
    )
    for s in samples:
        once = strip_markers(s)
        assert strip_markers(once) == once


# --------------------------------------------------------------------------- #
# Orchestrator — provider failure paths (fail open, never crash)
# --------------------------------------------------------------------------- #
async def test_provider_error_degrades_to_safe_reply(db_session, set_grounding_mode):
    set_grounding_mode("log")
    provider = RaisingProvider()

    result = await handle_chat(db_session, USER, SYMPTOM_Q, provider)

    assert result.response_message == safe_reply(NONE)
    assert result.risk_level == NONE
    assert result.recommended_action == "discuss_with_clinician"
    assert result.provenance == {"path": "symptom_rag", "degraded": "provider_error"}
    assert result.grounding is None
    assert result.session_id is not None

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].grounding_status == "provider_error"
    assert receipts[0].grounding is None
    assert receipts[0].used_rag is False
    assert receipts[0].query_hash == _sha(SYMPTOM_Q)


async def test_provider_error_at_high_risk_keeps_escalation(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    provider = RaisingProvider()

    result = await handle_chat(
        db_session, USER, "I have severe chest pain right now", provider
    )

    assert result.risk_level == HIGH
    assert result.response_message == HIGH_ESCALATION
    assert result.recommended_action == "seek_care_promptly"
    assert result.provenance["degraded"] == "provider_error"


async def test_provider_error_on_enforce_retry_degrades_not_crashes(
    db_session, set_grounding_mode
):
    set_grounding_mode("enforce")
    # First answer carries an invalid marker (no chunks exist), forcing the
    # enforce-mode corrective retry — which raises.
    provider = ScriptThenRaiseProvider(responses=["The dose is 5 mg [1]."])

    result = await handle_chat(db_session, USER, SYMPTOM_Q, provider)

    assert len(provider.calls) == 2
    assert result.response_message == safe_reply(NONE)
    assert result.provenance["path"] == "symptom_rag"
    assert "degraded" not in result.provenance
    assert result.grounding is None

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert len(receipts) == 1
    # The grounding layer itself crashed mid-flight → status "error".
    assert receipts[0].grounding_status == "error"
    assert receipts[0].grounding is None


# --------------------------------------------------------------------------- #
# Orchestrator — HIGH escalation prefix and validator substitution
# --------------------------------------------------------------------------- #
async def test_high_risk_reply_gets_escalation_prefix_and_passes_validation(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    provider = FakeProvider()  # benign default answer, no clinical numbers

    result = await handle_chat(
        db_session, USER, "I have severe chest pain today", provider
    )

    assert result.risk_level == HIGH
    assert result.response_message.startswith(HIGH_ESCALATION)
    # The LLM answer survived (not replaced by the bare safe reply).
    assert "steady habits" in result.response_message
    assert result.recommended_action == "seek_care_promptly"
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"


async def test_validator_failure_substitutes_safe_reply(db_session, set_grounding_mode):
    set_grounding_mode("log")
    provider = FakeProvider(responses=["You probably have diabetes [1]."])

    result = await handle_chat(db_session, USER, SYMPTOM_Q, provider)

    assert result.response_message == safe_reply(NONE)
    assert "probably" not in result.response_message
    assert "[1]" not in result.response_message
    # In log mode the (invalid-marker) answer was kept by grounding; it was the
    # output validator that rejected the diagnostic phrasing.
    assert result.grounding is not None
    assert result.grounding["status"] == "violations"

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert receipts[0].grounding_status == "violations"


async def test_validator_failure_even_when_grounding_clean(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    # No markers and no clinical numbers → grounding passes; validation must
    # still catch the diagnostic assertion independently.
    provider = FakeProvider(responses=["You probably have diabetes."])

    result = await handle_chat(db_session, USER, SYMPTOM_Q, provider)

    assert result.response_message == safe_reply(NONE)
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert receipts[0].grounding_status == "grounded"


# --------------------------------------------------------------------------- #
# Orchestrator — session continuity and session_id on every path
# --------------------------------------------------------------------------- #
async def test_session_continuity_two_turns_share_one_session(db_session):
    provider = FakeProvider()

    r1 = await handle_chat(db_session, USER, "hello", provider)
    assert r1.session_id is not None
    r2 = await handle_chat(
        db_session, USER, "hello again", provider, session_id=r1.session_id
    )
    assert r2.session_id == r1.session_id

    sessions = (await db_session.execute(select(ConversationSession))).scalars().all()
    assert len(sessions) == 1

    messages = (
        await db_session.execute(
            select(ConversationMessage).order_by(ConversationMessage.created_at)
        )
    ).scalars().all()
    assert len(messages) == 4
    assert all(m.session_id == r1.session_id for m in messages)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1].message == r1.response_message
    assert messages[3].message == r2.response_message


async def test_omitting_session_id_creates_distinct_sessions(db_session):
    provider = FakeProvider()
    r1 = await handle_chat(db_session, USER, "hello", provider)
    r2 = await handle_chat(db_session, USER, "hello", provider)
    assert r1.session_id != r2.session_id

    sessions = (await db_session.execute(select(ConversationSession))).scalars().all()
    assert len(sessions) == 2


async def test_caller_supplied_unknown_session_id_is_adopted(db_session):
    provider = FakeProvider()
    supplied = uuid.uuid4()
    result = await handle_chat(
        db_session, USER, "hello", provider, session_id=supplied
    )
    assert result.session_id == supplied
    row = (
        await db_session.execute(
            select(ConversationSession).where(ConversationSession.id == supplied)
        )
    ).scalars().first()
    assert row is not None


async def test_session_id_returned_on_emergency_and_scope_decline(db_session):
    provider = FakeProvider()

    emergency = await handle_chat(
        db_session, USER, "my father is unconscious", provider
    )
    assert emergency.response_message == EMERGENCY_DIRECTIVE
    assert emergency.risk_level == EMERGENCY
    assert emergency.session_id is not None

    decline = await handle_chat(
        db_session, USER, "what is the capital of france", provider
    )
    assert decline.response_message == SCOPE_DECLINE
    assert decline.risk_level == NONE
    assert decline.session_id is not None
    assert decline.session_id != emergency.session_id


# --------------------------------------------------------------------------- #
# Orchestrator — a receipt row for EVERY path, hashes only
# --------------------------------------------------------------------------- #
async def test_receipt_written_for_every_path(db_session, set_grounding_mode):
    set_grounding_mode("log")
    provider = FakeProvider()
    cases = [
        ("he is unconscious and not breathing", "triage_emergency"),
        ("hello there", "conversational"),
        ("show me my insights", "data_query"),
        ("what is the capital of france", "scope_declined"),
        (SYMPTOM_Q, "symptom_rag"),
    ]

    for i, (message, expected_path) in enumerate(cases, start=1):
        result = await handle_chat(db_session, USER, message, provider)
        assert result.provenance["path"] == expected_path
        assert result.session_id is not None
        receipts = (
            await db_session.execute(select(RagTurnReceipt))
        ).scalars().all()
        assert len(receipts) == i  # exactly one new receipt per turn

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    by_hash = {r.query_hash: r for r in receipts}
    assert set(by_hash) == {_sha(m) for m, _ in cases}

    for message, expected_path in cases:
        r = by_hash[_sha(message)]
        assert message not in r.query_hash  # hashes only, never raw text
        assert r.model_name == "fake"
        assert r.session_id is not None
        if expected_path == "symptom_rag":
            assert r.grounding_status == "grounded"
        else:
            assert r.grounding_status == "n/a"
            assert r.used_rag is False


# --------------------------------------------------------------------------- #
# Orchestrator — extracted_intent risk on the stored user message
# --------------------------------------------------------------------------- #
async def test_extracted_intent_risk_recorded_on_user_message(db_session):
    provider = FakeProvider()
    cases = [
        ("she suddenly passed out", EMERGENCY),
        ("I am coughing up blood", HIGH),
        ("hello", NONE),
    ]

    for message, expected_risk in cases:
        result = await handle_chat(db_session, USER, message, provider)
        rows = (
            await db_session.execute(
                select(ConversationMessage).where(
                    ConversationMessage.session_id == result.session_id,
                    ConversationMessage.role == "user",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].extracted_intent == {"risk": expected_risk}
        # Assistant turns persist their structured extras (action, and
        # document cards when present) so restored conversations keep them —
        # never the user's triage internals.
        assistant = (
            await db_session.execute(
                select(ConversationMessage).where(
                    ConversationMessage.session_id == result.session_id,
                    ConversationMessage.role == "assistant",
                )
            )
        ).scalars().all()
        assert len(assistant) == 1
        meta = assistant[0].extracted_intent
        assert meta is not None and "risk" not in meta
        assert meta.get("action")

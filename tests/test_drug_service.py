"""Drug service — deterministic lookup, reply building, and orchestrator wiring.

Covers app/drugs/service.py:
  * extract_drug_query_term — every intent pattern, noise trimming, punctuation,
    non-drug questions, term length bounds.
  * find_drug — exact → prefix → composition strategy order over the
    medicine_master catalogue, whole-word salt matching (the clove/love trap),
    single-ingredient and non-discontinued preferences, deterministic
    tie-breaks, and the <3-char guard.
  * find_substitutes — same-composition alternatives, deterministic order.
  * build_drug_reply — mandatory medication note, validator-safety, list caps,
    habit-forming / discontinued variants.
  * orchestrator integration — drug_query path with no LLM call, fall-through
    to symptom RAG, red-flag risk floor preserved, receipt written.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.chat.replies import HIGH_ESCALATION, MEDICATION_NOTE
from app.chat.validation import validate_reply
from app.drugs.service import (
    _edit_distance,
    build_drug_reply,
    build_interaction_reply,
    extract_drug_query_term,
    extract_interaction_query,
    find_drug,
    find_substitutes,
    suggest_drug,
)
from app.llm.fake import FakeProvider
from app.models.chat import RagTurnReceipt
from app.models.coredata import MedicineMaster
from app.triage.red_flags import EMERGENCY_DIRECTIVE

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _trigger_norm(name: str) -> str:
    # medicine_master's name_normalized trigger:
    # lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g')).
    # PG maintains it; sqlite fixtures must apply the same formula.
    return re.sub(r"[^a-zA-Z0-9]+", " ", name).lower()


def _drug(name: str, **kw) -> MedicineMaster:
    kw.setdefault("name_normalized", _trigger_norm(name))
    kw.setdefault("is_discontinued", False)
    kw.setdefault("status", "approved")
    return MedicineMaster(name=name, **kw)


async def _seed(db, *rows: MedicineMaster) -> None:
    db.add_all(rows)
    await db.flush()


# --------------------------------------------------------------------------- #
# extract_drug_query_term — one test per intent pattern
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # "side effects of X" (singular + plural, embedded in a sentence)
        ("side effects of dolo 650", "dolo 650"),
        ("side effect of paracetamol", "paracetamol"),
        ("can you tell me the side effects of atorvastatin", "atorvastatin"),
        # "what is X used/prescribed for"
        ("what is metformin used for", "metformin"),
        ("what is metformin prescribed for?", "metformin"),
        ("what is crocin tablet used for", "crocin"),
        # "what is/are X tablet(s) for"
        ("what are dolo 650 tablets for", "dolo 650"),
        ("what is ecosprin 75 tablet for", "ecosprin 75"),
        # "substitutes for/of X"
        ("substitutes for augmentin 625", "augmentin 625"),
        ("substitute of atorva 10", "atorva 10"),
        # "alternatives for/to X"
        ("alternatives to dolo 650", "dolo 650"),
        ("alternative for shelcal 500", "shelcal 500"),
        # "is X habit forming" (space + hyphen variants)
        ("is alprazolam habit forming", "alprazolam"),
        ("is alprazolam habit-forming?", "alprazolam"),
        # "tell me about the medicine/medication/drug/tablet X"
        ("tell me about the medicine augmentin", "augmentin"),
        ("tell me about the drug dolo 650", "dolo 650"),
        ("tell me about medication metformin", "metformin"),
        ("tell me about the tablet ecosprin 75", "ecosprin 75"),
        # "about my medicine/medication/tablet X"
        ("about my medication metformin", "metformin"),
        ("about my medicine dolo 650", "dolo 650"),
        ("something about my tablet ecosprin 75", "ecosprin 75"),
    ],
)
def test_extract_patterns(message: str, expected: str):
    assert extract_drug_query_term(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Single trailing filler word
        ("side effects of dolo 650 tablet", "dolo 650"),
        ("side effects of dolo 650 please", "dolo 650"),
        # Iterative trimming: "tablet please" needs two passes
        ("side effects of dolo 650 tablet please", "dolo 650"),
        ("substitutes for azee 500 tablet medicine today", "azee 500"),
        ("what is crocin syrup used for", "crocin"),
        ("side effects of benadryl capsules now", "benadryl"),
    ],
)
def test_extract_trailing_noise_trimmed(message: str, expected: str):
    assert extract_drug_query_term(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("side effects of dolo 650?", "dolo 650"),
        ("side effects of dolo 650.", "dolo 650"),
        ("side effects of dolo 650!!", "dolo 650"),
        ("what are the side effects of Dolo 650?", "Dolo 650"),
        ("is alprazolam habit forming???", "alprazolam"),
    ],
)
def test_extract_punctuation_stripped(message: str, expected: str):
    assert extract_drug_query_term(message) == expected


def test_extract_preserves_case_of_term():
    # Matching is case-insensitive; the captured term keeps the user's casing.
    assert extract_drug_query_term("SIDE EFFECTS OF DOLO 650") == "DOLO 650"


@pytest.mark.parametrize(
    "message",
    [
        "what are the symptoms of malaria",
        "i have a headache and a fever",
        "hello how are you",
        "tell me about diabetes",  # no medicine/drug/tablet keyword
        "what is hypertension",  # no "used/prescribed for" tail
        "my blood sugar is high",
        "",
        "   ",
        "side effects",  # pattern needs "of <term>"
        "substitutes",
    ],
)
def test_extract_non_drug_questions_return_none(message: str):
    assert extract_drug_query_term(message) is None


def test_extract_side_effects_of_activity_still_captures():
    # NOTE: actual behavior — the pattern cannot know "stopping exercise" is not
    # a drug; it captures the phrase and relies on find_drug returning None so
    # the orchestrator falls through to the RAG path.
    assert (
        extract_drug_query_term("side effects of stopping exercise")
        == "stopping exercise"
    )


def test_extract_term_minimum_length():
    # The term group requires at least 3 characters.
    assert extract_drug_query_term("side effects of ab") is None
    assert extract_drug_query_term("side effects of abc") == "abc"


def test_extract_term_maximum_length_truncates():
    # The term group captures at most 61 chars (1 + up to 60).
    long_name = "a" * 100
    got = extract_drug_query_term(f"side effects of {long_name}")
    assert got == "a" * 61
    assert got is not None and len(got) == 61


def test_extract_unicode_term_not_captured():
    # The term charset is ASCII [a-z0-9 .-]; non-Latin scripts don't match.
    assert extract_drug_query_term("side effects of パラセタモール") is None
    assert extract_drug_query_term("side effects of дولо") is None


def test_extract_hyphen_and_digit_terms():
    assert (
        extract_drug_query_term("side effects of co-trimoxazole")
        == "co-trimoxazole"
    )
    assert extract_drug_query_term("side effects of b12 forte") == "b12 forte"


def test_extract_greedy_capture_spans_conjunction():
    # NOTE: actual behavior — the greedy pattern swallows "and paracetamol";
    # such a term simply won't match in find_drug.
    assert (
        extract_drug_query_term("side effects of dolo 650 and paracetamol")
        == "dolo 650 and paracetamol"
    )


# --------------------------------------------------------------------------- #
# find_drug — matching strategies
# --------------------------------------------------------------------------- #
async def test_find_exact_normalized_match(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    hit = await find_drug(db_session, "dolo 650")
    assert hit is not None and hit.name == "Dolo 650"


async def test_find_exact_is_case_and_whitespace_insensitive(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    hit = await find_drug(db_session, "  DOLO \t  650  ")
    assert hit is not None and hit.name == "Dolo 650"


async def test_find_prefix_match(db_session):
    await _seed(db_session, _drug("Augmentin 625 Duo Tablet"))
    hit = await find_drug(db_session, "augmentin")
    assert hit is not None and hit.name == "Augmentin 625 Duo Tablet"


async def test_find_prefix_requires_word_boundary_space(db_session):
    # LIKE "dol %" must not match "dolo 650" (prefix is whole-first-word only).
    await _seed(db_session, _drug("Dolo 650"))
    assert await find_drug(db_session, "dol") is None


async def test_find_exact_preferred_over_prefix(db_session):
    await _seed(db_session, _drug("Dolo"), _drug("Dolo 650"))
    hit = await find_drug(db_session, "dolo")
    assert hit is not None and hit.name == "Dolo"


async def test_find_prefix_preferred_over_composition(db_session):
    await _seed(
        db_session,
        _drug(
            "Glyciphage 500",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
        ),
        _drug("Metformin 500 Tablet"),
    )
    hit = await find_drug(db_session, "metformin")
    assert hit is not None and hit.name == "Metformin 500 Tablet"


async def test_find_composition_whole_word_match(db_session):
    await _seed(
        db_session,
        _drug(
            "Glyciphage 500",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
        ),
    )
    hit = await find_drug(db_session, "metformin")
    assert hit is not None and hit.name == "Glyciphage 500"


async def test_find_composition_substring_trap_clove_love(db_session):
    # "love" is a substring of "clove"; the whole-word check must reject it.
    await _seed(
        db_session,
        _drug(
            "CloCare Gel",
            composition1="Clove Oil (10%)",
            composition_normalized="clove oil (10%)",
        ),
    )
    assert await find_drug(db_session, "love") is None
    # The genuine whole word still matches.
    hit = await find_drug(db_session, "clove")
    assert hit is not None and hit.name == "CloCare Gel"


async def test_find_composition_prefers_single_ingredient(db_session):
    # Combination product has the shorter name; single-ingredient still wins.
    await _seed(
        db_session,
        _drug(
            "Amaryl M",
            composition1="Glimepiride (1mg)",
            composition2="Metformin (500mg)",
            composition_normalized="glimepiride (1mg) + metformin (500mg)",
        ),
        _drug(
            "Glycomet 500 Tablet",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
        ),
    )
    hit = await find_drug(db_session, "metformin")
    assert hit is not None and hit.name == "Glycomet 500 Tablet"


async def test_find_composition_single_ingredient_outranks_discontinued(db_session):
    # NOTE: actual behavior — the sort key puts "composition2 IS NULL" before
    # "not discontinued", so a discontinued single-ingredient product beats an
    # active combination for a salt query.
    await _seed(
        db_session,
        _drug(
            "Amaryl M",
            composition1="Glimepiride (1mg)",
            composition2="Metformin (500mg)",
            composition_normalized="glimepiride (1mg) + metformin (500mg)",
        ),
        _drug(
            "Metold 500",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
            is_discontinued=True,
        ),
    )
    hit = await find_drug(db_session, "metformin")
    assert hit is not None and hit.name == "Metold 500"


async def test_find_composition_prefers_non_discontinued(db_session):
    # Both single-ingredient; the discontinued one has the shorter name.
    await _seed(
        db_session,
        _drug(
            "Met 500",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
            is_discontinued=True,
        ),
        _drug(
            "Glycomet 500 Tablet",
            composition1="Metformin (500mg)",
            composition_normalized="metformin (500mg)",
        ),
    )
    hit = await find_drug(db_session, "metformin")
    assert hit is not None and hit.name == "Glycomet 500 Tablet"


async def test_find_prefix_prefers_non_discontinued(db_session):
    await _seed(
        db_session,
        _drug("Augmentin 375 Tablet", is_discontinued=True),
        _drug("Augmentin 625 Duo Tablet"),
    )
    hit = await find_drug(db_session, "augmentin")
    assert hit is not None and hit.name == "Augmentin 625 Duo Tablet"


async def test_find_prefix_tiebreak_shortest_name(db_session):
    await _seed(
        db_session,
        _drug("Augmentin 625 Duo Tablet"),
        _drug("Augmentin 375 Tablet"),
    )
    hit = await find_drug(db_session, "augmentin")
    assert hit is not None and hit.name == "Augmentin 375 Tablet"


async def test_find_exact_tiebreak_shortest_then_alphabetical(db_session):
    # Same normalized name, different display names: shortest wins.
    await _seed(
        db_session,
        _drug("Acme 10 Long", name_normalized="acme 10"),
        _drug("Acme 10", name_normalized="acme 10"),
    )
    hit = await find_drug(db_session, "acme 10")
    assert hit is not None and hit.name == "Acme 10"


async def test_find_prefix_tiebreak_alphabetical_on_equal_length(db_session):
    await _seed(
        db_session,
        _drug("Zetamol 650 Duo B", name_normalized="zetamol 650 duo b"),
        _drug("Zetamol 650 Duo A", name_normalized="zetamol 650 duo a"),
    )
    hit = await find_drug(db_session, "zetamol")
    assert hit is not None and hit.name == "Zetamol 650 Duo A"


async def test_find_short_term_returns_none_even_if_row_exists(db_session):
    await _seed(db_session, _drug("AB", name_normalized="ab"))
    assert await find_drug(db_session, "ab") is None
    assert await find_drug(db_session, " a ") is None
    assert await find_drug(db_session, "") is None


async def test_find_unknown_term_returns_none(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    assert await find_drug(db_session, "zorbofloxacin") is None


async def test_find_ignores_unapproved_and_deleted_rows(db_session):
    await _seed(
        db_session,
        _drug("Dolo 650", status="pending"),
        _drug("Dolo 650 NF", deleted_at=datetime(2026, 1, 1)),
    )
    assert await find_drug(db_session, "dolo 650") is None


async def test_find_punctuated_query_matches_trigger_normalization(db_session):
    # The trigger collapses punctuation to spaces; the query normalizer must
    # do the same ("co-trimoxazole" → "co trimoxazole").
    await _seed(db_session, _drug("Co-Trimoxazole 480 Tablet"))
    hit = await find_drug(db_session, "co-trimoxazole")
    assert hit is not None and hit.name == "Co-Trimoxazole 480 Tablet"


# --------------------------------------------------------------------------- #
# find_substitutes — same-composition alternatives
# --------------------------------------------------------------------------- #
_COMP = "amoxycillin (500mg) + clavulanic acid (125mg)"


async def test_find_substitutes_deterministic_and_filtered(db_session):
    main = _drug("Augmentin 625 Duo Tablet", composition_normalized=_COMP)
    await _seed(
        db_session,
        main,
        _drug("Moxikind-CV 625", composition_normalized=_COMP),
        _drug("Clavam 625", composition_normalized=_COMP),
        # Excluded: discontinued, unapproved, deleted, different composition.
        _drug("Advent 625", composition_normalized=_COMP, is_discontinued=True),
        _drug("Draft 625", composition_normalized=_COMP, status="draft"),
        _drug(
            "Gone 625",
            composition_normalized=_COMP,
            deleted_at=datetime(2026, 1, 1),
        ),
        _drug("Other Tab", composition_normalized="paracetamol (650mg)"),
    )
    # ORDER BY length(name), name — never the insert order.
    assert await find_substitutes(db_session, main) == [
        "Clavam 625", "Moxikind-CV 625",
    ]


async def test_find_substitutes_capped_at_five(db_session):
    comp = "paracetamol (650mg)"
    main = _drug("Main Tab", composition_normalized=comp)
    await _seed(
        db_session,
        main,
        *[_drug(f"Sub{i} Tab", composition_normalized=comp) for i in range(7)],
    )
    assert await find_substitutes(db_session, main) == [
        f"Sub{i} Tab" for i in range(5)
    ]


async def test_find_substitutes_empty_without_composition(db_session):
    row = _drug("Mystery Tab")
    await _seed(db_session, row)
    assert await find_substitutes(db_session, row) == []


# --------------------------------------------------------------------------- #
# build_drug_reply
# --------------------------------------------------------------------------- #
_SUBSTITUTES = [
    "Moxikind-CV 625", "Clavam 625", "Advent 625", "Mega-CV 625",
    "Warclav 625", "Novamox CV 625", "Extraclav 625",
]


def _full_drug() -> MedicineMaster:
    return _drug(
        "Augmentin 625 Duo Tablet",
        composition1="Amoxycillin (500mg)",
        composition2="Clavulanic Acid (125mg)",
        composition_normalized="amoxycillin (500mg) + clavulanic acid (125mg)",
        used_for=[
            "Treatment of Bacterial infections", "Type 2 diabetes mellitus",
        ],
        side_effects=(
            "Vomiting, Nausea, Diarrhoea, Mouth ulcer, Headache, "
            "Skin rash, Dizziness"
        ),
        habit_forming=False,
    )


def _minimal_drug() -> MedicineMaster:
    return _drug("Mystery Tab")


def test_reply_always_contains_medication_note():
    for drug in (_full_drug(), _minimal_drug()):
        reply = build_drug_reply(drug)
        assert MEDICATION_NOTE in reply
        assert "do not stop or change" in reply


def test_reply_full_drug_passes_validator_at_none_risk():
    reply = build_drug_reply(_full_drug())
    result = validate_reply(reply, "none")
    assert result.ok, result.reason


def test_reply_minimal_drug_passes_validator_at_none_risk():
    reply = build_drug_reply(_minimal_drug())
    result = validate_reply(reply, "none")
    assert result.ok, result.reason


def test_reply_minimal_drug_exact_text():
    reply = build_drug_reply(_minimal_drug())
    assert reply == (
        "Mystery Tab. "
        + MEDICATION_NOTE
        + " This is general information, not medical advice for your specific "
        "situation — your doctor or pharmacist knows your context best."
    )


def test_reply_intro_composition():
    reply = build_drug_reply(_full_drug())
    assert (
        "Augmentin 625 Duo Tablet contains Amoxycillin (500mg), "
        "Clavulanic Acid (125mg)."
    ) in reply
    # No manufacturer talk — the reply reads like drug information, not a
    # product listing.
    assert "manufactured" not in reply


def test_reply_composition2_only():
    drug = _drug("Odd Tab", composition2="Zinc (10mg)")
    reply = build_drug_reply(drug)
    assert "Odd Tab contains Zinc (10mg)." in reply


def test_reply_uses_joined_with_semicolons():
    reply = build_drug_reply(_full_drug())
    assert (
        "It is generally used for: Treatment of Bacterial infections; "
        "Type 2 diabetes mellitus." in reply
    )


def test_reply_side_effects_capped_at_five_and_lowercased():
    reply = build_drug_reply(_full_drug())
    assert (
        "Commonly reported side effects include: "
        "vomiting, nausea, diarrhoea, mouth ulcer, headache." in reply
    )
    # Items 6 and 7 are dropped; the kept items are lowercased.
    assert "skin rash" not in reply.lower()
    assert "dizziness" not in reply.lower()
    assert "Vomiting" not in reply


def test_reply_substitutes_capped_at_five():
    reply = build_drug_reply(_full_drug(), substitutes=_SUBSTITUTES)
    assert (
        "Moxikind-CV 625; Clavam 625; Advent 625; Mega-CV 625; Warclav 625"
        in reply
    )
    assert "Novamox CV 625" not in reply
    assert "Extraclav 625" not in reply
    assert "pharmacist or your doctor can confirm" in reply


def test_reply_falsy_list_entries_filtered():
    drug = _drug("Filter Tab", used_for=["", None], side_effects="")
    reply = build_drug_reply(drug, substitutes=["", None])
    assert "generally used for" not in reply
    assert "side effects" not in reply
    assert "alternatives" not in reply.lower()


def test_reply_habit_forming_yes():
    reply = build_drug_reply(_drug("Alzolam 0.5", habit_forming=True))
    assert "medicine is listed as habit-forming" in reply
    assert "exactly as prescribed" in reply
    assert validate_reply(reply, "none").ok


def test_reply_habit_forming_no():
    reply = build_drug_reply(_drug("Dolo 650", habit_forming=False))
    assert "medicine is not listed as habit-forming" in reply
    assert validate_reply(reply, "none").ok


def test_reply_habit_forming_absent():
    reply = build_drug_reply(_drug("Dolo 650", habit_forming=None))
    assert "habit-forming" not in reply
    assert validate_reply(reply, "none").ok


def test_reply_discontinued_note():
    reply = build_drug_reply(_drug("Old Tab", is_discontinued=True))
    assert "listed as discontinued" in reply
    assert validate_reply(reply, "none").ok
    active = build_drug_reply(_drug("New Tab", is_discontinued=False))
    assert "discontinued" not in active


# --------------------------------------------------------------------------- #
# Orchestrator integration — handle_chat + FakeProvider
# --------------------------------------------------------------------------- #
async def test_orchestrator_drug_query_deterministic_no_llm(db_session):
    row = _full_drug()
    await _seed(db_session, row)
    provider = FakeProvider()

    result = await handle_chat(
        db_session, USER, "side effects of augmentin 625 duo tablet", provider
    )

    assert result.provenance == {
        "path": "drug_query",
        "drug": "Augmentin 625 Duo Tablet",
        "source": "medicine_master",
    }
    assert result.risk_level == "none"
    assert result.recommended_action == "discuss_with_prescriber"
    assert result.response_message == build_drug_reply(row)
    assert MEDICATION_NOTE in result.response_message
    # Fully deterministic: the LLM was never consulted.
    assert provider.calls == []

    # Same question again → byte-identical reply.
    again = await handle_chat(
        db_session, USER, "side effects of augmentin 625 duo tablet", provider
    )
    assert again.response_message == result.response_message
    assert provider.calls == []


async def test_orchestrator_drug_reply_includes_fetched_substitutes(db_session):
    row = _full_drug()
    await _seed(
        db_session, row, _drug("Clavam 625", composition_normalized=_COMP)
    )
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "substitutes for augmentin 625 duo", provider
    )
    assert result.provenance["path"] == "drug_query"
    assert "Clavam 625" in result.response_message
    assert provider.calls == []


async def test_orchestrator_drug_query_via_prefix_match(db_session):
    await _seed(db_session, _drug("Augmentin 625 Duo Tablet"))
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "what is augmentin used for?", provider
    )
    assert result.provenance["path"] == "drug_query"
    assert result.provenance["drug"] == "Augmentin 625 Duo Tablet"
    assert provider.calls == []


async def test_orchestrator_unknown_drug_falls_through_to_rag(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "side effects of zorbofloxacin", provider
    )
    assert result.provenance["path"] == "symptom_rag"
    assert result.provenance["used_rag"] is False
    # The LLM answered exactly once (no drug shortcut, no retry in log mode).
    assert len(provider.calls) == 1
    assert result.response_message == FakeProvider.DEFAULT


async def test_orchestrator_red_flag_overrides_drug_path_high(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    await _seed(db_session, _drug("Dolo 650"))
    provider = FakeProvider()
    message = "I get crushing chest pain — side effects of dolo 650?"

    result = await handle_chat(db_session, USER, message, provider)

    # Risk floor preserved: the drug shortcut only runs at NONE risk.
    assert result.risk_level == "high"
    assert result.provenance["path"] == "symptom_rag"
    assert result.recommended_action == "seek_care_promptly"
    assert result.response_message.startswith(HIGH_ESCALATION)
    assert len(provider.calls) == 1


async def test_orchestrator_red_flag_overrides_drug_path_emergency(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    provider = FakeProvider()
    message = "substitutes for dolo 650 — he passed out and is not breathing"

    result = await handle_chat(db_session, USER, message, provider)

    assert result.risk_level == "emergency"
    assert result.provenance["path"] == "triage_emergency"
    assert result.response_message == EMERGENCY_DIRECTIVE
    assert provider.calls == []


async def test_orchestrator_drug_lookup_failure_fails_open_to_rag(
    db_session, set_grounding_mode, monkeypatch
):
    set_grounding_mode("log")
    await _seed(db_session, _drug("Dolo 650"))

    async def _boom(db, term):
        raise RuntimeError("db exploded")

    # The orchestrator imports find_drug into its own namespace.
    monkeypatch.setattr("app.chat.orchestrator.find_drug", _boom)
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "side effects of dolo 650", provider
    )
    assert result.provenance["path"] == "symptom_rag"
    assert len(provider.calls) == 1


async def test_orchestrator_receipt_written_for_drug_turn(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    provider = FakeProvider()
    message = "side effects of dolo 650"

    result = await handle_chat(db_session, USER, message, provider)
    await db_session.commit()

    receipts = (
        (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    )
    assert len(receipts) == 1
    r = receipts[0]
    assert r.user_id == USER
    assert r.session_id == result.session_id
    assert r.query_hash == hashlib.sha256(message.encode()).hexdigest()
    assert r.model_name == "fake"
    assert r.used_rag is False
    assert r.grounding_status == "n/a"
    assert r.retrieved is None
    assert r.grounding is None
    # Hashes only — the raw message never appears on the receipt.
    assert message not in (r.query_hash + r.model_name + r.grounding_status)


# --------------------------------------------------------------------------- #
# Dose-suffix prefix window ("medrol 4" → "Medrol 4mg Tablet")
# --------------------------------------------------------------------------- #
async def test_find_dose_suffix_without_unit(db_session):
    await _seed(db_session, _drug("Medrol 4mg Tablet"))
    hit = await find_drug(db_session, "Medrol 4")
    assert hit is not None and hit.name == "Medrol 4mg Tablet"


async def test_find_dose_suffix_requires_digit_terminated_term(db_session):
    # Short brand stems must still not swallow unrelated products.
    await _seed(db_session, _drug("Panadol 500 Tablet"))
    assert await find_drug(db_session, "pan") is None


async def test_find_space_prefix_preferred_over_dose_suffix(db_session):
    await _seed(db_session, _drug("Medrol 4 Tablet"), _drug("Medrol 4mg Tablet"))
    hit = await find_drug(db_session, "medrol 4")
    assert hit is not None and hit.name == "Medrol 4 Tablet"


async def test_orchestrator_dose_suffix_drug_query(db_session):
    await _seed(db_session, _drug("Medrol 4mg Tablet"))
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "side effects of Medrol 4?", provider
    )
    assert result.provenance["path"] == "drug_query"
    assert result.provenance["drug"] == "Medrol 4mg Tablet"
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# Interaction / combination questions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Can I take cetirizine and paracetamol together?",
            ("cetirizine", "paracetamol"),
        ),
        ("can i take Gaviscon with alcohol", ("Gaviscon", "alcohol")),
        ("Is it safe to take Sporlac with milk?", ("Sporlac", "milk")),
        (
            "does metformin interact with telmisartan",
            ("metformin", "telmisartan"),
        ),
        (
            "Can I take Dolo 650 and Crocin together?",
            ("Dolo 650", "Crocin"),
        ),
    ],
)
def test_extract_interaction_pairs(message: str, expected: tuple[str, str]):
    assert extract_interaction_query(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "Is it safe to take Sporlac during pregnancy?",
        "Can I take metformin on an empty stomach?",
        "side effects of dolo 650",
        "what should I eat for breakfast",
    ],
)
def test_extract_interaction_non_matches(message: str):
    assert extract_interaction_query(message) is None


def test_interaction_reply_validator_safe_and_has_note():
    reply = build_interaction_reply("Cetirizine 10 Tablet", "Paracetamol 500")
    assert MEDICATION_NOTE in reply
    assert "pharmacist" in reply
    assert validate_reply(reply, "none").ok


async def test_orchestrator_interaction_query_deterministic(db_session):
    await _seed(
        db_session, _drug("Cetirizine 10 Tablet"), _drug("Paracetamol 500")
    )
    provider = FakeProvider()
    result = await handle_chat(
        db_session,
        USER,
        "Can I take cetirizine and paracetamol together?",
        provider,
    )
    assert result.provenance["path"] == "drug_interaction_query"
    # The reply uses the USER'S terms, not canonical product names.
    assert result.provenance["drugs"] == ["cetirizine", "paracetamol"]
    assert result.recommended_action == "discuss_with_prescriber"
    assert MEDICATION_NOTE in result.response_message
    assert provider.calls == []


async def test_orchestrator_interaction_with_alcohol(db_session):
    await _seed(db_session, _drug("Gaviscon Syrup"))
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "Can I take Gaviscon with alcohol?", provider
    )
    assert result.provenance["path"] == "drug_interaction_query"
    assert result.provenance["drugs"] == ["Gaviscon", "alcohol"]
    assert provider.calls == []


async def test_orchestrator_interaction_unknown_terms_fall_through(
    db_session, set_grounding_mode
):
    set_grounding_mode("log")
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "Can I take honey and lemon together?", provider
    )
    # Neither term is a verified medicine → normal LLM path, not the shortcut.
    assert result.provenance["path"] == "symptom_rag"
    assert len(provider.calls) == 1


async def test_orchestrator_interaction_respects_risk_floor(db_session):
    await _seed(db_session, _drug("Dolo 650"))
    provider = FakeProvider()
    result = await handle_chat(
        db_session,
        USER,
        "Can I take dolo 650 and crocin together? Also he passed out.",
        provider,
    )
    assert result.risk_level == "emergency"
    assert result.provenance["path"] == "triage_emergency"
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# suggest_drug — fuzzy catalogue suggestion for misspelled add commands
# --------------------------------------------------------------------------- #
def test_edit_distance_counts_a_transposition_as_one():
    # "dool" -> "dolo" is one slip of the thumb, not two edits.
    assert _edit_distance("dool", "dolo") == 1
    assert _edit_distance("metfromin", "metformin") == 1
    assert _edit_distance("dolo", "dolo") == 0
    assert _edit_distance("dolo", "metformin") > 2


async def test_suggest_fixes_a_transposed_name(db_session):
    # The live case: "add dool 650" must pull up Dolo 650, never store "dool".
    await _seed(db_session, _drug("Dolo 650 Tablet"), _drug("Crocin Advance"))
    hit = await suggest_drug(db_session, "dool 650")
    assert hit is not None and hit.name == "Dolo 650 Tablet"


async def test_suggest_prefers_the_typed_number(db_session):
    await _seed(db_session, _drug("Dolo 500 Tablet"), _drug("Dolo 650 Tablet"))
    hit = await suggest_drug(db_session, "dool 650")
    assert hit is not None and hit.name == "Dolo 650 Tablet"


async def test_suggest_fixes_a_late_typo_via_prefix_shrink(db_session):
    # The first divergence is deep in the word — deletion variants can't reach
    # it, the shrinking-prefix windows can.
    await _seed(db_session, _drug("Metformin 500mg Tablet"))
    hit = await suggest_drug(db_session, "metfromin 500")
    assert hit is not None and hit.name == "Metformin 500mg Tablet"


async def test_suggest_rejects_a_distant_name(db_session):
    await _seed(db_session, _drug("Atorvastatin 10 Tablet"))
    assert await suggest_drug(db_session, "dool 650") is None


async def test_suggest_needs_a_stem_of_four_chars(db_session):
    await _seed(db_session, _drug("Dolo 650 Tablet"))
    assert await suggest_drug(db_session, "dol") is None


async def test_suggest_ignores_unapproved_rows(db_session):
    await _seed(db_session, _drug("Dolo 650 Tablet", status="draft"))
    assert await suggest_drug(db_session, "dool 650") is None


async def test_suggest_is_deterministic_on_ties(db_session):
    # Same distance, same digits: the shorter, alphabetically-first name wins,
    # every time.
    await _seed(db_session, _drug("Doola 650"), _drug("Doolb 650"))
    first = await suggest_drug(db_session, "dool 650")
    second = await suggest_drug(db_session, "dool 650")
    assert first is not None and second is not None
    assert first.name == second.name == "Doola 650"

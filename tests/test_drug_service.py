"""Drug service — deterministic lookup, reply building, and orchestrator wiring.

Covers app/drugs/service.py:
  * extract_drug_query_term — every intent pattern, noise trimming, punctuation,
    non-drug questions, term length bounds.
  * find_drug — exact → prefix → composition strategy order, whole-word salt
    matching (the clove/love trap), single-ingredient and non-discontinued
    preferences, deterministic tie-breaks, and the <3-char guard.
  * build_drug_reply — mandatory medication note, validator-safety, list caps,
    habit-forming / discontinued variants.
  * orchestrator integration — drug_query path with no LLM call, fall-through
    to symptom RAG, red-flag risk floor preserved, receipt written.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.chat.replies import HIGH_ESCALATION, MEDICATION_NOTE
from app.chat.validation import validate_reply
from app.drugs.service import build_drug_reply, extract_drug_query_term, find_drug
from app.llm.fake import FakeProvider
from app.models.chat import RagTurnReceipt
from app.models.knowledge import DrugReference
from app.triage.red_flags import EMERGENCY_DIRECTIVE

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _drug(name: str, **kw) -> DrugReference:
    kw.setdefault("name_normalized", name.lower())
    kw.setdefault("is_discontinued", False)
    return DrugReference(name=name, **kw)


async def _seed(db, *rows: DrugReference) -> None:
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


# --------------------------------------------------------------------------- #
# build_drug_reply
# --------------------------------------------------------------------------- #
def _full_drug() -> DrugReference:
    return _drug(
        "Augmentin 625 Duo Tablet",
        manufacturer="GSK Pharmaceuticals Ltd",
        composition1="Amoxycillin (500mg)",
        composition2="Clavulanic Acid (125mg)",
        composition_normalized="amoxycillin (500mg) + clavulanic acid (125mg)",
        uses=["Treatment of Bacterial infections", "Type 2 diabetes mellitus"],
        side_effects=[
            "Vomiting", "Nausea", "Diarrhoea", "Mouth ulcer", "Headache",
            "Skin rash", "Dizziness",
        ],
        substitutes=[
            "Moxikind-CV 625", "Clavam 625", "Advent 625", "Mega-CV 625",
            "Warclav 625", "Novamox CV 625", "Extraclav 625",
        ],
        habit_forming="No",
    )


def _minimal_drug() -> DrugReference:
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


def test_reply_intro_composition_and_manufacturer():
    reply = build_drug_reply(_full_drug())
    assert (
        "Augmentin 625 Duo Tablet contains Amoxycillin (500mg), "
        "Clavulanic Acid (125mg) (manufactured by GSK Pharmaceuticals Ltd)."
    ) in reply


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
    reply = build_drug_reply(_full_drug())
    assert (
        "Moxikind-CV 625; Clavam 625; Advent 625; Mega-CV 625; Warclav 625"
        in reply
    )
    assert "Novamox CV 625" not in reply
    assert "Extraclav 625" not in reply
    assert "pharmacist or your doctor can confirm" in reply


def test_reply_falsy_list_entries_filtered():
    drug = _drug(
        "Filter Tab",
        uses=["", None],
        side_effects=[None, ""],
        substitutes=["", None],
    )
    reply = build_drug_reply(drug)
    assert "generally used for" not in reply
    assert "side effects" not in reply
    assert "alternatives" not in reply.lower()


@pytest.mark.parametrize("value", ["Yes", "yes", "  YES  "])
def test_reply_habit_forming_yes(value: str):
    reply = build_drug_reply(_drug("Alzolam 0.5", habit_forming=value))
    assert "medicine is listed as habit-forming" in reply
    assert "exactly as prescribed" in reply
    assert validate_reply(reply, "none").ok


@pytest.mark.parametrize("value", ["No", "no", " NO "])
def test_reply_habit_forming_no(value: str):
    reply = build_drug_reply(_drug("Dolo 650", habit_forming=value))
    assert "medicine is not listed as habit-forming" in reply
    assert validate_reply(reply, "none").ok


@pytest.mark.parametrize("value", [None, "", "Unknown"])
def test_reply_habit_forming_absent_or_unrecognized(value):
    reply = build_drug_reply(_drug("Dolo 650", habit_forming=value))
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
        "source": "drug_reference",
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

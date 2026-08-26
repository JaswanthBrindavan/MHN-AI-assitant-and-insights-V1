"""Grounding for claims that carry no numbers.

The old is_factual only matched units and thresholds, so a sentence with no
digits was never grounding-checked at all. "You can stop taking it once you
feel better" — arguably the most dangerous sentence this product could emit —
passed unexamined, because the numeric guards have nothing to look at.

Matched on grammatical SHAPE, not a phrase blocklist: a blocklist is the same
regex treadmill this codebase is trying to leave behind.
"""

from __future__ import annotations

from app.grounding.claims import analyze_grounding, assertion_kind, is_factual


# --------------------------------------------------------------------------- #
# What must be caught
# --------------------------------------------------------------------------- #
def test_a_medication_directive_is_an_assertion():
    assert assertion_kind("You can stop taking it once you feel better.") == "directive"
    assert assertion_kind("You should double the dose if it doesn't help.") == "directive"
    assert assertion_kind("You don't need to continue it.") == "directive"


def test_a_prognosis_stated_as_fact_is_an_assertion():
    assert assertion_kind("Fever usually resolves within a few days.") == "prognostic"
    assert assertion_kind("It will improve on its own.") == "prognostic"
    assert assertion_kind("That is nothing to worry about.") == "prognostic"


def test_assertions_are_factual_and_so_require_a_citation():
    assert is_factual("You can stop taking it once you feel better.")
    assert is_factual("Fever usually resolves within a few days.")


# --------------------------------------------------------------------------- #
# What must NOT be caught
# --------------------------------------------------------------------------- #
def test_routing_the_reader_to_care_is_never_flagged():
    """Blocking this would be the exact opposite of the product's purpose."""
    safe = [
        "You should discuss this with your doctor.",
        "You should stop and speak to your prescriber before changing anything.",
        "You may want to see a clinician about that.",
        "Please contact a pharmacist before you stop taking it.",
        "You need to go to the nearest emergency department right now.",
    ]
    for text in safe:
        assert assertion_kind(text) is None, text


def test_ordinary_prose_is_not_an_assertion():
    ordinary = [
        "Sleep is important for wellbeing.",
        "Many people find this helpful.",
        "Drink water regularly.",
        "This condition affects the small joints of the hand.",
        "Here are three things worth raising at your next visit.",
        "Some people notice it more in the evening.",
    ]
    for text in ordinary:
        assert assertion_kind(text) is None, text


# --------------------------------------------------------------------------- #
# Integration with the grounding verifier
# --------------------------------------------------------------------------- #
def test_an_uncited_directive_is_a_grounding_violation():
    report = analyze_grounding(
        "You can stop taking it once you feel better.",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "violations"
    assert any(v["type"] == "ungrounded_claim" for v in report.violations)


def test_a_cited_directive_is_grounded():
    report = analyze_grounding(
        "Fever usually resolves within a few days [1].",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"


def test_a_reply_that_only_routes_to_care_stays_grounded():
    report = analyze_grounding(
        "That is worth discussing with your doctor at your next visit.",
        num_chunks=1,
        has_patient_context=False,
        retrieval_happened=True,
    )
    assert report.status == "grounded"

"""Phase 4 — red-flag triage floor."""

from __future__ import annotations

import pytest

from app.triage.red_flags import (
    EMERGENCY,
    HIGH,
    NONE,
    max_level,
    triage,
)


@pytest.mark.parametrize(
    "message",
    [
        "he is unconscious and not breathing",
        "I think she is having a seizure",
        "my father's face is drooping and his speech is slurred",
        "I can't breathe",
        "someone is choking",
    ],
)
def test_emergency_phrases(message):
    assert triage(message).level == EMERGENCY


@pytest.mark.parametrize(
    "message",
    [
        "I have severe chest pain",
        "there is blood in my vomit",
        "my lips are turning blue",
        "severe shortness of breath since morning",
        "he seems to have severe confusion",
    ],
)
def test_high_phrases(message):
    assert triage(message).level == HIGH


@pytest.mark.parametrize(
    "message",
    [
        "I have a mild headache",
        "what foods are good for blood sugar?",
        "I feel a little tired today",
    ],
)
def test_none_level(message):
    assert triage(message).level == NONE


def test_acs_co_occurrence_escalates_to_emergency():
    # Chest pain + an associated feature → EMERGENCY (ACS pattern).
    assert triage("I have chest pain and my left arm hurts").level == EMERGENCY
    assert triage("chest tightness with sweating and nausea").level == EMERGENCY


def test_chest_pain_alone_is_high_not_emergency():
    # Plain chest pain with no associated feature does not hit the ACS rule,
    # but it is no longer NONE either: bare chest pain is a HIGH floor
    # (audit fix — it used to sail through to scope/drug/LLM at NONE).
    assert triage("I have some chest pain").level == HIGH


def test_matched_terms_are_deduped_and_sorted():
    result = triage("chest pain, chest pain, and sweating")
    assert result.matched_terms == sorted(set(result.matched_terms))
    assert result.is_emergency


def test_case_insensitive():
    assert triage("I CAN'T BREATHE").level == EMERGENCY


def test_apostrophe_insensitive():
    # Missing/curly apostrophes must not bypass the emergency floor.
    assert triage("i cant breathe").level == EMERGENCY
    assert triage("i can’t breathe").level == EMERGENCY


def test_max_level_never_downgrades():
    assert max_level(EMERGENCY, HIGH) == EMERGENCY
    assert max_level(HIGH, EMERGENCY) == EMERGENCY
    assert max_level(NONE, HIGH) == HIGH
    assert max_level(NONE, NONE) == NONE

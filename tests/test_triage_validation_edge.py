"""Edge-case battery for the red-flag triage floor and post-LLM reply validation.

Pins the full curated phrase tables (parametrized over the imported tuples), the
ACS co-occurrence matrix, normalization quirks, and every banned-phrase /
escalation-marker rule in app.chat.validation. Pure unit tests — no DB, no LLM.
"""

from __future__ import annotations

import pytest

from app.chat.validation import (
    _BANNED_SUBSTRINGS,
    _ESCALATION_MARKERS,
    _dynamic_diagnostic_re,
    find_banned,
    has_escalation,
    validate_reply,
)
from app.triage.red_flags import (
    ACS_ASSOCIATED_PHRASES,
    CHEST_PAIN_PHRASES,
    EMERGENCY,
    EMERGENCY_PHRASES,
    HIGH,
    HIGH_PHRASES,
    NONE,
    max_level,
    triage,
)

REGISTRY = ("Keratoconus", "Telogen Effluvium")


# --------------------------------------------------------------------------- #
# Triage: every curated phrase fires at its tier (pins the whole table)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("phrase", EMERGENCY_PHRASES)
def test_every_emergency_phrase_fires(phrase):
    result = triage(phrase)
    assert result.level == EMERGENCY
    assert result.is_emergency
    assert result.matched
    assert phrase in result.matched_terms


@pytest.mark.parametrize("phrase", EMERGENCY_PHRASES)
def test_every_emergency_phrase_fires_in_sentence(phrase):
    result = triage(f"Please help, my father {phrase} just now.")
    assert result.level == EMERGENCY


@pytest.mark.parametrize("phrase", HIGH_PHRASES)
def test_every_high_phrase_fires_at_high(phrase):
    # Exactly HIGH: no bare HIGH phrase may silently escalate (e.g. via the
    # ACS rule) or fall through to NONE.
    result = triage(phrase)
    assert result.level == HIGH
    assert not result.is_emergency
    assert result.matched
    assert phrase in result.matched_terms


# --------------------------------------------------------------------------- #
# Triage: apostrophe and case normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "I can't breathe",  # straight apostrophe
        "I can’t breathe",  # curly apostrophe
        "i cant breathe",  # no apostrophe at all
    ],
)
def test_apostrophe_variants_all_match(message):
    assert triage(message).level == EMERGENCY


def test_uppercase_matches():
    assert triage("HE IS UNCONSCIOUS").level == EMERGENCY
    assert triage("SEVERE CHEST PAIN").level == HIGH
    assert triage("CHEST PRESSURE AND COLD SWEAT").level == EMERGENCY


def test_spaced_out_apostrophe_is_not_matched():
    # Actual behavior: normalization drops apostrophes but not extra spaces,
    # so "can ' t breathe" normalizes to "can  t breathe" and misses the floor.
    assert triage("I can ' t breathe").level == NONE


# --------------------------------------------------------------------------- #
# Triage: phrases embedded in context
# --------------------------------------------------------------------------- #
def test_phrase_embedded_in_long_paragraph():
    filler = (
        "The week was otherwise uneventful with regular meals, gentle walks in "
        "the park, and good sleep every night. "
    )
    message = filler * 20 + "But tonight his speech is slurred somehow. " + filler * 20
    result = triage(message)
    assert result.level == EMERGENCY
    assert "speech is slurred" in result.matched_terms


def test_multiple_flags_returns_max_tier():
    result = triage("she has severe confusion and now a convulsion has started")
    assert result.level == EMERGENCY
    assert "convulsion" in result.matched_terms
    assert "severe confusion" in result.matched_terms


def test_high_plus_acs_pair_returns_emergency():
    result = triage("crushing chest pain and sweating")
    assert result.level == EMERGENCY
    assert "crushing chest pain" in result.matched_terms
    assert "chest pain" in result.matched_terms
    assert "sweating" in result.matched_terms


# --------------------------------------------------------------------------- #
# Triage: ACS co-occurrence matrix (every pair)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("assoc", ACS_ASSOCIATED_PHRASES)
@pytest.mark.parametrize("chest", CHEST_PAIN_PHRASES)
def test_every_acs_pair_is_emergency(chest, assoc):
    result = triage(f"I have {chest} and also {assoc} since an hour.")
    assert result.level == EMERGENCY
    assert chest in result.matched_terms
    assert assoc in result.matched_terms


@pytest.mark.parametrize("chest", CHEST_PAIN_PHRASES)
def test_chest_pain_phrase_alone_is_none(chest):
    assert triage(chest).level == NONE


@pytest.mark.parametrize("assoc", ACS_ASSOCIATED_PHRASES)
def test_associated_phrase_alone_is_none(assoc):
    assert triage(assoc).level == NONE


def test_negated_chest_pain_still_pairs_by_design():
    # NOTE: the severity floor is intentionally negation-blind. Substring
    # matching treats "no chest pain but sweating" as chest pain + associated
    # feature and escalates to EMERGENCY — over-triage is the safe direction.
    result = triage("no chest pain but sweating a lot")
    assert result.level == EMERGENCY
    assert "chest pain" in result.matched_terms
    assert "sweating" in result.matched_terms


# --------------------------------------------------------------------------- #
# Triage: degenerate and adversarial inputs
# --------------------------------------------------------------------------- #
def test_empty_string_is_none():
    result = triage("")
    assert result.level == NONE
    assert result.matched_terms == []
    assert not result.matched
    assert not result.is_emergency


@pytest.mark.parametrize("message", ["   ", "\n\t  \r\n", "  "])
def test_whitespace_only_is_none(message):
    assert triage(message).level == NONE


def test_emoji_only_is_none():
    assert triage("\U0001f642\U0001f602\U0001f389").level == NONE


def test_emoji_laden_emergency_still_fires():
    result = triage("\U0001f6a8\U0001f6a8 he stopped breathing \U0001f62d\U0001f62d")
    assert result.level == EMERGENCY
    assert "stopped breathing" in result.matched_terms


def test_ten_thousand_char_message_still_returns():
    message = "x" * 5000 + " cardiac arrest " + "y" * 5000
    assert len(message) > 10_000
    result = triage(message)
    assert result.level == EMERGENCY
    assert "cardiac arrest" in result.matched_terms


def test_ten_thousand_char_benign_message_is_none():
    assert triage("z" * 10_000).level == NONE


def test_hindi_text_mixed_with_english_flag():
    result = triage("मेरे पिता को seizure "
                    "आया है, जल्दी मदद")
    assert result.level == EMERGENCY
    assert "seizure" in result.matched_terms


# --------------------------------------------------------------------------- #
# Triage: matched_terms contract and max_level lattice
# --------------------------------------------------------------------------- #
def test_matched_terms_sorted_and_deduped():
    result = triage("Chest pain! chest pain again, sweating, seizure, seizure.")
    assert result.matched_terms == ["chest pain", "seizure", "sweating"]
    assert result.matched_terms == sorted(set(result.matched_terms))


def test_overlapping_phrases_both_reported():
    # "unconsciousness" contains "unconscious" — both table entries surface.
    result = triage("she slipped into unconsciousness")
    assert result.matched_terms == ["unconscious", "unconsciousness"]


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (NONE, NONE, NONE),
        (NONE, HIGH, HIGH),
        (NONE, EMERGENCY, EMERGENCY),
        (HIGH, NONE, HIGH),
        (HIGH, HIGH, HIGH),
        (HIGH, EMERGENCY, EMERGENCY),
        (EMERGENCY, NONE, EMERGENCY),
        (EMERGENCY, HIGH, EMERGENCY),
        (EMERGENCY, EMERGENCY, EMERGENCY),
    ],
)
def test_max_level_all_combinations(a, b, expected):
    assert max_level(a, b) == expected


def test_max_level_unknown_level_raises():
    with pytest.raises(KeyError):
        max_level("bogus", NONE)


def test_high_result_properties():
    result = triage("there is blood in my stool")
    assert result.level == HIGH
    assert result.matched
    assert not result.is_emergency


# --------------------------------------------------------------------------- #
# Validation: banned substrings (pins the whole tuple)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("phrase", _BANNED_SUBSTRINGS)
def test_every_banned_substring_blocks(phrase):
    result = validate_reply(f"I think {phrase}, so let's monitor.", NONE)
    assert not result.ok
    assert result.reason == f"banned:{phrase}"


def test_banned_substring_case_insensitive():
    result = validate_reply("YOUR MEDS ARE CAUSING THIS.", NONE)
    assert not result.ok
    assert result.reason == "banned:your meds are causing"


def test_meds_causal_claim_blocked():
    result = validate_reply("Honestly, your meds are causing this reaction.", NONE)
    assert not result.ok
    assert result.reason == "banned:your meds are causing"


# --------------------------------------------------------------------------- #
# Validation: static diagnostic assertions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reply",
    [
        "You may have diabetes.",
        "Based on all this, you are diabetic.",
        "You've got high blood pressure for sure.",
        "You might have asthma given the wheeze.",
    ],
)
def test_static_diagnostic_assertion_blocked(reply):
    result = validate_reply(reply, NONE)
    assert not result.ok
    assert result.reason == "banned:diagnostic-assertion"


@pytest.mark.parametrize(
    "reply",
    [
        "you have a heart of gold",  # no condition token nearby
        "You have questions, and that's okay.",  # benign "you have"
        "You have a question. Cancer runs in some families.",  # gap can't cross "."
        "Diabetes is a common condition many people manage well.",  # mention, no assertion
    ],
)
def test_benign_you_have_phrasing_not_blocked(reply):
    result = validate_reply(reply, NONE)
    assert result.ok
    assert result.reason == ""


# --------------------------------------------------------------------------- #
# Validation: numeric disease probabilities
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reply",
    [
        "There is a 100% chance of cancer.",
        "There is an 80 % probability of dengue.",  # spaces around %
        "It's a 50%chance at least.",  # no spaces at all
        "Roughly a 5 % likelihood overall.",
        "That carries a 99% risk of complications.",
    ],
)
def test_numeric_probability_blocked(reply):
    result = validate_reply(reply, NONE)
    assert not result.ok
    assert result.reason == "banned:numeric-disease-probability"


def test_probability_with_double_space_slips_through():
    # Actual behavior: \s? allows at most ONE space on each side of "%", so a
    # double space evades the probability regex.
    result = validate_reply("There is an 80  % probability of dengue.", NONE)
    assert result.ok


# --------------------------------------------------------------------------- #
# Validation: dynamic lexicon from registry condition names
# --------------------------------------------------------------------------- #
def test_dynamic_lexicon_blocks_registry_condition():
    # "keratoconus" is NOT in the static condition lexicon: without the
    # registry names this diagnostic assertion passes...
    reply = "You might have keratoconus."
    assert validate_reply(reply, NONE).ok
    # ...and with them it is blocked.
    result = validate_reply(reply, NONE, REGISTRY)
    assert not result.ok
    assert result.reason == "banned:diagnostic-assertion"


def test_probably_have_registry_condition_blocked():
    # "you probably have" is itself a banned substring, so this is blocked even
    # before the dynamic lexicon runs (substring check has precedence).
    result = validate_reply("You probably have keratoconus.", NONE, REGISTRY)
    assert not result.ok
    assert result.reason == "banned:you probably have"


def test_dynamic_matching_is_case_insensitive():
    result = validate_reply("you might have KERATOCONUS today.", NONE, REGISTRY)
    assert not result.ok
    assert result.reason == "banned:diagnostic-assertion"


def test_dynamic_matches_whole_names_only():
    # A partial registry name is not a match ("telogen" alone ≠ the alias).
    assert validate_reply("You might have telogen issues.", NONE, REGISTRY).ok


def test_dynamic_mention_without_assertion_passes():
    reply = "Keratoconus is a corneal condition; an eye doctor can test for it."
    assert validate_reply(reply, NONE, REGISTRY).ok


def test_dynamic_short_names_ignored():
    # Names under 4 chars are dropped from the dynamic lexicon.
    assert validate_reply("You might have flu.", NONE, ("flu", " tb ")).ok
    result = validate_reply("You might have dengue.", NONE, ("flu", "Dengue"))
    assert not result.ok
    assert result.reason == "banned:diagnostic-assertion"


def test_dynamic_empty_tuple_means_no_dynamic_check():
    assert validate_reply("You might have keratoconus.", NONE, ()).ok


def test_dynamic_regex_cached_and_normalized():
    p1 = _dynamic_diagnostic_re(("Dengue", "Typhoid Fever"))
    p2 = _dynamic_diagnostic_re((" typhoid fever ", "DENGUE", "flu"))
    assert p1 is p2  # same normalized name set → same cached pattern
    assert _dynamic_diagnostic_re(("flu", "tb")) is None
    assert _dynamic_diagnostic_re(("   ",)) is None
    assert _dynamic_diagnostic_re(()) is None


# --------------------------------------------------------------------------- #
# Validation: escalation requirement at HIGH / EMERGENCY
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("risk", [HIGH, EMERGENCY])
def test_pure_reassurance_blocked_at_elevated_risk(risk):
    result = validate_reply("Don't worry, this sounds mild. Rest at home and drink fluids.", risk)
    assert not result.ok
    assert result.reason == "missing-escalation"


def test_reassurance_fine_at_none_risk():
    assert validate_reply("Don't worry, this sounds mild. Rest at home.", NONE).ok


@pytest.mark.parametrize("risk", [HIGH, EMERGENCY])
@pytest.mark.parametrize("marker", _ESCALATION_MARKERS)
def test_every_escalation_marker_accepted(marker, risk):
    result = validate_reply(f"You should {marker} without delay.", risk)
    assert result.ok
    assert result.reason == ""


@pytest.mark.parametrize("marker", _ESCALATION_MARKERS)
def test_reply_that_is_only_a_marker_passes_at_high(marker):
    assert validate_reply(marker, HIGH).ok


def test_escalation_marker_case_insensitive():
    assert validate_reply("SEEK IMMEDIATE medical attention.", EMERGENCY).ok


def test_banned_checked_before_escalation():
    # A reply can carry an escalation directive AND banned phrasing — the
    # banned reason wins.
    result = validate_reply("This is likely dengue, seek immediate care.", HIGH)
    assert not result.ok
    assert result.reason == "banned:this is likely"


# --------------------------------------------------------------------------- #
# Validation: empty and degenerate replies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply", ["", "   ", "\n\t", "  "])
def test_empty_and_whitespace_replies_blocked(reply):
    result = validate_reply(reply, NONE)
    assert not result.ok
    assert result.reason == "empty"


def test_empty_beats_missing_escalation_at_high():
    assert validate_reply("", HIGH).reason == "empty"


def test_unknown_risk_level_skips_escalation_check():
    # Only HIGH/EMERGENCY require escalation; any other label behaves like NONE.
    assert validate_reply("Drink water and rest.", "moderate").ok


# --------------------------------------------------------------------------- #
# Validation: helper functions directly
# --------------------------------------------------------------------------- #
def test_find_banned_returns_none_for_benign_text():
    assert find_banned("Stay hydrated and see how you feel tomorrow.") is None


def test_has_escalation_true_and_false():
    assert has_escalation("Please go to the nearest clinic.")
    assert not has_escalation("Everything seems okay for now.")

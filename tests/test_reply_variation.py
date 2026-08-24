"""Reply variation — varied enough to sound human, never varied where it counts.

The safety property here is what is NOT varied: the emergency directive and the
self-harm reply are audited clinical copy. Varying them would mean re-reviewing
every variant with a clinician, and a paraphrase of a helpline instruction is
exactly the kind of change that looks harmless and is not.
"""

from __future__ import annotations

import uuid

from app.chat.replies import (
    _SAFE_NONES,
    EMERGENCY_DIRECTIVE,
    GREETING_REPLIES,
    HIGH_ESCALATION,
    IDENTITY_REPLIES,
    SCOPE_DECLINES,
    SELF_HARM_REPLY,
    pick,
    safe_reply,
)
from app.chat.validation import validate_reply
from app.triage.red_flags import EMERGENCY, HIGH, NONE

ALL_VARIANT_SETS = (
    ("greeting", GREETING_REPLIES),
    ("identity", IDENTITY_REPLIES),
    ("scope_decline", SCOPE_DECLINES),
    ("safe_none", _SAFE_NONES),
)


# --------------------------------------------------------------------------- #
# Every variant must be as safe as the original
# --------------------------------------------------------------------------- #
def test_every_variant_passes_the_validator_at_none_risk():
    for name, variants in ALL_VARIANT_SETS:
        for text in variants:
            verdict = validate_reply(text, NONE)
            assert verdict.ok, f"{name}: {verdict.reason} in {text[:60]!r}"


def test_no_variant_names_a_provider():
    for _name, variants in ALL_VARIANT_SETS:
        for text in variants:
            low = text.lower()
            for banned in ("anthropic", "openai", "claude", "gpt", "llm"):
                assert banned not in low, text[:60]


def test_no_variant_claims_to_be_a_doctor():
    for text in IDENTITY_REPLIES:
        low = text.lower()
        assert "not a doctor" in low or "i'm not a doctor" in low or "clinician" in low


def test_every_set_actually_has_variants():
    for name, variants in ALL_VARIANT_SETS:
        assert len(variants) >= 3, name
        assert len(set(variants)) == len(variants), f"{name} has duplicates"


# --------------------------------------------------------------------------- #
# Selection is deterministic
# --------------------------------------------------------------------------- #
def test_the_same_seed_always_picks_the_same_variant():
    seed = uuid.UUID("11111111-1111-1111-1111-111111111111")
    first = pick(GREETING_REPLIES, seed)
    assert all(pick(GREETING_REPLIES, seed) == first for _ in range(20))


def test_no_seed_falls_back_to_the_first_variant():
    assert pick(GREETING_REPLIES, None) == GREETING_REPLIES[0]


def test_different_seeds_do_spread_across_variants():
    """If every session got variant 0 the whole feature would be inert."""
    seen = {pick(GREETING_REPLIES, uuid.uuid4()) for _ in range(200)}
    assert len(seen) > 1


def test_pick_never_indexes_out_of_range():
    for _ in range(200):
        assert pick(SCOPE_DECLINES, uuid.uuid4()) in SCOPE_DECLINES


# --------------------------------------------------------------------------- #
# What must NOT vary
# --------------------------------------------------------------------------- #
def test_emergency_copy_is_never_varied():
    """Audited clinical copy. One string, always."""
    seeds = [uuid.uuid4() for _ in range(50)]
    assert {safe_reply(EMERGENCY, s) for s in seeds} == {EMERGENCY_DIRECTIVE}


def test_high_risk_copy_is_never_varied():
    seeds = [uuid.uuid4() for _ in range(50)]
    assert {safe_reply(HIGH, s) for s in seeds} == {HIGH_ESCALATION}


def test_the_self_harm_reply_keeps_the_helpline_number():
    assert "14416" in SELF_HARM_REPLY


def test_high_and_emergency_replies_still_carry_an_escalation():
    """The validator REQUIRES this at these levels — a variant that lost it
    would be replaced by a fallback, silently degrading the reply."""
    for level in (HIGH, EMERGENCY):
        assert validate_reply(safe_reply(level), level).ok


def test_safe_reply_at_none_risk_varies_but_stays_valid():
    seeds = [uuid.uuid4() for _ in range(100)]
    replies = {safe_reply(NONE, s) for s in seeds}
    assert len(replies) > 1
    for text in replies:
        assert validate_reply(text, NONE).ok


def test_safe_reply_without_a_seed_is_unchanged():
    """Existing callers that pass no seed keep the exact original string."""
    assert safe_reply(NONE) == _SAFE_NONES[0]

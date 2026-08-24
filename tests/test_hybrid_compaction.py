"""Hybrid compaction — prose alongside the structure, never instead of it.

The load-bearing invariant: a model paraphrase must NEVER sit between the
triage floor and what the pipeline believes. The structured keys come from the
same vocabulary as the safety floor, so if prose could edit them, a summarizer
hallucination would become a safety decision.
"""

from __future__ import annotations

import uuid

from app.chat.conversation import (
    add_message,
    ensure_session,
    latest_summary,
    maybe_compact,
)
from app.chat.memory import STICKY_KEYS
from app.chat.summarize import authoritative_keys, merge_prose, summarize_prose
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn


async def _fill(db_session, session_id, count: int = 30) -> None:
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(db_session, session_id, role, f"filler message {i}.")


# --------------------------------------------------------------------------- #
# The structure stays authoritative
# --------------------------------------------------------------------------- #
def test_merge_prose_never_touches_the_safety_keys():
    structured = {
        "flags": ["passed out"],
        "medications": ["metformin 500 mg"],
        "boundaries": ["i'm not a doctor"],
        "timeline": ["since tuesday"],
        "topics": ["T2DM"],
        "open_questions": [],
    }
    merged = merge_prose(structured, "Some prose about how they are feeling.")
    for key in STICKY_KEYS:
        assert merged[key] == structured[key], key
    assert merged["narrative"]


def test_merge_prose_returns_a_new_dict():
    """Mutating the caller's summary in place would make the failure mode
    invisible."""
    structured = {"flags": ["x"]}
    merged = merge_prose(structured, "prose")
    assert "narrative" not in structured
    assert merged is not structured


def test_no_prose_leaves_the_summary_untouched():
    structured = {"flags": ["x"], "topics": []}
    assert merge_prose(structured, None) == structured


def test_the_authoritative_keys_are_the_sticky_ones():
    """Named so the invariant is testable rather than merely intended."""
    assert authoritative_keys() == STICKY_KEYS


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
async def test_a_summarizer_failure_keeps_the_structure():
    """Prose is a bonus. Losing the structured summary because a model call
    failed would be a real regression."""
    provider = FakeProvider(raises=RuntimeError("provider down"))
    assert await summarize_prose(provider, [{"role": "user", "message": "hi"}]) is None


async def test_an_empty_transcript_produces_no_prose():
    provider = FakeProvider(turns=[LLMTurn(text="should not be called")])
    assert await summarize_prose(provider, []) is None
    assert await summarize_prose(provider, [{"role": "user", "message": "  "}]) is None


async def test_an_empty_model_reply_produces_no_prose():
    provider = FakeProvider(turns=[LLMTurn(text="   ")])
    assert await summarize_prose(provider, [{"role": "user", "message": "hi"}]) is None


async def test_prose_is_length_capped():
    from app.chat.summarize import MAX_SUMMARY_CHARS

    provider = FakeProvider(turns=[LLMTurn(text="x" * 5000)])
    prose = await summarize_prose(provider, [{"role": "user", "message": "hi"}])
    assert prose is not None
    assert len(prose) <= MAX_SUMMARY_CHARS


# --------------------------------------------------------------------------- #
# End to end through maybe_compact
# --------------------------------------------------------------------------- #
async def test_compaction_without_a_provider_is_unchanged(db_session):
    """The deterministic path must keep working exactly as before — the
    provider argument is optional on purpose."""
    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    await _fill(db_session, session_id)

    merged = await maybe_compact(db_session, session_id)
    assert merged is not None
    assert "narrative" not in merged
    for key in STICKY_KEYS:
        assert key in merged


async def test_compaction_with_a_provider_adds_prose(db_session):
    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    await _fill(db_session, session_id)

    provider = FakeProvider(
        turns=[LLMTurn(text="They have been asking about sleep and feel worried.")]
    )
    merged = await maybe_compact(db_session, session_id, provider)
    assert merged is not None
    assert "worried" in merged["narrative"]
    for key in STICKY_KEYS:
        assert key in merged


async def test_a_failing_summarizer_still_compacts(db_session):
    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    await _fill(db_session, session_id)

    provider = FakeProvider(raises=RuntimeError("provider down"))
    merged = await maybe_compact(db_session, session_id, provider)

    assert merged is not None
    assert "narrative" not in merged
    # And it was persisted, not just returned.
    row = await latest_summary(db_session, session_id)
    assert row is not None
    for key in STICKY_KEYS:
        assert key in row.summary


async def test_the_prose_is_persisted_with_the_summary(db_session):
    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    await _fill(db_session, session_id)

    provider = FakeProvider(turns=[LLMTurn(text="A short recollection.")])
    await maybe_compact(db_session, session_id, provider)

    row = await latest_summary(db_session, session_id)
    assert row is not None
    assert row.summary["narrative"] == "A short recollection."


async def test_flags_survive_prose_compaction(db_session):
    """The most important case: a red-flag term detected by the SAME table the
    triage floor uses must still be in the structured summary after a model
    has written prose over the same turns."""
    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    await add_message(db_session, session_id, "user", "I passed out yesterday")
    await _fill(db_session, session_id, 30)

    provider = FakeProvider(
        turns=[LLMTurn(text="They mentioned feeling unwell recently.")]
    )
    merged = await maybe_compact(db_session, session_id, provider)
    assert merged is not None
    assert "passed out" in merged["flags"]

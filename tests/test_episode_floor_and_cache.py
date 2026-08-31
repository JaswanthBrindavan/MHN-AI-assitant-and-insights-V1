"""The unresolved-red-flag floor, agentic citations, and the retrieval cache.

The floor exists because of a live staging turn. A reader had described chest
pain with left-arm discomfort — which `red_flags.py:178`'s ACS co-occurrence
rule classes EMERGENCY — and never said it settled. Days later they asked an
educational question about diabetes and the reply volunteered:

    "By the way, how's the chest pain and left arm discomfort doing now —
     fully settled, or still lingering at all?"

at `risk_level: NONE` with `discuss_with_clinician`. `episodes.worst_level`
existed to prevent exactly that and was never called from anywhere.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.episodes import open_or_touch as record_episode
from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.rag.retrieval import (
    RetrievedChunk,
    _cache_get,
    _cache_put,
    _retrieval_key,
    reset_retrieval_cache,
)
from app.triage.red_flags import EMERGENCY, HIGH, NONE, triage

# --------------------------------------------------------------------------
# The floor
# --------------------------------------------------------------------------

def test_the_original_symptom_pair_really_is_an_emergency():
    """Anchors the premise: without this the floor test proves nothing."""
    assert triage("chest pain and left arm discomfort").level == EMERGENCY


async def test_an_unresolved_emergency_episode_raises_a_later_calm_turn(
    db_session,
):
    user_id = uuid.uuid4()
    await record_episode(
        db_session, user_id, "chest pain and left arm discomfort", EMERGENCY
    )
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "what is diabetes", FakeProvider()
    )

    assert result.risk_level == HIGH, (
        "an unresolved emergency episode must raise a later turn's floor; "
        f"got {result.risk_level}"
    )
    assert result.recommended_action == "seek_care_promptly"


async def test_the_floor_is_capped_at_high_not_emergency(db_session):
    """Deliberate: restoring EMERGENCY would fire the deterministic emergency
    directive on every later turn until the reader said they were better,
    including "what is diabetes", which trains people to ignore it.
    """
    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "chest pain and sweating", EMERGENCY)
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "what is diabetes", FakeProvider()
    )
    assert result.risk_level == HIGH
    assert result.risk_level != EMERGENCY


async def test_a_settled_low_severity_episode_does_not_raise_anything(db_session):
    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "mild headache", NONE)
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "what is diabetes", FakeProvider()
    )
    assert result.risk_level == NONE


async def test_no_episodes_leaves_the_turn_untouched(db_session):
    result = await handle_chat(
        db_session, uuid.uuid4(), "what is diabetes", FakeProvider()
    )
    assert result.risk_level == NONE


async def test_the_current_message_can_still_raise_above_the_episode_floor(
    db_session,
):
    """The floor only ever RAISES. A real emergency now still wins."""
    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "mild headache", NONE)
    await db_session.flush()

    # NB "crushing chest pain" alone measures HIGH; only the ACS
    # co-occurrence rule (chest pain PLUS an associated feature) reaches
    # EMERGENCY. Using a phrase that genuinely does.
    result = await handle_chat(
        db_session, user_id, "chest pain and left arm discomfort", FakeProvider()
    )
    assert result.risk_level == EMERGENCY


# --------------------------------------------------------------------------
# Retrieval cache
# --------------------------------------------------------------------------

def _chunk(section: str) -> RetrievedChunk:
    return RetrievedChunk(
        id=section, condition_code="MC001", chunk_type=section,
        content=f"Diabetes mellitus — {section}:\nSome reviewed content here.",
        score=0.5,
    )


def test_the_cache_normalises_whitespace_and_case():
    """"What is  Diabetes " and "what is diabetes" are the same lookup."""
    a = _retrieval_key({"MC001"}, "What is  Diabetes ", 4)
    b = _retrieval_key({"MC001"}, "what is diabetes", 4)
    assert a == b


def test_the_cache_key_does_not_store_the_readers_words():
    """A health question is the reader's own words; the key is a digest."""
    key = _retrieval_key({"MC001"}, "why do i keep getting dizzy", 4)
    assert "dizzy" not in repr(key)


def test_a_different_scope_is_a_different_entry():
    assert _retrieval_key({"MC001"}, "what is it", 4) != _retrieval_key(
        {"MC002"}, "what is it", 4
    )


def test_a_different_k_is_a_different_entry():
    assert _retrieval_key({"MC001"}, "what is it", 4) != _retrieval_key(
        {"MC001"}, "what is it", 8
    )


def test_a_hit_returns_a_copy_so_callers_cannot_corrupt_it():
    reset_retrieval_cache()
    key = _retrieval_key({"MC001"}, "what is diabetes", 4)
    _cache_put(key, [_chunk("definition"), _chunk("symptoms")])

    first = _cache_get(key)
    assert first is not None
    first.pop()  # a caller slicing/reordering its own list

    second = _cache_get(key)
    assert second is not None
    assert len(second) == 2, "a caller mutating its result corrupted the cache"


def test_reset_clears_everything():
    key = _retrieval_key({"MC001"}, "what is diabetes", 4)
    _cache_put(key, [_chunk("definition")])
    assert _cache_get(key) is not None
    reset_retrieval_cache()
    assert _cache_get(key) is None


def test_the_cache_is_bounded():
    from app.rag.retrieval import RETRIEVAL_CACHE_MAX_ENTRIES, _retrieval_cache

    reset_retrieval_cache()
    for i in range(RETRIEVAL_CACHE_MAX_ENTRIES + 50):
        _cache_put(_retrieval_key({"MC001"}, f"question number {i}", 4),
                   [_chunk("definition")])
    assert len(_retrieval_cache) <= RETRIEVAL_CACHE_MAX_ENTRIES


def test_an_expired_entry_is_a_miss(monkeypatch):
    import app.rag.retrieval as r

    reset_retrieval_cache()
    key = _retrieval_key({"MC001"}, "what is diabetes", 4)
    _cache_put(key, [_chunk("definition")])
    assert _cache_get(key) is not None

    later = [0.0]
    real = r.time.monotonic
    monkeypatch.setattr(
        r.time, "monotonic",
        lambda: real() + r.RETRIEVAL_CACHE_TTL_SECONDS + 1 + later[0],
    )
    assert _cache_get(key) is None


@pytest.mark.parametrize("second_call_should_hit_db", [False])
async def test_repeated_retrieval_does_not_touch_the_database_again(
    db_session, engine, second_call_should_hit_db
):
    """The point of the cache: the same question costs no round trips twice."""
    from sqlalchemy import event

    from app.models.chat import McpChunk
    from app.rag.retrieval import retrieve_chunks

    reset_retrieval_cache()
    db_session.add(
        McpChunk(
            condition_code="MC001", chunk_type="definition",
            content="Diabetes mellitus — definition:\nA chronic metabolic "
                    "condition affecting blood glucose regulation over time.",
        )
    )
    await db_session.flush()

    first = await retrieve_chunks(db_session, {"MC001"}, "what is diabetes")
    assert first, "seeded chunk should be retrievable"

    counter = {"n": 0}

    def _count(*_a, **_kw):
        counter["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        second = await retrieve_chunks(db_session, {"MC001"}, "what is diabetes")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    assert [c.id for c in second] == [c.id for c in first]
    assert counter["n"] == 0, (
        f"cached retrieval still issued {counter['n']} queries"
    )

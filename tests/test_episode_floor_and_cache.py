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
        db_session, user_id, "should i be worried about this", FakeProvider()
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
        db_session, user_id, "should i be worried about this", FakeProvider()
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


# --------------------------------------------------------------------------
# The reader must be able to CLEAR an escalation they raised
#
# Reproduces a staging session verbatim. "chest pain and left arm discomfort"
# opened THREE episodes (chest pain / chest pain (pattern) / left arm), the
# close-out only fired when exactly one was open, so "its gone.. i am feeling
# better" resolved nothing and every later turn — including "what is diabetes?"
# — kept the seek-care banner for fourteen days.
# --------------------------------------------------------------------------

async def test_a_rule_label_never_becomes_an_episode(db_session):
    """"chest pain (pattern)" names the ACS rule, not something the reader said."""
    from app.chat import memory_assembly
    from app.chat.episodes import open_episodes

    user_id = uuid.uuid4()
    await memory_assembly.record(
        db_session, user_id,
        message="chest pain and left arm discomfort",
        risk=EMERGENCY,
        flags=["chest pain", "chest pain (pattern)", "left arm"],
    )
    await db_session.flush()

    symptoms = {e.symptom for e in await open_episodes(db_session, user_id)}
    assert "chest pain (pattern)" not in symptoms
    assert "chest pain" in symptoms


async def test_saying_you_are_better_clears_a_multi_row_incident(db_session):
    from app.chat import memory_assembly
    from app.chat.episodes import open_episodes

    user_id = uuid.uuid4()
    await memory_assembly.record(
        db_session, user_id,
        message="chest pain and left arm discomfort",
        risk=EMERGENCY,
        flags=["chest pain", "left arm"],
    )
    await db_session.flush()
    assert len(await open_episodes(db_session, user_id)) == 2, "premise"

    await memory_assembly.record(
        db_session, user_id,
        message="its gone.. i am feeling better",
        risk=NONE,
        flags=[],
    )
    await db_session.flush()

    assert await open_episodes(db_session, user_id) == [], (
        "a recovery report must clear the whole incident, not require the "
        "reader to name each row"
    )


async def test_the_full_staging_sequence_ends_calm(db_session):
    """The three turns as they were actually typed."""
    user_id = uuid.uuid4()

    first = await handle_chat(
        db_session, user_id, "chest pain and left arm discomfort", FakeProvider()
    )
    assert first.risk_level == EMERGENCY

    await handle_chat(
        db_session, user_id, "its gone.. i am feeling better", FakeProvider()
    )

    third = await handle_chat(
        db_session, user_id, "what is diabetes?", FakeProvider()
    )
    assert third.risk_level == NONE, (
        "after the reader says they are better, an unrelated question must "
        f"not still carry the escalation; got {third.risk_level}"
    )
    assert third.recommended_action == "discuss_with_clinician"


async def test_a_carried_escalation_does_not_claim_the_reader_described_it(
    db_session,
):
    """"Some of what you describe can be serious" is false when they described
    nothing this turn — they asked what diabetes is."""
    from app.chat.replies import CARRIED_ESCALATION, HIGH_ESCALATION

    user_id = uuid.uuid4()
    await record_episode(
        db_session, user_id, "chest pain and left arm discomfort", EMERGENCY
    )
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "should i be worried about this", FakeProvider()
    )
    assert result.risk_level == HIGH
    assert not result.response_message.startswith(HIGH_ESCALATION)
    assert result.response_message.startswith(CARRIED_ESCALATION)


async def test_a_symptom_described_now_still_gets_the_direct_wording(db_session):
    from app.chat.replies import HIGH_ESCALATION

    result = await handle_chat(
        db_session, uuid.uuid4(), "i have had a bad headache for three days",
        FakeProvider(),
    )
    if result.risk_level == HIGH:
        assert result.response_message.startswith(HIGH_ESCALATION)


# --------------------------------------------------------------------------
# Chrome-session findings
# --------------------------------------------------------------------------

def test_a_colloquial_drug_reference_still_triggers_the_refusal():
    """Found by driving the deployed chat as a real user.

    "can i take metformin with my bp tablet" returned None from the extractor,
    so `_interaction_refusal` never fired, the turn reached the agentic engine,
    and the model answered from its own weights — "commonly prescribed
    together ... generally considered routine" — about the reader's REAL
    prescriptions, from a catalogue holding no interaction data at all. Naming
    both drugs was refused correctly, so the gap was purely in the phrasing.

    Cause: the noise strippers reduce "my bp tablet" to "bp", and a
    3-character minimum then discarded the whole match.
    """
    from app.drugs.service import extract_interaction_query

    for message in (
        "can i take metformin with my bp tablet",
        "can i take metformin with my bp tablet?",
        "can i take dolo with my bp tablet",
    ):
        assert extract_interaction_query(message) is not None, message


def test_the_refusal_echoes_the_readers_own_words_when_cleaning_strips_too_much():
    from app.drugs.service import extract_interaction_query

    pair = extract_interaction_query("can i take metformin with my bp tablet")
    assert pair is not None
    assert pair[0] == "metformin"
    assert "bp" in pair[1]


def test_together_is_not_part_of_the_drug_name():
    """The reply read "telmisartan together can be taken together"."""
    from app.drugs.service import extract_interaction_query

    pair = extract_interaction_query(
        "is it ok to take metformin and telmisartan together"
    )
    assert pair == ("metformin", "telmisartan")


def test_an_ordinary_question_is_still_not_an_interaction_query():
    from app.drugs.service import extract_interaction_query

    for message in (
        "whats a normal blood sugar level",
        "what is diabetes",
        "what are the symptoms of diabetes",
    ):
        assert extract_interaction_query(message) is None, message


async def test_citations_drop_a_condition_only_carried_from_an_earlier_turn(
    db_session,
):
    """Staging cited MC051 (hypertension) four times on a diabetes question.

    An earlier turn about a blood-pressure tablet had pulled MC051 into scope;
    the model ignored it; the citation list reported it anyway. A citation the
    answer plainly did not use tells the reader the wrong profile was consulted.
    """
    from app.chat.orchestrator import _agentic_citations
    from app.rag.retrieval import RetrievedChunk

    chunks = [
        RetrievedChunk(id="a", condition_code="MC001", chunk_type="symptoms",
                       content="Diabetes — symptoms:\nThirst.", score=0.9),
        RetrievedChunk(id="b", condition_code="MC051", chunk_type="definition",
                       content="Hypertension — definition:\nHigh BP.", score=0.4),
    ]
    cites = await _agentic_citations(db_session, chunks, carried={"MC051"})
    assert cites is not None
    assert {c["condition_code"] for c in cites} == {"MC001"}


async def test_citations_carry_a_label_a_reader_can_read(db_session):
    """A client rendering one field showed "MC051" four times."""
    from app.chat.orchestrator import _agentic_citations
    from app.rag.retrieval import RetrievedChunk

    cites = await _agentic_citations(
        db_session,
        [RetrievedChunk(id="a", condition_code="MC001", chunk_type="symptoms_2",
                        content="Diabetes — symptoms:\nThirst.", score=0.9)],
    )
    assert cites is not None
    assert "symptoms" in cites[0]["label"]
    assert "symptoms_2" not in cites[0]["label"]


async def test_citations_never_end_up_empty_when_everything_was_carried(
    db_session,
):
    """Filtering must not silently remove the evidence that a lookup happened."""
    from app.chat.orchestrator import _agentic_citations
    from app.rag.retrieval import RetrievedChunk

    chunks = [
        RetrievedChunk(id="b", condition_code="MC051", chunk_type="definition",
                       content="Hypertension — definition:\nHigh BP.", score=0.4),
    ]
    cites = await _agentic_citations(db_session, chunks, carried={"MC051"})
    assert cites and cites[0]["condition_code"] == "MC051"


# --------------------------------------------------------------------------
# Audit finding C6 — a dietary idiom fired the self-harm crisis reply
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "I have been cutting down on sugar",
    "im cutting back on salt",
    "trying to cut out sweets",
    "i cut down on carbs",
    "should i cut down on rice",
])
def test_cutting_down_on_food_is_not_self_harm(message):
    """"been cutting" matched "I have been cutting down on sugar".

    A reader asking a diet question got the self-harm crisis reply with a
    helpline number — and, once the episode floor landed, a 14-day EMERGENCY
    episode pinning every later turn to seek-care.
    """
    result = triage(message)
    assert result.self_harm is False, message
    assert result.level == NONE, message


@pytest.mark.parametrize("message", [
    "i have been cutting myself",
    "i keep cutting myself",
    "i want to hurt myself",
    "i cut my wrists",
    "i dont want to live",
    "i have been cutting my arms",
])
def test_real_self_harm_disclosures_still_fire(message):
    """The guard must be narrow. Recall on this table is the whole point."""
    result = triage(message)
    assert result.self_harm is True, message
    assert result.level == EMERGENCY, message


# --------------------------------------------------------------------------
# Reported by a reader: the escalation sentence led EVERY reply
#
# "its not necessary to keep on repeating the same thing again and again ...
#  even after i said i doing fine ... we can just ask that once in a while
#  rather than for every response. And when user says he is doing fine then
#  the symptoms should marked as inactive."
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "im doing fine",
    "i am fine now",
    "im fine",
    "im okay now",
    "nothing hurts now",
    "all settled",
    "back to normal",
])
def test_the_ways_people_actually_say_they_are_better(message):
    """None of these was recognised, so nothing was ever marked inactive."""
    from app.chat.episodes import is_recovery_message

    assert is_recovery_message(message) is True, message


@pytest.mark.parametrize("message", [
    "im fine but i have chest pain",
    "feeling ok but my chest hurts",
])
def test_a_message_that_reports_a_flag_is_not_a_recovery(message):
    """The guard the loose phrasings need. Silencing a real report is the one
    failure this table must never produce."""
    from app.chat.episodes import is_recovery_message

    assert is_recovery_message(message, has_red_flag=True) is False, message


async def test_saying_you_are_fine_closes_the_episode(db_session):
    from app.chat.episodes import open_episodes

    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "chest pain", EMERGENCY)
    await db_session.flush()

    await handle_chat(db_session, user_id, "im doing fine now", FakeProvider())
    assert await open_episodes(db_session, user_id) == []


async def test_a_corpus_lookup_does_not_re_escalate(db_session):
    """The reader asked what a word means. Repeating an urgent instruction
    there is how it stops being read."""
    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "chest pain", EMERGENCY)
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "what is prediabetes", FakeProvider()
    )
    assert result.risk_level == NONE
    assert "seek medical care" not in result.response_message.lower()


async def test_a_personal_turn_still_re_escalates(db_session):
    """The cut must not silence the turns where it can change what they do."""
    user_id = uuid.uuid4()
    await record_episode(db_session, user_id, "chest pain", EMERGENCY)
    await db_session.flush()

    result = await handle_chat(
        db_session, user_id, "should i be worried about this", FakeProvider()
    )
    assert result.risk_level == HIGH

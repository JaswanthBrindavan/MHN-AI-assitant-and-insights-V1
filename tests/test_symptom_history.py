"""Symptoms are recorded whether they are still active or not.

`symptom_logs` was declared in V6, mapped in `app/models/chat.py` and swept by
`erasure.py` — and nothing had ever written a row. Only `active_symptom_states`
was written, and that is deleted on recovery and filtered as stale after two
weeks, so the moment a reader got better their history vanished with the
episode. "What symptoms have I reported?" was unanswerable because nothing was
ever recorded.

Two tables, two questions, and both are written in `open_or_touch` so no caller
can record one without the other:

* `active_symptom_states` — what is open NOW, and what raises the risk floor.
* `symptom_logs` — the append-only history, kept either way.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.chat.episodes import history, open_or_touch, resolve
from app.models.chat import ActiveSymptomState, SymptomLog
from app.models.common import utcnow

USER = uuid.UUID("aaaa1111-2222-3333-4444-555566667777")
OTHER = uuid.UUID("aaaa1111-2222-3333-4444-555566667778")


async def _logged(db, user=USER):
    return (await db.execute(
        select(SymptomLog).where(SymptomLog.user_id == user)
        .order_by(SymptomLog.created_at)
    )).scalars().all()


async def test_a_mention_is_written_to_both_tables(db_session):
    await open_or_touch(db_session, USER, "headache", "low", ["headache"])

    rows = await _logged(db_session)
    assert len(rows) == 1
    assert rows[0].symptom == "headache"
    assert rows[0].risk_level == "low"

    active = (await db_session.execute(
        select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
    )).scalars().all()
    assert len(active) == 1


async def test_the_log_appends_where_the_episode_is_touched(db_session):
    """One episode, three reports. The episode is ONE row updated in place;
    the history is three, because what was said each time is the history."""
    for _ in range(3):
        await open_or_touch(db_session, USER, "headache", "low", ["headache"])

    assert len(await _logged(db_session)) == 3
    active = (await db_session.execute(
        select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
    )).scalars().all()
    assert len(active) == 1


async def test_history_survives_recovery(db_session):
    """The point of the change. Before this, saying "I'm better" erased the
    only record that the symptom had ever been reported."""
    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    await resolve(db_session, USER, "headache")

    got = await history(db_session, USER)
    assert [s.symptom for s in got] == ["headache"]
    assert got[0].active is False, "resolved, but still part of the history"


async def test_an_open_symptom_is_marked_active(db_session):
    await open_or_touch(db_session, USER, "chest pain", "high", ["chest pain"])
    got = await history(db_session, USER)
    assert got[0].active is True


async def test_active_and_inactive_are_reported_together(db_session):
    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    await resolve(db_session, USER, "headache")
    await open_or_touch(db_session, USER, "cough", "low", ["cough"])

    got = {s.symptom: s.active for s in await history(db_session, USER)}
    assert got == {"headache": False, "cough": True}


async def test_the_count_and_last_seen_are_per_symptom(db_session):
    await open_or_touch(db_session, USER, "cough", "low", ["cough"])
    await open_or_touch(db_session, USER, "cough", "low", ["cough"])
    await open_or_touch(db_session, USER, "fever", "low", ["fever"])

    got = {s.symptom: s.times for s in await history(db_session, USER)}
    assert got == {"cough": 2, "fever": 1}


async def test_the_co_occurring_terms_are_kept(db_session):
    """"chest pain" beside "left arm" is a different report from "chest pain"
    alone, and the pair is what a clinician would want to see."""
    await open_or_touch(
        db_session, USER, "chest pain", "emergency",
        ["chest pain", "left arm", "chest pain (pattern)"],
    )
    rows = await _logged(db_session)
    assert rows[0].matched_terms == [
        "chest pain", "left arm", "chest pain (pattern)"
    ]


async def test_history_is_owner_scoped(db_session):
    await open_or_touch(db_session, OTHER, "headache", "low", ["headache"])
    assert await history(db_session, USER) == []


async def test_the_limit_bounds_the_answer_not_the_evidence(db_session):
    """Grouping happens in SQL. A row cap would silently drop the oldest half
    of a heavy user's history before the counting even started."""
    for i in range(5):
        for _ in range(4):
            await open_or_touch(db_session, USER, f"symptom {i}", "low", [])

    got = await history(db_session, USER, limit=2)
    assert len(got) == 2
    assert all(s.times == 4 for s in got), "counts computed over ALL rows"


async def test_a_blank_symptom_records_nothing(db_session):
    await open_or_touch(db_session, USER, "   ", "low", [])
    assert await _logged(db_session) == []


# --------------------------------------------------------------------------- #
# Retention — append-only means nothing else would ever bound it
# --------------------------------------------------------------------------- #
async def test_old_symptom_rows_are_purged(db_session):
    """One row per mention, growing with use. It is neither transcript (no
    message text) nor audit (it is about the reader, not the system), so it
    gets its own window rather than borrowing one of theirs."""
    from app.chat.retention import purge_expired

    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    rows = await _logged(db_session)
    rows[0].created_at = utcnow() - timedelta(days=500)
    await db_session.flush()
    await open_or_touch(db_session, USER, "cough", "low", ["cough"])

    counts = await purge_expired(
        db_session, message_days=180, receipt_days=400, symptom_days=400,
        batch_size=100,
    )
    assert counts["symptoms_purged"] == 1
    assert [r.symptom for r in await _logged(db_session)] == ["cough"]


async def test_an_erasure_request_still_takes_the_history(db_session):
    """The table was already in the erasure sweep before anything wrote to it.
    Now that it holds data, that has to actually be true."""
    from app.chat.erasure import _ERASE_IN_ORDER

    assert any(name == "symptom_logs" for name, _ in _ERASE_IN_ORDER)


async def test_a_stale_episode_reads_as_inactive(db_session):
    """`open_episodes` filters episodes older than STALE_AFTER on read rather
    than deleting them. The history has to apply the SAME rule, or the floor
    treats a symptom as closed while the summary calls it still open."""
    from app.chat.episodes import STALE_AFTER

    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    active = (await db_session.execute(
        select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
    )).scalars().all()
    active[0].last_seen_at = utcnow() - STALE_AFTER - timedelta(days=1)
    await db_session.flush()

    got = await history(db_session, USER)
    assert got[0].active is False


# --------------------------------------------------------------------------- #
# The reader can actually ask for it
# --------------------------------------------------------------------------- #
# The health summary is "everything on the reader's record", it is served
# VERBATIM, and it is reachable from BOTH engines through the shared prologue
# and a tool. Putting the history there needs no new parser and no new route —
# which matters, because a guard or a reader whose reachability depends on a
# phrase matching is the failure that keeps recurring in this codebase.
async def test_the_summary_reports_symptoms_active_and_not(db_session):
    from app.chat.data_handlers import handle_summary_query

    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    await resolve(db_session, USER, "headache")
    await open_or_touch(db_session, USER, "cough", "low", ["cough"])

    out = await handle_summary_query(db_session, USER, "health summary")
    assert out is not None
    reply = out["reply"]
    assert "headache" in reply and "cough" in reply
    assert "still open" in reply, "the reader should see what is still carried"


async def test_the_summary_reports_them_as_mentions_not_findings(db_session):
    """"You mentioned" is what happened. "You have" would be a diagnosis made
    out of a chat log."""
    from app.chat.data_handlers import handle_summary_query

    await open_or_touch(db_session, USER, "headache", "low", ["headache"])
    out = await handle_summary_query(db_session, USER, "health summary")
    assert out is not None
    assert "Symptoms you have mentioned" in out["reply"]


# --------------------------------------------------------------------------- #
# Nothing reports a symptom without it being recorded
# --------------------------------------------------------------------------- #
# The bypass class, applied to the write side: a handler that answers and
# returns BEFORE the recording step would drop that symptom from the history
# silently, and the suite would stay green because every other path records.
#
# Checked by hand at the time: of the five early `return ChatResult` in the
# shared prologue, one (emergency) records, and the other four cannot be
# reached with a symptom present — the medication flow and the about-me lookup
# are gated on NONE, the scope decline on `not tr.matched`, and `route()`
# returns SYMPTOM_RAG whenever triage matched, so a conversational or identity
# reply is unreachable. These tests keep that true rather than trusting it.
async def test_an_emergency_turn_records_the_symptom(db_session):
    """The severity most worth remembering, on the path that answers WITHOUT
    the model and returns early."""
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    await handle_chat(
        db_session, user, "i have crushing chest pain and my left arm hurts",
        FakeProvider(),
    )
    rows = await _logged(db_session, user)
    assert rows, "an emergency was answered and never recorded"


async def test_an_ordinary_symptom_turn_records_it(db_session):
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    await handle_chat(
        db_session, user, "i have had a headache for three days", FakeProvider()
    )
    assert await _logged(db_session, user), "an ordinary symptom went unrecorded"


async def test_an_identity_question_cannot_swallow_a_symptom(db_session):
    """`route()` returns SYMPTOM_RAG whenever triage matched, so the canned
    identity reply — which returns early and does not record — is unreachable
    with a symptom in the message. If that ordering ever changes, a reader
    could report chest pain and be answered about the assistant instead."""
    from app.chat.router import CONVERSATIONAL, route

    assert route("who are you", triage_matched=False) == CONVERSATIONAL
    assert route("who are you, i have chest pain", triage_matched=True) != (
        CONVERSATIONAL
    )


# --------------------------------------------------------------------------- #
# The ordinary-symptom table must never touch the floor
# --------------------------------------------------------------------------- #
# This is the whole reason it is a SECOND table rather than a fourth tier in
# triage. Recording a headache must not open an episode, must not raise the
# risk level, and must not put a seek-care banner on the next fourteen turns —
# an escalation the reader cannot escape is how a real warning gets trained
# into wallpaper.
async def test_an_ordinary_symptom_opens_no_episode(db_session):
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    await handle_chat(
        db_session, user, "i have a headache and some acidity", FakeProvider()
    )

    assert await _logged(db_session, user), "recorded in the history"
    active = (await db_session.execute(
        select(ActiveSymptomState).where(ActiveSymptomState.user_id == user)
    )).scalars().all()
    assert active == [], "an everyday complaint must not open an episode"


async def test_an_ordinary_symptom_does_not_raise_the_floor(db_session):
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider
    from app.triage.red_flags import NONE

    user = uuid.uuid4()
    result = await handle_chat(
        db_session, user, "i have had a headache for three days", FakeProvider()
    )
    assert result.risk_level == NONE


async def test_the_two_vocabularies_do_not_overlap(db_session):
    """A phrase in both tables would be recorded twice for one complaint and,
    worse, would blur which table decides severity."""
    from app.triage.red_flags import (
        EMERGENCY_PHRASES,
        HIGH_PHRASES,
        ORDINARY_SYMPTOM_PHRASES,
    )

    flags = {p.lower() for p in EMERGENCY_PHRASES + HIGH_PHRASES}
    overlap = flags & {p.lower() for p in ORDINARY_SYMPTOM_PHRASES}
    assert not overlap, f"phrases in both the floor and the history: {overlap}"


async def test_a_red_flag_is_recorded_once_not_twice(db_session):
    """Both writers run on the same turn. A message carrying a red flag AND an
    ordinary symptom must produce one row each, not a duplicate of the flag."""
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    await handle_chat(
        db_session, user,
        "i have crushing chest pain and my left arm hurts, also a headache",
        FakeProvider(),
    )
    rows = await _logged(db_session, user)
    assert len(rows) == len({r.symptom for r in rows}), (
        f"duplicate symptom rows: {[r.symptom for r in rows]}"
    )

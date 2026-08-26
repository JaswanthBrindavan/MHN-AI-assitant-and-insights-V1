"""utcnow() must be strictly increasing — message ordering depends on it.

conversation_messages is ordered by (created_at, id) in six places, and `id`
is a random uuid4. If two rows share created_at the tiebreak is random, which
silently reorders conversation history, the recent turns the model is shown,
and which messages compaction folds.
"""

from __future__ import annotations

import uuid

from app.chat.conversation import add_message, ensure_session
from app.models.common import utcnow


def test_utcnow_never_repeats_under_a_tight_loop():
    """The system clock is coarser than this loop — bare datetime.now() ties."""
    stamps = [utcnow() for _ in range(1000)]
    assert len(set(stamps)) == 1000, "utcnow() returned a duplicate"


def test_utcnow_is_strictly_increasing():
    stamps = [utcnow() for _ in range(500)]
    # Deliberately not strict=True: the two slices differ in length by one.
    assert all(b > a for a, b in zip(stamps, stamps[1:], strict=False))


def test_utcnow_still_tracks_wall_clock():
    """Monotonicity must not let the clock run away from real time."""
    from datetime import UTC, datetime

    before = datetime.now(UTC)
    for _ in range(1000):
        utcnow()
    after = utcnow()
    # 1000 tied rows drift at most 1000us; allow a wide margin for a real tick.
    assert (after - before).total_seconds() < 1.0


async def test_messages_read_back_in_insertion_order(db_session):
    """The end-to-end property that matters: a burst of messages written in
    one tick must read back in the order they were written."""
    from sqlalchemy import select

    from app.models.chat import ConversationMessage

    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    written = [
        await add_message(db_session, session_id, "user", f"message {i}")
        for i in range(30)
    ]

    read_back = (
        (
            await db_session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(
                    ConversationMessage.created_at, ConversationMessage.id
                )
            )
        )
        .scalars()
        .all()
    )

    assert [m.id for m in read_back] == [m.id for m in written]
    assert [m.message for m in read_back] == [f"message {i}" for i in range(30)]


async def test_context_assembly_sees_the_turns_in_order(db_session):
    """The first thing the tie broke: what the model is shown.

    `assemble_context` feeds the recent-turns block. Out-of-order turns make
    the model resolve "it"/"that" against the wrong message — a follow-up
    answered about the wrong thing, with nothing in the reply to signal it.
    """
    from app.chat.conversation import assemble_context

    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    for i in range(12):
        await add_message(
            db_session, session_id, "user" if i % 2 == 0 else "assistant",
            f"turn {i:02d}",
        )

    _summary, recent = await assemble_context(db_session, session_id)

    texts = [t["message"] for t in recent]
    assert texts == sorted(texts), f"turns out of order: {texts}"


async def test_compaction_folds_the_right_turns_under_a_burst(db_session):
    """The second thing the tie broke, and the one that was observed.

    `covers_through_message_id` records how far compaction has folded. With
    tied timestamps the tiebreak was a random uuid4, so the marker pointed at
    an arbitrary message and the next compaction folded the wrong window —
    silently, because the summary still looked plausible.
    """
    from sqlalchemy import select

    from app.chat.conversation import COMPACT_THRESHOLD, KEEP_VERBATIM, maybe_compact
    from app.models.chat import ConversationMessage, ConversationSummary

    session_id = await ensure_session(db_session, uuid.uuid4(), None)
    written = [
        await add_message(
            db_session, session_id, "user" if i % 2 == 0 else "assistant",
            f"turn {i:02d}",
        )
        for i in range(COMPACT_THRESHOLD + 4)
    ]

    await maybe_compact(db_session, session_id)

    row = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().first()
    assert row is not None, "compaction did not fire"

    # It must point at the last FOLDED message — the one KEEP_VERBATIM back
    # from the end — not at an arbitrary member of the tied batch.
    expected = written[len(written) - KEEP_VERBATIM - 1]
    assert row.covers_through_message_id == expected.id, (
        "compaction folded the wrong window: the marker is not the last "
        "folded message"
    )

    # And the marker must be a real message in this session, always.
    marker = (
        await db_session.execute(
            select(ConversationMessage).where(
                ConversationMessage.id == row.covers_through_message_id
            )
        )
    ).scalars().first()
    assert marker is not None and marker.session_id == session_id


def test_utcnow_stays_strictly_increasing_across_threads():
    """`utcnow` guards its state with a lock; without it two threads can be
    handed the same instant and the tie is back."""
    import threading

    seen: list = []
    lock = threading.Lock()

    def _burst():
        local = [utcnow() for _ in range(200)]
        with lock:
            seen.extend(local)

    threads = [threading.Thread(target=_burst) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(seen)) == len(seen), "two callers were handed the same instant"

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

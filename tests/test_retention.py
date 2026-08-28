"""Retention: keep the audit, drop the content.

`conversation_messages` + `rag_turn_receipts` are ~97.5% of Davi-owned
per-user bytes — derived at 9.94 TB/year at 10M users — and nothing deleted
either of them.

They get DIFFERENT windows on purpose. Receipts hash the message instead of
storing it, so they carry no PHI and are the actual audit trail; messages are
the content and the bloat. Keeping the evidence longer than the content is the
answer to "we need audit logs but the transcript bloats the database".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.chat.retention import purge_expired
from app.models.chat import (
    ConversationMessage,
    ConversationSession,
    RagTurnReceipt,
)
from app.models.common import utcnow

USER = uuid.UUID("00000000-0000-0000-0000-00000000dead")


async def _seed(db, *, message_ages_days, receipt_ages_days):
    session = ConversationSession(user_id=USER)
    db.add(session)
    await db.flush()
    now = utcnow()
    for age in message_ages_days:
        db.add(
            ConversationMessage(
                session_id=session.id, role="user", message=f"{age} days old",
                created_at=now - timedelta(days=age),
            )
        )
    for age in receipt_ages_days:
        db.add(
            RagTurnReceipt(
                user_id=USER, query_hash="a" * 64, model_name="fake",
                prompt_version="v1", grounding_mode="off",
                grounding_status="n/a", created_at=now - timedelta(days=age),
            )
        )
    await db.commit()


async def _counts(db):
    return {
        "messages": (
            await db.execute(select(func.count(ConversationMessage.id)))
        ).scalar(),
        "receipts": (
            await db.execute(select(func.count(RagTurnReceipt.id)))
        ).scalar(),
    }


async def test_old_content_goes_and_recent_content_stays(db_session):
    await _seed(db_session, message_ages_days=[1, 30, 200, 400],
                receipt_ages_days=[1, 200])

    result = await purge_expired(
        db_session, message_days=180, receipt_days=400
    )

    assert result["messages_purged"] == 2  # the 200- and 400-day-old ones
    assert (await _counts(db_session))["messages"] == 2


async def test_the_audit_trail_outlives_the_content(db_session):
    """The whole point of two windows.

    A 300-day-old turn: the message is gone, but the receipt proving what the
    system did — and that it was grounded — is still there.
    """
    await _seed(db_session, message_ages_days=[300], receipt_ages_days=[300])

    await purge_expired(db_session, message_days=180, receipt_days=400)

    counts = await _counts(db_session)
    assert counts["messages"] == 0, "content should have been dropped"
    assert counts["receipts"] == 1, "the audit trail was dropped with it"


async def test_receipts_do_eventually_expire(db_session):
    """Longer is not forever — unbounded retention is its own compliance risk."""
    await _seed(db_session, message_ages_days=[], receipt_ages_days=[500])

    await purge_expired(db_session, message_days=180, receipt_days=400)

    assert (await _counts(db_session))["receipts"] == 0


async def test_a_batch_size_of_zero_disables_the_sweep(db_session):
    """An operator must be able to stage this rather than run it blind."""
    await _seed(db_session, message_ages_days=[999], receipt_ages_days=[999])

    result = await purge_expired(
        db_session, message_days=1, receipt_days=1, batch_size=0
    )

    assert result.get("skipped") == 1
    assert (await _counts(db_session))["messages"] == 1


async def test_deletion_is_batched_not_one_giant_statement(db_session):
    """A single unbounded DELETE would lock millions of rows on a database
    shared with two other services."""
    await _seed(
        db_session,
        message_ages_days=[365] * 25,
        receipt_ages_days=[],
    )

    result = await purge_expired(
        db_session, message_days=180, receipt_days=400, batch_size=10
    )

    # 25 rows in batches of 10 — the loop must keep going until none remain,
    # not stop after the first batch.
    assert result["messages_purged"] == 25
    assert (await _counts(db_session))["messages"] == 0


async def test_nothing_recent_is_ever_touched(db_session):
    await _seed(db_session, message_ages_days=[0, 1, 5],
                receipt_ages_days=[0, 1, 5])

    result = await purge_expired(
        db_session, message_days=180, receipt_days=400
    )

    assert result == {
        "messages_purged": 0, "receipts_purged": 0,
        "summaries_superseded_purged": 0, "summaries_purged": 0,
    }
    counts = await _counts(db_session)
    assert counts["messages"] == 3 and counts["receipts"] == 3


async def test_superseded_and_stale_summaries_are_purged(db_session):
    """Summaries are content, not audit: superseded versions go immediately,
    and the survivor follows the MESSAGE retention window (audit high — they
    used to outlive the transcript forever, questions verbatim included)."""
    import uuid as _uuid
    from datetime import timedelta

    from app.chat.retention import purge_expired
    from app.models.chat import ConversationSession, ConversationSummary
    from app.models.common import utcnow

    sid = _uuid.uuid4()
    db_session.add(ConversationSession(id=sid, user_id=_uuid.uuid4()))
    old = utcnow() - timedelta(days=400)
    for version in (1, 2, 3):
        db_session.add(ConversationSummary(
            session_id=sid, version=version, summary={"v": version},
        ))
    await db_session.flush()
    # age every version far past the message window
    from sqlalchemy import update
    await db_session.execute(
        update(ConversationSummary).values(created_at=old)
    )
    await db_session.commit()

    counts = await purge_expired(
        db_session, message_days=180, receipt_days=400, batch_size=100
    )
    assert counts["summaries_superseded_purged"] == 2  # v1, v2
    assert counts["summaries_purged"] == 1             # stale v3
    from sqlalchemy import func, select
    left = (await db_session.execute(
        select(func.count()).select_from(ConversationSummary)
    )).scalar()
    assert left == 0

"""Bounded retention for the chat transcript and the audit trail.

Derived (`project_docs/per-user-memory.md`): `conversation_messages` and
`rag_turn_receipts` are **97.5%** of Davi-owned per-user bytes — 9.94 TB/year
at 10M users — and until now nothing ever deleted either.

The two are kept for DIFFERENT periods, because they answer different
questions:

* **Messages** are the content. They are what a reader sees in their history
  and what makes a transcript auditable in the ordinary sense, and they are the
  bloat. Bounded by `message_retention_days`.
* **Receipts** are the audit trail proper. They store a SHA-256 of the message
  rather than the message, plus the model, the retrieved chunks and the
  grounding verdict — so they answer "what did the system do, and could it
  justify it?" without holding any PHI at all. They are smaller and safer, so
  they are kept LONGER.

That split is the answer to "we need audit logs but the transcript bloats the
database": keep the evidence, drop the content. Aggregating messages per user
instead would bloat less but destroy the audit — an audit that cannot produce
the specific exchange is not an audit.

Deletes are BATCHED. The nightly sweep already runs its whole body in one
transaction; adding an unbounded `DELETE` to it would hold locks over millions
of rows and block autovacuum cluster-wide on a database shared with two other
services.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ConversationMessage, RagTurnReceipt
from app.models.common import utcnow

logger = logging.getLogger("davi.retention")


async def _purge_older_than(
    db: AsyncSession, model, cutoff: datetime, batch_size: int
) -> int:
    """Delete in committed batches, oldest first. Returns rows removed.

    Keyed on the primary key rather than `OFFSET`: a moving window makes
    offset pagination skip rows, and skipped rows here are rows that are never
    deleted.
    """
    removed = 0
    while True:
        ids = (
            await db.execute(
                select(model.id)
                .where(model.created_at < cutoff)
                .order_by(model.created_at)
                .limit(batch_size)
            )
        ).scalars().all()
        if not ids:
            break
        result = await db.execute(delete(model).where(model.id.in_(ids)))
        removed += getattr(result, "rowcount", 0) or len(ids)
        # Commit each batch: locks are released, and a failure halfway leaves
        # the batches that already succeeded intact.
        await db.commit()
    return removed


async def purge_expired(
    db: AsyncSession,
    *,
    message_days: int,
    receipt_days: int,
    batch_size: int = 5000,
    now: datetime | None = None,
) -> dict[str, int]:
    """Apply both retention windows. Returns per-table counts."""
    if batch_size <= 0:
        logger.info("retention disabled (batch size 0)")
        return {"messages_purged": 0, "receipts_purged": 0, "skipped": 1}

    now = now or utcnow()
    counts: dict[str, int] = {}

    # Messages first: they are the larger share, and deleting them cannot
    # orphan a receipt (receipts reference no message).
    counts["messages_purged"] = await _purge_older_than(
        db, ConversationMessage, now - timedelta(days=message_days), batch_size
    )
    counts["receipts_purged"] = await _purge_older_than(
        db, RagTurnReceipt, now - timedelta(days=receipt_days), batch_size
    )
    return counts

"""Reader feedback on a chat turn.

drawbacks.md §8.2: nothing captured whether a reply was any good, so
improvement was entirely developer-initiated from anecdote. This closes the
loop — and the loop only closes if a down-voted turn can become a regression
case, which is why the row carries enough to reconstruct one.

Deliberately joined to ``rag_turn_receipts`` rather than duplicating anything:
the receipt already holds the query hash, the model, the retrieved chunks and
the grounding verdict. Feedback adds the reader's judgement to a record that
already exists.

PHI: ``comment`` is free text a reader typed, so it IS potentially sensitive.
It is stored because a correction without the reader's words is rarely
actionable — but it is never logged, never sent to a model, and is erased by
the same "forget me" path as the profile.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, UUIDPrimaryKey

# Coarse on purpose. A five-star scale invites deliberation about the scale;
# a thumb is a judgement about the answer.
RATINGS = ("up", "down")

# Why it was wrong, when the reader will say. Bounded so it can be counted.
REASONS = (
    "wrong",           # factually incorrect
    "unhelpful",       # correct but useless
    "unsafe",          # should not have said that
    "confusing",       # could not follow it
    "too_cautious",    # refused or hedged when it should have answered
    "other",
)


class TurnFeedback(Base, UUIDPrimaryKey, CreatedAt):
    """One reader's judgement of one assistant turn."""

    __tablename__ = "turn_feedback"
    __table_args__ = (
        # One verdict per reader per turn; sending it again is a correction,
        # not a second vote.
        sa.UniqueConstraint("user_id", "message_id", name="uq_turn_feedback"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, nullable=False, index=True
    )
    # The assistant message being judged.
    message_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    # The receipt for that turn, when one exists — it carries the model, the
    # retrieved chunks and the grounding verdict a regression case needs.
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)

    rating: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    comment: Mapped[str | None] = mapped_column(sa.String(2000), nullable=True)

    # Set when the turn has been turned into a quality case, so the review
    # queue can show what is still outstanding.
    triaged_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

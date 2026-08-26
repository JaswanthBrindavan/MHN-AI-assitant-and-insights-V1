"""A scheduled, reversible erasure request.

Erasure is deferred rather than immediate, by decision: a grace period is what
lets an accidental or coerced deletion be undone, and retention regimes
generally expect a bounded window rather than an instant vanish.

Two properties make a deferred erasure honest rather than a delay tactic:

1. **The data stops being USED the moment it is requested.** A pending request
   suppresses every per-user memory read, so the assistant genuinely forgets
   the reader from that second — it just has not yet destroyed the bytes.
   Without this, "we have deleted your data" is false for the whole window.
2. **The window is bounded and recorded.** ``scheduled_for`` is set when the
   request is made, not computed at purge time, so shortening or lengthening
   the configured grace later cannot silently move an existing promise.

What is deliberately NOT erased, and why:

* ``consent_ledger`` — append-only, and the record that consent was given and
  withdrawn. Destroying it would destroy the evidence that the erasure was
  authorised in the first place.
* ``insight_review_audit`` — the record of which clinician read whose data. It
  exists to protect the subject; a subject-triggered delete that erases it
  would let access go unaccounted for.

Both are flagged in `project_docs/open-items.md` as decisions worth ratifying
rather than assumptions to inherit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, UUIDPrimaryKey

PENDING = "pending"
COMPLETED = "completed"
CANCELLED = "cancelled"
STATUSES = (PENDING, COMPLETED, CANCELLED)


class ErasureRequest(Base, UUIDPrimaryKey, CreatedAt):
    """One request to erase everything Davi holds about one user."""

    __tablename__ = "erasure_requests"

    # Plain uuid, NO FK to "user" — the coexistence rule for Davi tables. Also
    # necessary rather than merely conventional here: this row must outlive the
    # account it describes, which is the whole point of it.
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)

    requested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    # Fixed at request time, NOT derived from config at purge time — otherwise
    # changing the grace period would silently move a promise already made.
    scheduled_for: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=PENDING
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # What was actually removed, per table. Kept because "we deleted your data"
    # is a claim somebody may one day have to substantiate.
    deleted_counts: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    source: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="api"
    )

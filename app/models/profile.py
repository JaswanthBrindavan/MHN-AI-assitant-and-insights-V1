"""Long-term user profile — what the assistant is allowed to remember about you.

Distinct from ``user_memories``, which records what a reader has *discussed*.
This is what they have *told us about themselves*, and it is consent-gated:
nothing is stored here until a ``chat_personalization`` grant exists in the
consent ledger, and everything is viewable and erasable through the API.

Deliberately NOT free text. Every field is a small, enumerable fact, so what is
held about a reader can be shown back to them on one screen and deleted in one
call. A free-text "notes" column would be impossible to audit and would quietly
become a second, unreviewable medical record.

Follows the house convention: plain ``uuid`` ``user_id`` with NO foreign key to
``"user"`` (Flyway owns that table).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, JSONColumn, UUIDPrimaryKey

# Coarse age bands rather than a birth date: enough to pitch an answer, not
# enough to identify anyone, and it never goes stale by a day.
AGE_BANDS = ("under_18", "18_29", "30_44", "45_59", "60_74", "75_plus")

# How the reader wants to be spoken to. A 22-year-old asking about acne and a
# 70-year-old asking about heart failure should not get identically pitched
# prose — but the reader decides, we do not infer it.
COMMUNICATION_STYLES = ("plain", "detailed")


class UserProfile(Base, UUIDPrimaryKey, CreatedAt):
    """One row per user. Every field optional; absence means "not told"."""

    __tablename__ = "user_profiles"
    __table_args__ = (
        sa.UniqueConstraint("user_id", name="uq_user_profile_user"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, nullable=False, index=True
    )

    # --- how to talk to them ---
    age_band: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    sex: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    communication_style: Mapped[str | None] = mapped_column(
        sa.String(16), nullable=True
    )
    preferred_language: Mapped[str | None] = mapped_column(
        sa.String(16), nullable=True
    )

    # --- what matters clinically, as the reader described it ---
    # Lists of short strings. Never used to assert a diagnosis — they are
    # context for the answer, exactly like the pedigree [P] block.
    chronic_conditions: Mapped[list | None] = mapped_column(
        JSONColumn, nullable=True
    )
    current_medications: Mapped[list | None] = mapped_column(
        JSONColumn, nullable=True
    )
    allergies: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    goals: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)

    # Pregnancy changes what is safe to say about a great many things, so it is
    # worth its own field rather than hiding inside chronic_conditions.
    is_pregnant: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)

    # The consent grant that permits storing any of this.
    consent_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("consent_ledger.id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

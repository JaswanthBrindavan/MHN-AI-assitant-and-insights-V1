"""Core identity, consent, and pedigree (family-history) tables."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, UUIDPrimaryKey

# Fixed pedigree slots — six per user (two parents, four grandparents).
PEDIGREE_SLOTS = (
    "mother",
    "father",
    "grandmother_maternal",
    "grandfather_maternal",
    "grandmother_paternal",
    "grandfather_paternal",
)


class User(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "users"


class ConsentLedger(Base, UUIDPrimaryKey, CreatedAt):
    """APPEND-ONLY consent events.

    There are no update/delete code paths for this table. In production the app
    DB role must have UPDATE/DELETE revoked on it (see README ops notes).
    """

    __tablename__ = "consent_ledger"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # granted|revoked
    scope: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class PedigreeMember(Base, UUIDPrimaryKey, CreatedAt):
    """One of six fixed relative slots for a user."""

    __tablename__ = "pedigree_members"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "slot", name="uq_pedigree_member_slot"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False, index=True
    )
    slot: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    vital_status: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    cause_of_death: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)


class PedigreeCondition(Base, UUIDPrimaryKey, CreatedAt):
    """One row per (user, slot, condition) — the insights engine's input."""

    __tablename__ = "pedigree_conditions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False, index=True
    )
    slot: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    condition_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    condition_display: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # onset_band: under_30, 30_34 ... 70_plus, unknown
    onset_band: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # certainty: verified | confirmed | as_far_as_i_know
    certainty: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    # provenance: connected_verified | self_report
    provenance: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    consent_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("consent_ledger.id"), nullable=True
    )
    soft_deleted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

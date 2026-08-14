"""Identity reference, consent, and pedigree (family-history) tables.

This backend coexists with the existing MHN/Davi database. The ``user`` table
is owned and migrated by the core app (Flyway); we map only the columns we need
and never migrate it (see ``EXTERNAL_TABLES`` in the Alembic env). Following the
existing AI subsystem's convention, our tables store ``user_id`` as a plain uuid
with NO foreign key to ``user`` — keeping the AI backend decoupled from the
core schema.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, JSONColumn, UUIDPrimaryKey

# Fixed pedigree slots — six per user (two parents, four grandparents).
PEDIGREE_SLOTS = (
    "mother",
    "father",
    "grandmother_maternal",
    "grandfather_maternal",
    "grandmother_paternal",
    "grandfather_paternal",
)

# Tables that already exist in the shared database and are managed by another
# tool (Flyway core / the existing AI Alembic chain). Our migrations must never
# create or drop these; the Alembic env excludes them.
EXTERNAL_TABLES = {"user"}


class User(Base):
    """Partial mapping of the core app's ``user`` table.

    Read-only from this backend's perspective and NOT created by our migrations.
    Only the columns needed for synthetic seeding and lookups are mapped; the
    NOT NULL columns are included so a synthetic insert satisfies the real
    table's constraints.
    """

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    user_name: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    health_card_number: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    hashcode: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ConsentLedger(Base, UUIDPrimaryKey, CreatedAt):
    """APPEND-ONLY consent events.

    There are no update/delete code paths for this table. In production the app
    DB role must have UPDATE/DELETE revoked on it (see README ops notes).
    """

    __tablename__ = "consent_ledger"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # granted|revoked
    scope: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)


class PedigreeMember(Base, UUIDPrimaryKey, CreatedAt):
    """One of six fixed relative slots for a user."""

    __tablename__ = "pedigree_members"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "slot", name="uq_pedigree_member_slot"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    slot: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    vital_status: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    cause_of_death: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)


class PedigreeCondition(Base, UUIDPrimaryKey, CreatedAt):
    """One row per (user, slot, condition) — the insights engine's input."""

    __tablename__ = "pedigree_conditions"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    slot: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    condition_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    condition_display: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # onset_band: under_30, 30_34 ... 70_plus, unknown
    onset_band: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # certainty: verified | confirmed | as_far_as_i_know
    certainty: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    # provenance: connected_verified | self_report
    provenance: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    # FK to our OWN consent_ledger table is fine (same subsystem).
    consent_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("consent_ledger.id"), nullable=True
    )
    soft_deleted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

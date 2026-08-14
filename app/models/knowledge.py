"""Clinically-validated knowledge tables: condition registry + drug reference.

Populated from the MHN Master Condition Profiles (512 docx files) and the
merged Indian medicines database (~250K rows) by the ingest scripts. These are
OUR tables (AI subsystem) — distinct from the Flyway-owned `medicine_master`.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, JSONColumn, UUIDPrimaryKey


class ConditionRegistry(Base, UUIDPrimaryKey, CreatedAt):
    """One row per Master Condition Profile (MC code)."""

    __tablename__ = "condition_registry"

    condition_code: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    # AKA aliases parsed from the profile ("Sugar Disease", "NIDD", ...).
    aliases: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Legacy engine codes that map to this condition (e.g. ["T2DM"]).
    engine_codes: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    source_file: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class DrugReference(Base, UUIDPrimaryKey, CreatedAt):
    """One row per medicine from the merged drug database."""

    __tablename__ = "drug_reference"

    source_id: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, index=True
    )
    manufacturer: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    dosage_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pack_size: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    price_inr: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    is_discontinued: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    composition1: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    composition2: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Lowercased salt names for generic lookup ("metformin", "amoxycillin").
    composition_normalized: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True, index=True
    )
    side_effects: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    uses: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    substitutes: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    chemical_class: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    habit_forming: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    therapeutic_class: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True
    )
    action_class: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)

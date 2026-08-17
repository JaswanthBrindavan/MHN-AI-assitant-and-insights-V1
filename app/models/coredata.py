"""Partial read models for core-app (Flyway-owned) tables.

These map the EXISTING MHN/Davi tables the chat data-abilities read from —
documents, vitals, lifestyle logs, tracking, family links, THP reference.
They are never created or altered by our migrations (see EXTERNAL_TABLES).
``lifestyle_log`` is the one table we also INSERT rows into (tracker adds on
the user's behalf); everything else is read-only from this backend.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import JSONColumn


def _pg_enum(name: str, *values: str):
    """String column that binds as the core app's PG enum type.

    ``create_type=False``: the enum already exists (Flyway-owned); we only
    reference it so parameter binds cast correctly ($1::<enum> not ::VARCHAR).
    SQLite (unit tests) sees a plain string.
    """
    return sa.String(32).with_variant(
        postgresql.ENUM(*values, name=name, create_type=False), "postgresql"
    )

# Table names owned by the core app that this module maps (merged into
# EXTERNAL_TABLES in app.models.core).
COREDATA_TABLES = {
    "reports",
    "scans_imaging",
    "prescriptions",
    "vaccinations",
    "vital_reading",
    "body_measurement",
    "lifestyle_log",
    "manual_tracking",
    "medicine_tracking",
    "family_connect",
    "relations",
    "family_file_access",
    "traditional_health_parameters",
}


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ScanImaging(Base):
    __tablename__ = "scans_imaging"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Vaccination(Base):
    __tablename__ = "vaccinations"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    next_due_on: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class VitalReading(Base):
    __tablename__ = "vital_reading"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    vital_type: Mapped[str] = mapped_column(
        _pg_enum("vital_type_enum", "heart_rate", "blood_pressure",
                 "blood_sugar", "spo2"),
        nullable=False,
    )
    value_primary: Mapped[float] = mapped_column(sa.Numeric(6, 2), nullable=False)
    value_secondary: Mapped[float | None] = mapped_column(
        sa.Numeric(6, 2), nullable=True
    )
    unit: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )


class BodyMeasurement(Base):
    __tablename__ = "body_measurement"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    type: Mapped[str] = mapped_column(
        _pg_enum("body_measurement_type_enum", "weight", "height", "bmi",
                 "body_fat", "muscle_mass", "water", "bone_mass",
                 "visceral_fat"),
        nullable=False,
    )
    value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    date: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class LifestyleLog(Base):
    __tablename__ = "lifestyle_log"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    log_type: Mapped[str] = mapped_column(
        _pg_enum("lifestyle_log_type_enum", "water", "alcohol", "coffee",
                 "tea", "smoking"),
        nullable=False,
    )
    quantity: Mapped[float] = mapped_column(sa.Numeric(6, 2), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSONColumn, nullable=True
    )
    logged_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )


class ManualTracking(Base):
    __tablename__ = "manual_tracking"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    type: Mapped[str] = mapped_column(
        _pg_enum("manual_tracking_type_enum", "steps", "calories", "water",
                 "sleep", "heart_rate", "blood_pressure", "blood_sugar",
                 "spo2"),
        nullable=False,
    )
    value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    goal: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class MedicineTracking(Base):
    """The user's tracked medications (core-app table). Read-only here.

    Partial mapping — only the columns the health snapshot reads. Active =
    ``stopped_at IS NULL``; private rows are never surfaced. The many
    scheduling/enum columns (day_pattern, dosage_form, schedule_pattern, the
    generated effective_end) are left unmapped since we only read names.
    """

    __tablename__ = "medicine_tracking"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    strength: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    private: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    is_prn: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    stopped_at: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    starts_at: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    ends_at: Mapped[date | None] = mapped_column(sa.Date, nullable=True)


class FamilyConnect(Base):
    __tablename__ = "family_connect"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    acceptor_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    accepted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    req_file_share: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    acc_file_share: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    relation_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    inverse: Mapped[str] = mapped_column(sa.String(100), nullable=False)


class FamilyFileAccess(Base):
    __tablename__ = "family_file_access"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    fc_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    allowed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)


class TraditionalHealthParameter(Base):
    __tablename__ = "traditional_health_parameters"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    units: Mapped[str] = mapped_column(sa.String(25), nullable=False)
    aliases: Mapped[list | None] = mapped_column(
        sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite"), nullable=True
    )


_ = date  # (kept for future date-typed columns)

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

# Every Flyway-owned enum this module references. Production and the
# coexistence dump both already have them; a bare test PostgreSQL does not, and
# `Base.metadata.create_all` will not make them because of `create_type=False`.
# The pg fixture creates these first -- see `tests/conftest.py::pg_engine`.
PG_ENUMS: list = []


def _pg_enum(name: str, *values: str):
    """String column that binds as the core app's PG enum type.

    ``create_type=False``: the enum already exists (Flyway-owned); we only
    reference it so parameter binds cast correctly ($1::<enum> not ::VARCHAR).
    SQLite (unit tests) sees a plain string.
    """
    enum = postgresql.ENUM(*values, name=name, create_type=False)
    PG_ENUMS.append(enum)
    return sa.String(32).with_variant(enum, "postgresql")

# Table names owned by the core app that this module maps (merged into
# EXTERNAL_TABLES in app.models.core).
COREDATA_TABLES = {
    "unclassified_files",
    "insurance",
    "bills",
    "doctor",
    "doctor_connect",
    "doctor_specialization",
    "reports",
    "scans_imaging",
    "prescriptions",
    "vaccinations",
    "vital_reading",
    "body_measurement",
    "lifestyle_log",
    "lifestyle_daily_total",
    "manual_tracking",
    "medicine_tracking",
    "family_connect",
    "relations",
    "family_file_access",
    "file_access_exclusions",
    "traditional_health_parameters",
    "thp_age_range",
    "medical_condition",
    "medicine_master",
    "period_day_log",
    "period_settings",
    "period_status",
    "period_tracking",
    "sahha_daily_total",
    "sahha_weekly_total",
    "mood_log",
    "user_thp_series",
    "lifestyle_limit",
    "body_measurement_goal",
    "sahha_goal",
}


class UnclassifiedFile(Base):
    """A document as uploaded, before mhn-ai classifies and files it.

    This is the unit mhn-ai's document-processing runs operate on: Spring (or
    Davi's chat upload) inserts a row here, and the pipeline classifies it,
    files it into its section table, and extracts values. ``filepath`` is the
    S3 key the worker downloads (Davi's dev/chassis uploads store a local
    stand-in path).
    """

    __tablename__ = "unclassified_files"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


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
                 "tea", "smoking", "energy_drink", "other_drink"),
        nullable=False,
    )
    quantity: Mapped[float] = mapped_column(sa.Numeric(6, 2), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    # `quantity` is the CANONICAL measure and the one the rollups sum -- ml for
    # water and alcohol, servings for everything else -- because `resolveUnit`
    # 400s any other unit and `MetricFanout.of` adds
    # `new Measure(primary(type), quantity)`. See LIFESTYLE_UNITS.
    #
    # These two are mhn-spring's per-row copies of that same number (V35,
    # ManualTrackingServiceImpl:208-212). Only `volume_ml` feeds a rollup, the
    # derived `drink_volume_ml` whole-day fluid series; `servings` feeds none.
    # NULL on every row written before V35. Neither is a total to read.
    volume_ml: Mapped[float | None] = mapped_column(
        sa.Numeric(9, 2), nullable=True
    )
    servings: Mapped[float | None] = mapped_column(
        sa.Numeric(6, 2), nullable=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSONColumn, nullable=True
    )
    logged_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )


class LifestyleDailyTotal(Base):
    """One row per (user, metric, day) — mhn-spring's pre-aggregated graph
    feed for ``lifestyle_log``. Read-only.

    Read INSTEAD of re-bucketing ``lifestyle_log`` in Python for one reason
    that matters to anything pairing a habit with a wearable reading: this
    ``bucket_start`` and ``sahha_daily_total.bucket_start`` were both assigned
    by Spring in ``app.tracking.zone`` at write time, and that zone is not
    recoverable from the data. ``date(logged_at)`` would be the UTC day, so a
    late-evening log would land on a different "day" than the night's sleep it
    is being compared with.

    The cost of that alignment: rows Davi writes through
    ``add_lifestyle_log`` reach this table only when Spring reconciles, so the
    most recent day or two can lag the log. Callers that compare days should
    exclude today anyway (a day in progress is not a day).

    ``total`` is ``SUM(quantity)`` for the day, in the metric's own unit --
    which is safe because the platform stores exactly one unit per metric and
    rejects any other. It is still read here only for PRESENCE ("was anything
    logged that day"): ``lifestyle_totals`` answers amounts off the log
    itself, where a row Davi has just written is already visible.
    """

    __tablename__ = "lifestyle_daily_total"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    # mhn-spring's V35 RENAMED this column from `log_type` to `metric` and
    # retyped it — `db/existing_schema.sql:6540-6543`, on all four rollup
    # tables. Mapping the old name raised UndefinedColumn on every read: the
    # V28 failure again, and `test_schema_parity` did not catch it because it
    # replayed ADD and DROP COLUMN but not RENAME.
    #
    # The new enum is WIDER than the log-type one. Alongside the seven habits
    # it carries three DERIVED metrics — `caffeine_mg`, `ethanol_g` and
    # `drink_volume_ml` — which are mhn-spring's own unit-safe totals. Anything
    # wanting an amount rather than a presence should read those rather than
    # summing `lifestyle_log.quantity`, which mixes units.
    metric: Mapped[str] = mapped_column(
        _pg_enum("lifestyle_metric_enum", "water", "alcohol", "coffee",
                 "tea", "smoking", "energy_drink", "other_drink",
                 "caffeine_mg", "ethanol_g", "drink_volume_ml"),
        primary_key=True,
    )
    bucket_start: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    total: Mapped[float] = mapped_column(sa.Numeric(12, 2), nullable=False)
    entries: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    days_counted: Mapped[int] = mapped_column(sa.Integer, nullable=False)


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


class _SahhaRollup:
    """The shape every ``sahha_*_total`` rollup shares (all three are identical).

    Read-only; mhn-spring's V40 owns them and rebuilds them delete-then-insert
    on every sync and again nightly, so a recent bucket is not settled.

    ``total`` is a plain SUM for EVERY metric, including the ones whose only
    honest figure is a mean (resting heart rate, HRV, the scores). The mean is
    ``total / entries`` -- see ``app.coredata.service.SAHHA_METRICS``.

    ``metric`` is varchar, NOT an enum, deliberately: Sahha's vocabulary grows
    and an unknown value must be a row, not a failed read.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    metric: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    bucket_start: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    total: Mapped[float] = mapped_column(sa.Numeric(16, 4), nullable=False)
    entries: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    days_counted: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class SahhaDailyTotal(Base, _SahhaRollup):
    """One row per (user, metric, day)."""

    __tablename__ = "sahha_daily_total"


class SahhaWeeklyTotal(Base, _SahhaRollup):
    """One row per (user, metric, week). ``bucket_start`` is the SUNDAY that
    opens the week -- not PostgreSQL's Monday, and not Python's
    ``date.weekday()``. Never re-derive it; read it.
    """

    __tablename__ = "sahha_weekly_total"


class MedicineTracking(Base):
    """The user's tracked medications (core-app table). Read-only here.

    Partial mapping — only the columns the health snapshot reads. Private rows
    are never surfaced.

    "Active" is NOT ``stopped_at IS NULL`` alone, which is what this said and what
    the reader was told. A course with an end date that has passed is neither
    stopped nor deleted, so it stayed on the list: on a real account "Dolo 650 mg"
    and "Telmisartan 40 mg" both finished on 02 Sep and were still being read back
    as current two days later. ``effective_end`` is the column that knows, and it
    is mapped for exactly that.
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
    # Soft delete, same trap as medical_condition: a deleted course still has
    # `stopped_at IS NULL`, so it reads as current until this is filtered.
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # What tells two courses of the same drug apart: 650 mg at night is not the
    # same prescription as 650 mg in the morning, and neither is a tablet the same
    # as a syrup. Read so that collapsing duplicates can require them to agree.
    dosage_form: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    schedule_pattern: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    # GENERATED ALWAYS in production — `ends_at IS NULL ? NULL : GREATEST(ends_at,
    # extended_till)`. Mapped as a plain column because this service only ever
    # reads this table; declaring the expression would make it undeclarable in the
    # sqlite schema the tests build (no GREATEST).
    effective_end: Mapped[date | None] = mapped_column(sa.Date, nullable=True)


class FamilyConnect(Base):
    """Family link + consent. Production semantics (FileServiceImpl
    hasConnectionRead): the read grant sits on the OWNER's side — ``req_read``
    when the owner sent the request, ``acc_read`` when they accepted. The
    older ``req_file_share``/``acc_file_share`` columns remain in the baseline
    schema; production's ddl-auto added the new ones, so both are mapped
    nullable and readers prefer new-with-fallback."""

    __tablename__ = "family_connect"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    requester_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    acceptor_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    accepted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    req_file_share: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    acc_file_share: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    req_read: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    acc_read: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    relation_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class FileAccessExclusion(Base):
    """Per-file opt-out from family sharing (production consent layer).

    A row means: ``user_id`` (the VIEWER) is excluded from this specific
    resource even though connection-level read is granted. Mirrors
    file_access_exclusions, which supersedes the legacy family_file_access.
    """

    __tablename__ = "file_access_exclusions"
    __table_args__ = (
        sa.UniqueConstraint(
            "user_id", "resource_type", "resource_id",
            name="uq_file_access_exclusion",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    inverse: Mapped[str] = mapped_column(sa.String(100), nullable=False)


class Doctor(Base):
    """Partial mapping of the core app's doctor directory (read-only)."""

    __tablename__ = "doctor"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    verified: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    specialization_id: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True
    )


class DoctorSpecialization(Base):
    __tablename__ = "doctor_specialization"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)


class DoctorConnect(Base):
    """Partial mapping of doctor-patient connections (read-only). A consult
    relationship exists once both sides have accepted."""

    __tablename__ = "doctor_connect"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    doctor_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    doctor_acceptance: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True
    )
    user_acceptance: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Insurance(Base):
    __tablename__ = "insurance"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    filepath: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class FamilyFileAccess(Base):
    __tablename__ = "family_file_access"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    fc_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    resource_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    allowed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)


class MoodLog(Base):
    """One mood entry per user per day. Read-only.

    ``score`` is the slider stop, 1-10, higher being more pleasant. The seven
    display bands live in mhn-spring's ``MoodScale`` and are deliberately NOT
    mapped: their own comment says a score is orderable and averageable and a
    band is not, so anything comparing days must use the score.

    ``factors`` is a list of MoodFactor codes, and an EMPTY array is a real
    answer — "I logged a score and skipped the chips" — not a missing one.
    Anything counting factors has to use entries-with-factors as its
    denominator, per the column's own comment in the Flyway chain.
    """

    __tablename__ = "mood_log"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    log_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    score: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    factors: Mapped[list | None] = mapped_column(
        sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class TraditionalHealthParameter(Base):
    __tablename__ = "traditional_health_parameters"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    units: Mapped[str] = mapped_column(sa.String(25), nullable=False)
    aliases: Mapped[list | None] = mapped_column(
        sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite"), nullable=True
    )
    # Curation state, added by the backend in V14/V18. Mapped because Davi
    # must not grade a patient's value against reference data the owning team
    # has not approved: "HDL/LDL Ratio" ships as status='draft'.
    #
    # Nullable with a permissive default so a database predating those columns
    # still behaves — the rows there are the curated originals.
    status: Mapped[str | None] = mapped_column(
        sa.String(32), nullable=True, default="approved"
    )
    visible: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True, default=True
    )


class MedicineMaster(Base):
    """The core app's medicine catalogue (Flyway V19). Read-only here.

    Partial mapping — only the columns the chat drug path reads. In PG,
    ``name_normalized`` and ``composition_normalized`` are trigger-maintained
    (lowercased, punctuation collapsed to single spaces); sqlite test fixtures
    must set them explicitly with the same formulas.
    """

    __tablename__ = "medicine_master"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    name_normalized: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    generic_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    composition1: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    composition2: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    composition_normalized: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )
    # Comma-joined list (", " separator), unlike drug_reference's JSON list.
    side_effects: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    used_for: Mapped[list | None] = mapped_column(
        sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite"), nullable=True
    )
    habit_forming: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    is_discontinued: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    status: Mapped[str] = mapped_column(
        _pg_enum("reference_status_enum", "draft", "pending", "approved",
                 "rejected", "archived", "merged"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ThpAgeRange(Base):
    """Age-banded reference range for a health parameter (production data).

    The thresholds (min ≤ low_warn ≤ ideal ≤ high_warn ≤ max) are the
    clinically-curated ideal ranges the value-check reads from the backend.
    ``min``/``max`` are the graph axis bounds, NOT clinical thresholds: only
    ``low_warn``/``high_warn`` grade a value. Read-only here.

    mhn-spring's V28 dropped ``low_danger``/``high_danger`` and rebuilt the
    CHECK over what is left. Mapping a column production does not have makes
    every SELECT on this table raise ``UndefinedColumn``, so check
    ``db/existing_schema.sql`` before adding one — ``tests/test_schema_parity``
    is the guard that would have caught V28.
    """

    __tablename__ = "thp_age_range"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    thp_id: Mapped[int] = mapped_column(sa.Integer, nullable=False, index=True)
    age_min: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    age_max: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # `any` | `female` | `male`. 78 of the 277 seeded rows are sex-specific,
    # covering 28 parameters — Alkaline Phosphatase is male 45-129 and female
    # 35-104, HDL is male 40-60 and female 50-70. While this column was
    # unmapped the band was chosen by age alone, so which one a reader was
    # graded against came down to row order.
    sex: Mapped[str] = mapped_column(sa.String(16), nullable=False,
                                     server_default="any")
    min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    low_warn: Mapped[float] = mapped_column(sa.Float, nullable=False)
    ideal: Mapped[float] = mapped_column(sa.Float, nullable=False)
    high_warn: Mapped[float] = mapped_column(sa.Float, nullable=False)
    max: Mapped[float] = mapped_column(sa.Float, nullable=False)


class MedicalCondition(Base):
    """Conditions, surgeries AND allergies — one table split by ``type``.

    mhn-spring's V7 turned the original conditions table into a three-in-one
    record rather than three tables, because the hub screen reads all three
    together and three tables would have meant three copies of the
    family-sharing switch.

    Read-only here, and PARTIAL: only the columns Davi needs. Note ``private``,
    which the owning service honours — anything Davi surfaces must honour it
    too, or Davi shows what the app deliberately hides.
    """

    __tablename__ = "medical_condition"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # condition | surgery | allergy
    type: Mapped[str | None] = mapped_column(
        _pg_enum("medical_record_type_enum", "condition", "surgery", "allergy"),
        nullable=True,
    )
    status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    started_on: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    ended_on: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Allergy-only columns.
    reaction: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(
        _pg_enum("allergy_category_enum", "food", "environmental", "medication"),
        nullable=True,
    )
    severity: Mapped[str | None] = mapped_column(
        _pg_enum("allergy_severity_enum", "mild", "medium", "severe"),
        nullable=True,
    )
    # Nullable-with-fallback: the column is `bool DEFAULT false NULL`, so a row
    # predating it reads NULL. NULL is treated as NOT private, matching the
    # column default rather than inventing a stricter rule than the app's.
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    # Soft delete. A row the reader deleted in the app keeps its `status` --
    # 'active' -- so a reader that ignores this column reports a DELETED
    # condition as a current one.
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


# --------------------------------------------------------------------------- #
# Cycle tracking
# --------------------------------------------------------------------------- #
# mhn-spring's V5 argues the privacy model in its own DDL, and it is worth
# quoting because it decides how Davi may use any of this:
#
#   "DEFAULT TRUE, unlike every other `private` column in this schema. The
#    existing family sharing model is default-ALLOW... Shipping cycle data on
#    that model would, on release day, hand every accepted connection — spouse,
#    parent, sibling, in-law — visibility of contraception and pregnancy status
#    that nobody opted into."
#
# and, on the fertile window:
#
#   "Off unless the user turns it on. A fertile window is an estimate, it is
#    not contraception, and defaulting it on would put a claim in front of
#    people who never asked for one."
#
# So, for Davi: this is OWN-DATA ONLY, never a family member's, not even for an
# accepted connection. `resource_type_enum` was deliberately NOT extended with
# a cycle value, so it is not a shareable resource at all.


class PeriodSettings(Base):
    """Whether cycle tracking is on, and what the reader agreed to show."""

    __tablename__ = "period_settings"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    enabled: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    # DEFAULT TRUE here, unlike everywhere else in the schema.
    private: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    share_with_doctor: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    goal: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    predict_enabled: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    # Davi must not state a fertile window unless this is on.
    show_fertile_window: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True
    )
    assumed_cycle_length: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True
    )
    paused_until: Mapped[date | None] = mapped_column(sa.Date, nullable=True)


class PeriodStatus(Base):
    """Life stage, contraception, pregnancy — TEMPORAL, latest row wins.

    `cycles_countable` and `predictions_suppressed` are GENERATED columns that
    encode mhn-spring's clinical logic (which stages, contraceptives and
    surgeries make a cycle countable). Davi READS them rather than re-deriving
    them, for the same reason it asks Spring for adherence: two services
    disagreeing about the same fact in front of one reader is worse than a
    network call.
    """

    __tablename__ = "period_status"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(sa.Date, nullable=False)
    stage: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    contraception: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    pregnancy: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    surgical: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    breastfeeding: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    diagnosed_pcos: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    # Generated by the database. Read, never computed.
    cycles_countable: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True
    )
    predictions_suppressed: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True
    )


class PeriodTracking(Base):
    """One recorded cycle."""

    __tablename__ = "period_tracking"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    start_date: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    end_date: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # A PREDICTED cycle is an estimate the app drew, not something that
    # happened. Davi must never report one as a fact.
    # `is_predicted` and `symptoms` were mapped here and exist in NO
    # environment: not in the Flyway chain, not in production. The
    # parity guard exempted them as "ddl-auto", which was simply wrong,
    # and the query that filtered on `is_predicted` therefore raised
    # UndefinedColumn and fell into its own except -- so cycle history
    # silently returned EMPTY in production. Third instance of the V28
    # class, and the only one an exemption was hiding.
    cycle_length: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    flow_intensity: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class PeriodDayLog(Base):
    """One row per user per day on which anything was logged (mhn-spring's V5).

    Where the symptoms actually live. The note on ``PeriodTracking`` above
    explains why they are not there: V1 hung them off the cycle row, V5 dropped
    that column outright, and the mapping that survived pointed at nothing.
    **This table is the one with a writer**, and ``symptoms text[]`` is declared
    in V5 with a validated vocabulary behind it — mhn-spring's ``PeriodSymptom``
    enum, which is what the codes in the array come from.

    Deliberately independent of bleeding: a day with no flow and three symptoms
    is the ordinary mid-cycle entry, which is exactly the day worth reading.

    Only the three columns anything here needs are mapped. Given the history
    above, mapping a column this service does not read is a liability rather
    than future-proofing.
    """

    __tablename__ = "period_day_log"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    log_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    #: Codes, not labels — "lower_back_pain", not "Lower back pain".
    symptoms: Mapped[list | None] = mapped_column(
        sa.ARRAY(sa.String).with_variant(sa.JSON(), "sqlite"), nullable=True
    )


class UserThpSeries(Base):
    """mhn-spring's materialised biomarker trends feed (their V31).

    One row per (user, biomarker) holding EVERY reading the AI extraction has
    produced for that test, in the exact shape ``GET /files/biomarkers``
    returns — which is what the mobile apps draw their graphs from.

    Davi used to derive lab history by walking the newest 20 ``reports`` rows
    and re-parsing ``content.ai.extraction.results[]``, grouping by the raw
    printed test name. That disagreed with the app twice over: it saw only the
    last 20 reports, and "HbA1c" / "HBA1C" / "Hb A1c" were three parameters to
    us and one to them (they group on ``AiMarkers.normalize()``, stored here as
    ``thp_key``). Same biomarker, two different graphs.

    Read for the OWNER only. The upstream comment is explicit that the series
    is privacy-agnostic — private reports' readings are stored here too, and
    reading-level filtering against the viewer's accessible report ids is
    expected to happen at read time. Davi does not do that filtering, so family
    reads keep the older per-document path, which already honours
    ``req_read``/``acc_read`` and ``file_access_exclusions``.
    """

    __tablename__ = "user_thp_series"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    thp_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    reference_range: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    readings: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


# --------------------------------------------------------------------------- #
# Targets the reader set for themselves
# --------------------------------------------------------------------------- #
# Three tables, one shape: (metric, effective_from, value, unit). They are
# history, not settings — a row per change, so "my goal" is the newest row
# whose ``effective_from`` has arrived, never simply the newest row (a goal
# dated next Monday is not today's goal).
#
# Nothing in Davi read any of them, so a reader who had set a daily water
# target, a weight goal and a step goal in the app was told none of it existed
# when they asked here.
_BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class LifestyleLimit(Base):
    """A self-set ceiling on a lifestyle metric (coffee, alcohol, ...)."""

    __tablename__ = "lifestyle_limit"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    metric: Mapped[str] = mapped_column(
        _pg_enum("lifestyle_metric_enum", "water", "alcohol", "coffee",
                 "tea", "smoking", "energy_drink", "other_drink",
                 "caffeine_mg", "ethanol_g", "drink_volume_ml"),
        nullable=False,
    )
    effective_from: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    limit_value: Mapped[float | None] = mapped_column(sa.Numeric, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class BodyMeasurementGoal(Base):
    """A weight/BMI/body-fat target, with the direction the reader chose."""

    __tablename__ = "body_measurement_goal"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    type: Mapped[str] = mapped_column(
        _pg_enum("body_measurement_type_enum", "weight", "height", "bmi",
                 "body_fat", "muscle_mass", "water", "bone_mass",
                 "visceral_fat"),
        nullable=False,
    )
    effective_from: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    goal_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(
        _pg_enum("goal_direction_enum", "lose", "gain", "maintain"),
        nullable=True,
    )
    unit: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class SahhaGoal(Base):
    """A target on a wearable-derived metric (steps, sleep, ...)."""

    __tablename__ = "sahha_goal"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    goal_value: Mapped[float | None] = mapped_column(sa.Numeric, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

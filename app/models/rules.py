"""Versioned clinical config (rules, templates) and generated insight artifacts.

All clinical constants seeded into these tables ship as DRAFT — pending
clinician sign-off.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, JSONColumn, UUIDPrimaryKey


class RiskRule(Base, UUIDPrimaryKey):
    """Versioned rule config. Rules are DATA; predicates are code (pattern_key)."""

    __tablename__ = "risk_rules"

    rule_key: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    pattern_key: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    condition_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    tier: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    modifier: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    template_key: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    sensitive: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)


class InsightTemplate(Base, UUIDPrimaryKey):
    __tablename__ = "insight_templates"
    __table_args__ = (
        sa.UniqueConstraint(
            "template_key", "version", name="uq_insight_template_key_version"
        ),
    )

    template_key: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    locale: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="en-IN")
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="draft"
    )  # draft | approved


class InsightArtifact(Base, UUIDPrimaryKey, CreatedAt):
    """A rendered, reproducible insight for (user, condition)."""

    __tablename__ = "insight_artifacts"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    condition_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    tier: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    facts_used: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    fired_rules: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    template_key: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    template_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    pipeline_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    # active | superseded | held_for_review | suppressed
    # ("suppressed" is set by the clinician review queue; see
    #  app/api/v1/review.py and engine.LIVE_STATUSES.)
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="active")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("insight_artifacts.id"), nullable=True
    )
    recompute_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class PatternArtifact(Base, UUIDPrimaryKey, CreatedAt):
    """One computed behaviour pattern, stored so reads never compute.

    Mirrors ``InsightArtifact`` deliberately, because the invariant is the
    same one: "only the nightly sweep creates artifacts; the read path serves
    stored rows". `/api/v1/patterns` was computing ~14 queries on every screen
    load before this existed.

    ``content_hash`` covers the finding, not the moment it was taken, so a day
    where nothing changed produces NO new row. That is what makes day-wise
    history affordable: a row appears the day a pattern actually moves, rather
    than 7 pairs x 365 days x every reader.
    """

    __tablename__ = "pattern_artifacts"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    #: `exposure__outcome__lag` — the route id the detail screen uses.
    pattern_key: Mapped[str] = mapped_column(sa.String(96), nullable=False, index=True)
    exposure: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    lag: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: False when the minimum-days gate refused. Stored rather than dropped:
    #: the "not yet" card is built from these counts.
    enough_data: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    days_with: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    days_without: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    mean_with: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    mean_without: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    difference: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    favourable: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    #: The rendered card, so the read path does no wording work either.
    card: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    # active | superseded
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="active")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("pattern_artifacts.id"), nullable=True
    )
    computed_for: Mapped[date] = mapped_column(sa.Date, nullable=False)
    recompute_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

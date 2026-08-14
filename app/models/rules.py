"""Versioned clinical config (rules, templates) and generated insight artifacts.

All clinical constants seeded into these tables ship as DRAFT — pending
clinician sign-off.
"""

from __future__ import annotations

import uuid

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
    # active | superseded | held_for_review
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="active")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("insight_artifacts.id"), nullable=True
    )
    recompute_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

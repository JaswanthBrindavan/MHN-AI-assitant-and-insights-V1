"""Phase 7 — nightly sweep: full recompute + purge of old soft-deletes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.common import utcnow
from app.models.core import PedigreeCondition
from app.models.jobs import JobRun
from app.models.rules import InsightArtifact
from scripts.nightly_sweep import run_sweep
from scripts.seed_synthetic import seed_synthetic


@pytest.mark.asyncio
async def test_nightly_sweep_recomputes_and_records_job(db_session):
    await seed_synthetic(db_session)
    await db_session.commit()

    result = await run_sweep(db_session)
    await db_session.commit()

    assert result["users_recomputed"] == 3
    # Idempotent recompute: still exactly 5 active artifacts.
    active = (
        await db_session.execute(
            select(InsightArtifact).where(InsightArtifact.status == "active")
        )
    ).scalars().all()
    assert len(active) == 5

    job = (
        await db_session.execute(
            select(JobRun).where(JobRun.name == "nightly_sweep")
        )
    ).scalars().first()
    assert job is not None
    assert job.status == "succeeded"


@pytest.mark.asyncio
async def test_nightly_sweep_purges_old_soft_deletes(db_session):
    user_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
    now = utcnow()

    old = PedigreeCondition(
        user_id=user_id, slot="mother", condition_code="T2DM",
        condition_display="type 2 diabetes", onset_band="55_59",
        certainty="confirmed", provenance="self_report",
        soft_deleted=True, soft_deleted_at=now - timedelta(days=40),
    )
    recent = PedigreeCondition(
        user_id=user_id, slot="father", condition_code="HTN",
        condition_display="high blood pressure", onset_band="50_54",
        certainty="confirmed", provenance="self_report",
        soft_deleted=True, soft_deleted_at=now - timedelta(days=5),
    )
    db_session.add_all([old, recent])
    await db_session.flush()

    result = await run_sweep(db_session, now=now)
    await db_session.commit()

    assert result["conditions_purged"] == 1
    remaining = (
        await db_session.execute(
            select(PedigreeCondition).where(PedigreeCondition.user_id == user_id)
        )
    ).scalars().all()
    # Only the recently-deleted row survives.
    assert len(remaining) == 1
    assert remaining[0].condition_code == "HTN"

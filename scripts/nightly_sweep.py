"""Nightly sweep: full recompute for all users + purge old soft-deletes.

- Recomputes insights for every user with pedigree data (reads never compute;
  this is the sanctioned batch recompute).
- Hard-purges pedigree_conditions soft-deleted more than 30 days ago.
- Records a job_runs row for observability.

Run:  python -m scripts.nightly_sweep
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.insights.engine import recompute_insights
from app.models.common import utcnow
from app.models.core import PedigreeCondition
from app.models.jobs import JobRun

PURGE_AFTER_DAYS = 30


async def run_sweep(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or utcnow()
    job = JobRun(name="nightly_sweep", trigger="cron", status="running", started_at=now)
    db.add(job)
    await db.flush()

    try:
        user_ids = (
            await db.execute(select(PedigreeCondition.user_id).distinct())
        ).scalars().all()
        recomputed = 0
        for uid in user_ids:
            await recompute_insights(db, uid, reason="nightly_sweep")
            recomputed += 1

        cutoff = now - timedelta(days=PURGE_AFTER_DAYS)
        to_purge = (
            await db.execute(
                select(PedigreeCondition.id).where(
                    PedigreeCondition.soft_deleted.is_(True),
                    PedigreeCondition.soft_deleted_at.is_not(None),
                    PedigreeCondition.soft_deleted_at < cutoff,
                )
            )
        ).scalars().all()
        if to_purge:
            await db.execute(
                delete(PedigreeCondition).where(
                    PedigreeCondition.id.in_(to_purge)
                )
            )

        job.status = "succeeded"
        job.finished_at = utcnow()
        result = {"users_recomputed": recomputed, "conditions_purged": len(to_purge)}
    except Exception as exc:  # noqa: BLE001 — record failure on the job row
        job.status = "failed"
        job.finished_at = utcnow()
        job.error = str(exc)
        await db.flush()
        raise
    await db.flush()
    return result


async def _main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        result = await run_sweep(db)
        await db.commit()
    print(f"Nightly sweep complete: {result}")


if __name__ == "__main__":
    asyncio.run(_main())

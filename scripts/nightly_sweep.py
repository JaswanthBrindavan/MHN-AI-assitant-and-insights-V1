"""Nightly sweep: full recompute for all users + purge old soft-deletes.

- Recomputes insights for every user with pedigree data (reads never compute;
  this is the sanctioned batch recompute).
- Hard-purges pedigree_conditions soft-deleted more than 30 days ago.
- EXECUTES scheduled erasures whose grace period has expired.
- Applies retention to the chat transcript and the audit trail.
- Records a job_runs row for observability.

The last two commit as they go and are batched. They deliberately do NOT run
inside the recompute's transaction: both are destructive and unbounded in
principle, and this database is shared with mhn-spring and mhn-ai.

Run:  python -m scripts.nightly_sweep
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.episodes import purge_stale
from app.chat.erasure import execute_due
from app.chat.retention import purge_expired
from app.config import get_settings
from app.db import get_sessionmaker
from app.insights.engine import recompute_insights
from app.models.common import utcnow
from app.models.core import PedigreeCondition
from app.models.jobs import JobRun

PURGE_AFTER_DAYS = 30


async def run_sweep(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or utcnow()
    # actor_user_id stays NULL: scheduled work has no actor, and a NULL here
    # means "the system", never "an unknown user".
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
        # Symptom episodes nobody has mentioned in STALE_AFTER are over. Read
        # paths already filter them out; this is where the rows actually go,
        # because a chat turn is the wrong place to run a cleanup.
        episodes_purged = await purge_stale(db)

        result = {
            "users_recomputed": recomputed,
            "conditions_purged": len(to_purge),
            "episodes_purged": episodes_purged,
        }

        # Erasure and retention COMMIT AS THEY GO, so they run after the
        # recompute rather than inside its transaction. Both are destructive
        # and batched; holding them open with everything else would lock a
        # database two other services share.
        settings = get_settings()
        result.update(await execute_due(db))
        result.update(
            await purge_expired(
                db,
                message_days=settings.message_retention_days,
                receipt_days=settings.receipt_retention_days,
                batch_size=settings.retention_batch_size,
            )
        )
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

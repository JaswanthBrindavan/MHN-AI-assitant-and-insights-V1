"""Write behaviour-pattern artifacts. The only thing that creates them.

The invariant this exists to satisfy, from CLAUDE.md:

    Reads never compute: only `recompute_insights` (after pedigree writes and
    in the nightly sweep) creates artifacts. `GET /insights` and the
    data-query handler only serve stored rows.

`/api/v1/patterns` was computing ~14 queries per screen load. Now the sweep
writes and the route reads.

SUPERSEDE, NOT APPEND. `content_hash` covers the finding, so a day where
nothing changed writes NOTHING. A row appears the day a pattern actually
moves, which is what makes day-wise history affordable.

WHEN IT RUNS MATTERS MORE THAN THE HOUR. The wearable rollups only catch up
when mhn-spring reconciles overnight, which is why the window excludes today.
Run this before their reconciliation and it stores yesterday's pattern from a
partial rollup — correct-looking and wrong.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.rules import PatternArtifact
from app.patterns.core import Observation, content_hash
from app.patterns.facts import fact_for as lookup_fact
from app.patterns.render import to_card
from app.patterns.service import PAIRS, compute

logger = logging.getLogger(__name__)

_TITLES = {(p.exposure, p.outcome, p.lag): p.title for p in PAIRS}


def _hash_one(o: Observation) -> str:
    """Identity of ONE finding, so pairs supersede independently."""
    return content_hash([o])


async def recompute_patterns(
    db: AsyncSession,
    user_id,
    *,
    reason: str = "nightly_sweep",
    today: date | None = None,
    with_facts: bool = True,
) -> int:
    """Recompute every pair for one reader. Returns rows written.

    The general, clinician-reviewed sentence is looked up per pair unless
    ``with_facts`` is False. It is retrieved by code from the same Master
    Condition Profiles the chat quotes, never generated: if the corpus has
    nothing, the card carries no "in general" line. A missing fact costs a
    line; a wrong one costs the reader's trust.
    """
    observations = await compute(db, user_id, today=today)
    written = 0
    stamp = today or utcnow().date()

    for o in observations:
        digest = _hash_one(o)
        existing = (
            await db.execute(
                select(PatternArtifact).where(
                    PatternArtifact.user_id == user_id,
                    PatternArtifact.pattern_key == o.key,
                    PatternArtifact.status == "active",
                )
            )
        ).scalars().first()

        # Nothing changed: leave the row alone. This is the whole reason a
        # reader does not accumulate 7 rows a night.
        if existing is not None and existing.content_hash == digest:
            continue

        fact = None
        if with_facts:
            fact = await lookup_fact(db, o.exposure, o.outcome)

        fresh = PatternArtifact(
            user_id=user_id,
            pattern_key=o.key,
            exposure=o.exposure,
            outcome=o.outcome,
            lag=o.lag,
            enough_data=o.enough,
            days_with=o.days_with,
            days_without=o.days_without,
            mean_with=o.mean_with,
            mean_without=o.mean_without,
            difference=o.difference,
            favourable=o.favourable,
            card=to_card(
                o, title=_TITLES.get((o.exposure, o.outcome, o.lag), ""),
                fact=fact,
            ),
            content_hash=digest,
            status="active",
            computed_for=stamp,
            recompute_reason=reason,
        )
        db.add(fresh)
        await db.flush()

        if existing is not None:
            await db.execute(
                update(PatternArtifact)
                .where(PatternArtifact.id == existing.id)
                .values(status="superseded", superseded_by=fresh.id)
            )
        written += 1

    return written


async def active_patterns(db: AsyncSession, user_id) -> list[PatternArtifact]:
    """The read path. Stored rows only — this must never compute."""
    return list(
        (
            await db.execute(
                select(PatternArtifact)
                .where(
                    PatternArtifact.user_id == user_id,
                    PatternArtifact.status == "active",
                )
                .order_by(PatternArtifact.pattern_key)
            )
        ).scalars().all()
    )

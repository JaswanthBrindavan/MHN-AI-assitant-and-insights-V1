"""Behaviour patterns — the Insights and Correlations screens.

Separate from ``/api/v1/insights``, which is the FAMILY-HISTORY engine. The app
labels both screens "Insights"; in code they are different features and are
kept apart so nobody wires one into the other.

READS NEVER COMPUTE. These routes serve rows the nightly sweep wrote. The one
exception is a reader who has never been swept: they get one computation, it
is STORED, and every later request is a plain read. That fallback exists
because the sweep has never actually run in this deployment — `job_runs` is
empty — and without it the screen would be permanently blank rather than
merely stale, which would look like a data problem instead of a job nobody
scheduled.

Every figure is the reader's own recorded data compared against itself.
Nothing is graded, nothing is diagnosed, and no reply claims one thing caused
another — see ``app/patterns/render.py``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.db import get_db
from app.patterns.core import MIN_DAYS_PER_GROUP, WINDOW_DAYS
from app.patterns.engine import active_patterns, recompute_patterns

router = APIRouter(prefix="/patterns", tags=["patterns"])

SUBTITLE = (
    "What your own records did on the same days. These are patterns in your "
    "data, not proof that one caused the other."
)


async def _cards(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """Stored cards, computing once if this reader has never been swept."""
    rows = await active_patterns(db, user_id)
    if not rows:
        await recompute_patterns(db, user_id, reason="first_use")
        rows = await active_patterns(db, user_id)
    return [r.card or {} for r in rows]


@router.get("/correlations")
async def list_correlations(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screen 3 — every pair, and separately the ones still short of days.

    The waiting ones are RETURNED rather than hidden: screen 1's "3 more
    nights to unlock" is built from their counts, and a reader who can see
    what is coming is better served than one shown an empty list.
    """
    cards = await _cards(db, current_user)
    return {
        "window_days": WINDOW_DAYS,
        "min_days_per_group": MIN_DAYS_PER_GROUP,
        "subtitle": SUBTITLE,
        "correlations": [c for c in cards if c.get("enough_data")],
        "not_yet": [c for c in cards if not c.get("enough_data")],
    }


@router.get("/correlations/{key}")
async def correlation_detail(
    key: str,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screen 4 — one pair, as stored."""
    for card in await _cards(db, current_user):
        if card.get("key") == key:
            return card
    raise HTTPException(status_code=404, detail="No such pattern")

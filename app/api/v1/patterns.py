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
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.db import get_db
from app.patterns.core import MIN_DAYS_PER_GROUP, WINDOW_DAYS
from app.patterns.engine import active_patterns, recompute_patterns
from app.patterns.service import (
    OUTCOMES,
    TREND_METRICS,
    attention,
    daily_series,
    gather_yesterday,
    tracking_today,
    trend,
)
from app.patterns.yesterday import summarise_yesterday

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


@router.get("/summary")
async def summary(
    metric: str = "sleep_duration",
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screens 1 and 2 — "Building your baseline" and "This week".

    `building_baseline` is what screen 1 renders: true while no pair has
    enough days yet. `days_needed` comes from the closest pair rather than a
    guess, so "3 more nights to unlock" is a real number.
    """
    if metric not in TREND_METRICS:
        raise HTTPException(status_code=400, detail="Unknown metric")

    cards = await _cards(db, current_user)
    ready = [c for c in cards if c.get("enough_data")]
    waiting = [c for c in cards if not c.get("enough_data")]

    # How close the nearest pair is to unlocking. Screen 1's progress bar.
    needed = None
    if waiting:
        needed = min(
            max(0, MIN_DAYS_PER_GROUP - min(
                int(c.get("days_with") or 0), int(c.get("days_without") or 0)
            ))
            for c in waiting
        )

    series = await daily_series(db, current_user, metric, days=14)
    label, unit = OUTCOMES[metric][1], OUTCOMES[metric][2]
    return {
        "building_baseline": not ready,
        "days_needed": needed,
        "this_week": {
            "metric": metric,
            "label": label,
            "unit": unit,
            # Absent days are simply not here. The client must not zero-fill:
            # a day the device did not sync is not a day of zero sleep.
            "series": [
                {"day": p.day.isoformat(), "value": round(p.value, 1)}
                for p in series[-7:]
            ],
            "trend": trend(series),
        },
        # Screen 2 shows the first few and links to the full list.
        "correlations": ready[:3],
        "correlations_total": len(ready),
        "not_yet_total": len(waiting),
        # Screen 2's "Safety" row, and screen 5 in full.
        "attention": await attention(db, current_user),
        "subtitle": SUBTITLE,
    }


@router.get("/attention")
async def worth_a_look(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screen 5 — readings that have moved against the reader's OWN baseline.

    Not a grade. Everywhere else a wearable number is never graded, because
    there are no reference ranges for sleep or HRV and inventing them would be
    inventing clinical content. This compares the reader to themselves, states
    both figures, and points at a doctor rather than at a conclusion.
    """
    return {"items": await attention(db, current_user)}


@router.get("/yesterday")
async def yesterday_at_a_glance(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The home-screen card — two lines about the day just gone.

    Here rather than in each app because three clients render this card, and a
    ladder of rules plus a page of copy would otherwise be written three times
    and drift three ways. It is also almost entirely wording, and wording that
    lives in a client can only be changed by shipping an app-store release.

    `as_of` is the day the lines describe: **yesterday, not today**. A day in
    progress is not a day, and the wearable rollups only catch up when Spring
    reconciles overnight. Clients should show the date rather than assume it —
    a reader opening the app at 00:30 is being told about the day before the
    one they just finished.

    `has_data` false means nothing was recorded at all, and there is no card to
    draw. That is deliberately distinct from a steady day, which HAS data and
    says so: "no major changes" and "nothing recorded" look identical on screen
    and mean opposite things.

    Owner-scoped, and only ever the caller's own row. There is no family path
    here on purpose: `symptom_logs` has no sharing model, and the other family
    reads honour `req_read`/`acc_read` and `file_access_exclusions` that this
    would bypass entirely.
    """
    # Read the clock ONCE and hand the same date to both. Two calls to the clock
    # can straddle midnight, and the result is a reply that names one day in
    # `as_of` and describes another — a window in the wording that is not the
    # window queried.
    today = tracking_today()
    facts = await gather_yesterday(db, current_user, today=today)
    said = summarise_yesterday(facts)
    day = (today - timedelta(days=1)).isoformat()

    if said is None:
        return {"as_of": day, "has_data": False, "headline": None, "detail": None}
    return {
        "as_of": day,
        "has_data": True,
        "headline": said.headline,
        "detail": said.detail,
    }

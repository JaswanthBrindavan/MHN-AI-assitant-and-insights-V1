"""Behaviour patterns — the Insights and Correlations screens.

Separate from ``/api/v1/insights``, which is the FAMILY-HISTORY engine. The app
labels both screens "Insights"; in code they are different features and are
kept apart so nobody wires one into the other.

Every figure here is the reader's own recorded data compared against itself.
Nothing is graded, nothing is diagnosed, and no reply claims one thing caused
another — see ``app/patterns/render.py`` for how the sentence is built and why
it is split into observation, hedge and general fact.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.db import get_db
from app.patterns.core import MIN_DAYS_PER_GROUP, WINDOW_DAYS
from app.patterns.render import to_card
from app.patterns.service import PAIRS, compute

router = APIRouter(prefix="/patterns", tags=["patterns"])

_TITLES = {(p.exposure, p.outcome, p.lag): p.title for p in PAIRS}


def _title(o) -> str:
    return _TITLES.get((o.exposure, o.outcome, o.lag), "")


@router.get("/correlations")
async def list_correlations(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screen 3 — every pair, whether or not it has enough days yet.

    The ones without enough days are RETURNED rather than hidden: screen 1's
    "no patterns yet" card is built from them, and a reader who can see that
    three more nights would unlock something is better served than one who
    sees an empty list.
    """
    observations = await compute(db, current_user)
    ready = [o for o in observations if o.enough]
    waiting = [o for o in observations if not o.enough]
    return {
        "window_days": WINDOW_DAYS,
        "min_days_per_group": MIN_DAYS_PER_GROUP,
        # The subtitle the design puts at the top of the screen. Worded as an
        # observation, because that is what it is.
        "subtitle": (
            "What your own records did on the same days. These are patterns in "
            "your data, not proof that one caused the other."
        ),
        "correlations": [to_card(o, title=_title(o)) for o in ready],
        "not_yet": [to_card(o, title=_title(o)) for o in waiting],
    }


@router.get("/correlations/{key}")
async def correlation_detail(
    key: str,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Screen 4 — one pair, with the two groups the comparison used.

    `series` is what the "Exposure vs Outcome" chart draws: one point per
    measured day, flagged with whether the habit was logged that day. Days the
    device did not sync are absent from both groups rather than counted as
    baseline, so the chart cannot imply a reading that was never taken.
    """
    observations = await compute(db, current_user)
    match = next((o for o in observations if o.key == key), None)
    if match is None:
        raise HTTPException(status_code=404, detail="No such pattern")
    card = to_card(match, title=_title(match))
    card["exposure_days"] = [d.isoformat() for d in match.contributing_days]
    return card

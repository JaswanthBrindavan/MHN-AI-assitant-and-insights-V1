"""Build the daily series the pattern core compares, and word the result.

Reads only. Every query is scoped to one user and bounded by the window, and
each pair is computed in its own SAVEPOINT so one missing rollup cannot cost
the whole screen.

WHAT PAIRS EXIST, and why these. The owner's brief, verbatim: "Correlations
with THPs will happen in v2.. not now.. so need with lhps and vitals". So the
exposures are the lifestyle logs, and the outcomes are the wearable rollups,
the recorded vitals and the daily mood score. Lab values from reports are NOT
paired in v1.

TWO CARDS FROM THE DESIGN ARE DELIBERATELY ABSENT:

* "Late caffeine -> LIGHTER sleep" and "deep sleep drops 12%". Sahha's
  catalogue has duration, debt, latency, interruptions and regularity — there
  is no deep-sleep or sleep-stage metric anywhere in it, so that figure could
  only be invented. The SHORTER-sleep half is real and is computed.
* "Evening workout -> longer sleep onset". Activity arrives as daily
  durations, never session times, so evening cannot be told from morning —
  and there is no workout tracking yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.coredata import (
    LifestyleLog,
    MoodLog,
    SahhaDailyTotal,
    VitalReading,
)
from app.patterns.core import (
    LAG_NEXT_DAY,
    LAG_SAME_DAY,
    WINDOW_DAYS,
    DayValue,
    Observation,
    observe,
)
from app.telemetry import record_fail_open

logger = logging.getLogger(__name__)

# Reader-facing names. The outcome noun is used mid-sentence, so it is phrased
# to read naturally after "your recorded ...".
OUTCOMES: dict[str, tuple[str, str, str]] = {
    # key: (source, label, unit)
    "sleep_duration": ("wearable", "sleep", "min"),
    "steps": ("wearable", "steps", ""),
    "heart_rate_resting": ("wearable", "resting heart rate", "bpm"),
    "heart_rate_variability_sdnn": ("wearable", "HRV", "ms"),
    "blood_pressure": ("vital", "systolic blood pressure", "mmHg"),
    "blood_sugar": ("vital", "blood sugar", "mg/dL"),
    "heart_rate": ("vital", "heart rate", "bpm"),
    "spo2": ("vital", "oxygen saturation", "%"),
    "mood": ("mood", "mood score", ""),
}

EXPOSURES: dict[str, str] = {
    "coffee": "coffee",
    "tea": "tea",
    "alcohol": "alcohol",
    "smoking": "smoking",
    "water_low": "water",   # a below-median water day; see `_low_days`
}


@dataclass(frozen=True)
class Pair:
    exposure: str
    outcome: str
    lag: str
    #: Only count the habit when logged at or after this hour, local to the
    #: stored timestamp. This is what makes "LATE caffeine" a real exposure
    #: rather than "any coffee": `lifestyle_log.logged_at` is a timestamp, so
    #: the hour is available without any new data.
    after_hour: int | None = None
    title: str = ""


#: The v1 catalogue. Deliberately small: every pair here has data behind it.
PAIRS: tuple[Pair, ...] = (
    Pair("coffee", "sleep_duration", LAG_SAME_DAY, 15,
         "Late caffeine and your sleep"),
    Pair("alcohol", "heart_rate_resting", LAG_NEXT_DAY, None,
         "Alcohol and your resting heart rate"),
    Pair("alcohol", "sleep_duration", LAG_SAME_DAY, None,
         "Alcohol and your sleep"),
    Pair("smoking", "heart_rate_variability_sdnn", LAG_SAME_DAY, None,
         "Smoking and your HRV"),
    Pair("water_low", "mood", LAG_NEXT_DAY, None,
         "Low-water days and your mood"),
    Pair("coffee", "heart_rate", LAG_SAME_DAY, None,
         "Coffee and your heart rate"),
    Pair("alcohol", "blood_pressure", LAG_NEXT_DAY, None,
         "Alcohol and your blood pressure"),
)


def window_bounds(today: date | None = None) -> tuple[date, date]:
    """[start, end) covering the last WINDOW_DAYS complete days.

    Today is EXCLUDED: a day in progress is not a day, and the wearable
    rollups only catch up when Spring reconciles overnight.
    """
    end = today or utcnow().date()
    return end - timedelta(days=WINDOW_DAYS), end


async def _logged_days(
    db: AsyncSession, user_id, log_type: str, start: date, end: date,
    after_hour: int | None,
) -> set[date]:
    """Days the habit was logged. With `after_hour`, only late entries count."""
    q = select(LifestyleLog.logged_at).where(
        LifestyleLog.user_id == user_id,
        LifestyleLog.log_type == log_type,
        sa.func.date(LifestyleLog.logged_at) >= start,
        sa.func.date(LifestyleLog.logged_at) < end,
    )
    rows = (await db.execute(q)).scalars().all()
    days: set[date] = set()
    for ts in rows:
        if ts is None:
            continue
        if after_hour is not None and ts.hour < after_hour:
            continue
        days.add(ts.date())
    return days


async def _low_days(
    db: AsyncSession, user_id, log_type: str, start: date, end: date,
) -> set[date]:
    """Days the reader logged LESS of something than their own median.

    "A low water day" is only meaningful against that person's own habit, not
    a target we invented: someone who logs 1,200 ml most days has a different
    low day from someone who logs 3,000.
    """
    q = (
        select(
            sa.func.date(LifestyleLog.logged_at).label("d"),
            sa.func.sum(LifestyleLog.quantity).label("total"),
        )
        .where(
            LifestyleLog.user_id == user_id,
            LifestyleLog.log_type == log_type,
            sa.func.date(LifestyleLog.logged_at) >= start,
            sa.func.date(LifestyleLog.logged_at) < end,
        )
        .group_by(sa.func.date(LifestyleLog.logged_at))
    )
    rows = [(r.d, float(r.total or 0)) for r in (await db.execute(q)).all()]
    if len(rows) < 4:          # too few days to have a "usual" at all
        return set()
    totals = sorted(t for _, t in rows)
    mid = len(totals) // 2
    median = (
        totals[mid] if len(totals) % 2 else (totals[mid - 1] + totals[mid]) / 2
    )
    return {
        (d if isinstance(d, date) else date.fromisoformat(str(d)))
        for d, t in rows if t < median
    }


async def _outcome_series(
    db: AsyncSession, user_id, outcome: str, start: date, end: date,
) -> list[DayValue]:
    """One value per day for an outcome, from whichever table owns it."""
    source = OUTCOMES[outcome][0]

    if source == "wearable":
        rows = (
            await db.execute(
                select(SahhaDailyTotal.bucket_start, SahhaDailyTotal.total,
                       SahhaDailyTotal.entries)
                .where(
                    SahhaDailyTotal.user_id == user_id,
                    SahhaDailyTotal.metric == outcome,
                    SahhaDailyTotal.entries >= 1,
                    SahhaDailyTotal.bucket_start >= start,
                    SahhaDailyTotal.bucket_start < end,
                )
            )
        ).all()
        from app.coredata.service import _headline

        return [
            DayValue(r.bucket_start, _headline(outcome, float(r.total), r.entries))
            for r in rows
        ]

    if source == "vital":
        rows = (
            await db.execute(
                select(
                    sa.func.date(VitalReading.recorded_at).label("d"),
                    sa.func.avg(VitalReading.value_primary).label("v"),
                )
                .where(
                    VitalReading.user_id == user_id,
                    VitalReading.vital_type == outcome,
                    sa.func.date(VitalReading.recorded_at) >= start,
                    sa.func.date(VitalReading.recorded_at) < end,
                )
                .group_by(sa.func.date(VitalReading.recorded_at))
            )
        ).all()
        return [
            DayValue(
                r.d if isinstance(r.d, date) else date.fromisoformat(str(r.d)),
                float(r.v),
            )
            for r in rows
        ]

    rows = (
        await db.execute(
            select(MoodLog.log_date, MoodLog.score).where(
                MoodLog.user_id == user_id,
                MoodLog.log_date >= start,
                MoodLog.log_date < end,
            )
        )
    ).all()
    return [DayValue(r.log_date, float(r.score)) for r in rows]


async def compute(
    db: AsyncSession, user_id, *, today: date | None = None
) -> list[Observation]:
    """Every pair in the catalogue, computed over the window. Never raises."""
    start, end = window_bounds(today)
    out: list[Observation] = []
    for pair in PAIRS:
        try:
            async with db.begin_nested():
                log_type = EXPOSURES[pair.exposure]
                days = (
                    await _low_days(db, user_id, log_type, start, end)
                    if pair.exposure.endswith("_low")
                    else await _logged_days(
                        db, user_id, log_type, start, end, pair.after_hour
                    )
                )
                series = await _outcome_series(
                    db, user_id, pair.outcome, start, end
                )
            out.append(
                observe(pair.exposure, pair.outcome, days, series, lag=pair.lag)
            )
        except Exception:  # noqa: BLE001 — one pair must not cost the screen
            logger.warning(
                "pattern pair failed: %s/%s", pair.exposure, pair.outcome,
                exc_info=True,
            )
            record_fail_open("patterns_pair")
    return out

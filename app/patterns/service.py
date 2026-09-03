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
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import (
    tracking_day_bounds,
    tracking_today,
    tracking_zone,
    utcnow,
)
from app.models.coredata import (
    LifestyleDailyTotal,
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


# --------------------------------------------------------------------------- #
# Screen 2 — "This week", and screen 5 — "Worth a look"
# --------------------------------------------------------------------------- #
#: What the weekly chart can show. Each is a daily series the reader already
#: has; nothing here is derived or modelled.
TREND_METRICS = (
    "sleep_duration", "steps", "heart_rate_resting",
    "heart_rate_variability_sdnn",
)


async def daily_series(
    db: AsyncSession, user_id, metric: str, days: int = 14,
    *, today: date | None = None,
) -> list[DayValue]:
    """The last `days` complete days for one metric, oldest first."""
    end = today or utcnow().date()
    start = end - timedelta(days=days)
    series = await _outcome_series(db, user_id, metric, start, end)
    return sorted(series, key=lambda p: p.day)


def trend(series: list[DayValue]) -> dict | None:
    """This week against the one before it. A direction, never a verdict.

    The design's caption is "Recovery trending up — Great job! Your body is
    recovering well this week." The first half is a fact about the numbers and
    is kept. The second is a grade on a wearable reading, which this product
    does not do anywhere else, so it is not written here either.
    """
    if len(series) < 8:
        return None
    ordered = sorted(series, key=lambda p: p.day)
    this_week, last_week = ordered[-7:], ordered[-14:-7]
    if len(last_week) < 3 or len(this_week) < 3:
        return None
    now = sum(p.value for p in this_week) / len(this_week)
    before = sum(p.value for p in last_week) / len(last_week)
    if before == 0:
        return None
    change = (now - before) / abs(before)
    if abs(change) < 0.05:
        direction = "steady"
    else:
        direction = "up" if change > 0 else "down"
    return {
        "direction": direction,
        "this_week_mean": round(now, 1),
        "last_week_mean": round(before, 1),
        "days_this_week": len(this_week),
    }


async def attention(
    db: AsyncSession, user_id, *, today: date | None = None
) -> list[dict]:
    """Screen 5 — readings that have moved against the reader's own baseline.

    Its own SAVEPOINT per metric for the same reason the pairs have one: a
    missing rollup must not blank the tab.
    """
    from app.patterns.baseline import WATCHED
    from app.patterns.baseline import detect as _detect
    from app.patterns.baseline import to_card as _card

    out: list[dict] = []
    for metric in WATCHED:
        try:
            async with db.begin_nested():
                series = await daily_series(db, user_id, metric, days=28,
                                            today=today)
            found = _detect(metric, series, today=today)
            if found is not None:
                out.append(_card(found))
        except Exception:  # noqa: BLE001 — one metric must not cost the screen
            logger.warning("attention check failed: %s", metric, exc_info=True)
            record_fail_open("patterns_attention")
    return out


# --------------------------------------------------------------------------- #
# "Yesterday at a glance" — the home-screen card
# --------------------------------------------------------------------------- #
#: Wearable and vital metrics the card reads, each with the absolute change
#: that counts as a move, in that metric's OWN unit (see `OUTCOMES`).
#:
#: Absolute rather than a shared percentage because the metrics do not share a
#: scale: eleven steps more than usual is a rounding artefact, three beats per
#: minute on a resting heart rate is not, and no single ratio is right for both.
YESTERDAY_METRICS: dict[str, float] = {
    "sleep_duration": 30.0,                 # minutes
    "steps": 1500.0,                        # steps
    "heart_rate_resting": 3.0,              # bpm
    "heart_rate_variability_sdnn": 6.0,     # ms
    "spo2": 2.0,                            # percentage points
    "mood": 2.0,                            # 1-10 slider stops
}

#: Manual trackers, in the unit `lifestyle_daily_total.total` keeps for each.
YESTERDAY_HABITS: dict[str, float] = {
    "water": 500.0,     # ml
    "coffee": 1.0,      # servings
    "alcohol": 15.0,    # ml
}

#: How far back the baseline looks. Two weeks is enough to survive a weekend
#: and short enough to still describe how somebody is living now.
YESTERDAY_BASELINE_DAYS = 14

#: mhn-spring's `PeriodSymptom` entries carrying `redFlag = true`.
#:
#: Duplicated from a Java enum, knowingly. The alternative is an HTTP call to
#: Spring on a home-screen render, and that enum's own comment says the list
#: "changes about never" and that adding one is a reviewed code change. The
#: cost of drift here is a symptom losing its place in the ordering — it is
#: still shown, and nothing is diagnosed either way.
SPRING_RED_FLAG_SYMPTOMS = frozenset({
    "clots_large", "flooding", "spotting_between", "severe_pain",
    "bleeding_after_menopause",
})

#: The zone the READER's own calendar days are cut in.
#:
#: Named for the reader, not for `app.tracking.zone`, because they are not the
#: same thing and calling this "the tracking zone" is what led an earlier draft
#: to apply it to tables it does not govern.
#:
#: **It governs exactly two things here.** `period_day_log.log_date` is a
#: `@PathVariable` on `PUT /period/days/{date}` — the CLIENT sends the date, so
#: it is the reader's own local calendar day and never passes through any server
#: zone at all. And `symptom_logs.created_at`, which this service writes as a
#: UTC instant, is bounded below by converting from this zone, because the
#: reader's "yesterday" is theirs rather than the server's.
#:
#: **It does NOT govern the day-bucketed rollups.** `lifestyle_daily_total` and
#: `sahha_daily_total` carry a day mhn-spring resolved at write time in
#: `app.tracking.zone`, and that property is **unset in the deployed service**
#: (`application.properties`: `app.tracking.zone=${TRACKING_ZONE:}`), so it
#: falls back to the JVM default and those buckets are UTC today. Its own
#: startup warning says so. No UTC bucket matches a reader's day exactly, so
#: those are read on the bucket carrying the SAME DATE — the closest one by a
#: wide margin, covering 18.5 of the reader's 24 hours against 5.5 for the
#: bucket before it. Approximate, knowingly, and named as such rather than
#: dressed up as a match.
#:
#: Setting `TRACKING_ZONE=Asia/Kolkata` on the Spring service collapses all of
#: this into one anchor and is the real fix; it would also stop new rows
#: disagreeing with V1's and V35's backfills, which hardcode Asia/Kolkata and
#: are therefore already inconsistent with everything written since.
#:
#: A fixed offset rather than `ZoneInfo("Asia/Kolkata")`: India has never
#: observed daylight saving, so +05:30 IS the zone rather than an approximation
#: of it, and a named zone needs the IANA database, which Windows does not ship
#: — `ZoneInfoNotFoundError` on any machine without `tzdata`.
# Moved to `app/models/common.py`, beside `utcnow()`, and the offset is now
# `Settings.tracking_zone_offset_minutes`. Two reasons: `app/coredata/service.py`
# needs the same anchor and importing it from here would invert the layering,
# and the value belongs in configuration rather than a constant — assuming a
# value for another service's property is what caused this whole class.
#
# `TRACKING_ZONE=Asia/Kolkata` has been SET on the Spring service and is NOT
# yet live: the property is read at startup, and the deployments that change
# created are awaiting approval, so the running JVM still resolves `Etc/UTC`.
# The variable existing in a dashboard and the running process having read it
# are two different facts, and only the first is established — which is the
# same shape as the assumption that started this whole thread.
#
# So the three anchors are still three, the `server_day` split below is still
# load-bearing, and the rollup readers stay on the UTC day until a restart is
# confirmed from the startup log. When it is, they move TOGETHER — this file's
# windows, `calendar_window`, `targets` and `handle_correlation_query` — because
# V2 promises a limit and the total it bounds agree about where a day begins,
# and half a migration breaks that guarantee rather than keeping it.
#
# Keep the `day`/`server_day` split even after that. They are distinct in
# principle and equal only by configuration; collapsing them turns a documented
# coincidence into an invisible assumption, and someone can unset the variable.
#
# Pinning also reconciles NOTHING. Rows written before the restart stay
# UTC-bucketed, so a window reaching back across it mixes two anchors 5.5 hours
# apart — weighted toward readers whose history is mostly older rows, and it
# heals as those age out.
READER_ZONE = tracking_zone()

#: At most this many symptoms are named. Two lines cannot list a whole day.
MAX_SYMPTOMS = 3

#: Sort floor for a ticked symptom, which carries no time of its own.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _by_day(series: list[DayValue]) -> dict[date, float]:
    return {p.day: p.value for p in series}


def _signal_for(by_day: dict[date, float], day: date, *, min_change: float, span: int):
    """Yesterday's figure against the days behind it, or None if unrecorded."""
    from app.patterns.yesterday import baseline_of, day_signal

    value = by_day.get(day)
    if value is None:
        return None
    behind = [by_day.get(day - timedelta(days=n)) for n in range(1, span + 1)]
    return day_signal(value, baseline_of(behind), min_change=min_change)


async def _habit_totals(
    db: AsyncSession, user_id, start: date, end: date,
) -> dict[str, dict[date, float]]:
    """Daily totals per habit, from mhn-spring's own rollup.

    The rollup rather than `lifestyle_log` because `bucket_start` was assigned
    in `app.tracking.zone` at write time, and that is the day boundary a
    wearable night is aligned to. `date(logged_at)` would be the UTC day, so a
    late-evening coffee would land against the wrong night's sleep.
    """
    rows = (
        await db.execute(
            select(
                LifestyleDailyTotal.metric,
                LifestyleDailyTotal.bucket_start,
                LifestyleDailyTotal.total,
            ).where(
                LifestyleDailyTotal.user_id == user_id,
                LifestyleDailyTotal.metric.in_(tuple(YESTERDAY_HABITS)),
                LifestyleDailyTotal.bucket_start >= start,
                LifestyleDailyTotal.bucket_start <= end,
            )
        )
    ).all()

    out: dict[str, dict[date, float]] = {m: {} for m in YESTERDAY_HABITS}
    for r in rows:
        out.setdefault(r.metric, {})[r.bucket_start] = float(r.total)
    return out


async def _symptoms_on(db: AsyncSession, user_id, day: date) -> tuple[str, ...]:
    """What the reader said, and what they ticked, on one day."""
    return await symptoms_between(db, user_id, day, day)


async def ticked_between(
    db: AsyncSession, user_id, since: date, until: date,
) -> tuple[tuple[str, int], ...]:
    """Cycle-tracking symptoms over an INCLUSIVE range, as (phrase, rank).

    Split out of `symptoms_between` so a caller that ALREADY holds the chat
    side can read just this one without a second pass over `symptom_logs`. The
    health summary is that caller: it renders its own richer view of
    `symptom_logs` (counts, dates, whether the episode is still open) and needs
    only the codes that never reach chat.

    The normalisation stays HERE, which is the point. mhn-spring stores
    `PeriodSymptom` codes ("lower_back_pain") and chat stores the phrase
    somebody typed ("lower back pain"); both must reduce to one spelling or a
    reader sees the same complaint twice. Callers may difference the result
    against phrases they already hold, because both sides are the same
    lowercase spaced form — but no caller should be converting codes itself.

    Rank mirrors `symptoms_between`: a Spring red flag ranks with the
    high-severity chat rows, anything else alongside an ordinary mention.
    """
    from app.models.coredata import PeriodDayLog

    rows = (
        await db.execute(
            select(PeriodDayLog.symptoms).where(
                PeriodDayLog.user_id == user_id,
                PeriodDayLog.log_date >= since,
                PeriodDayLog.log_date <= until,
            )
        )
    ).scalars().all()

    out: dict[str, int] = {}
    for codes in rows or []:
        for code in codes or []:
            phrase = str(code).replace("_", " ").strip().lower()
            if not phrase:
                continue
            rank = 1 if str(code) in SPRING_RED_FLAG_SYMPTOMS else 2
            out[phrase] = min(rank, out.get(phrase, rank))
    return tuple(out.items())


async def symptoms_between(
    db: AsyncSession, user_id, since: date, until: date,
    *, limit: int = MAX_SYMPTOMS,
) -> tuple[str, ...]:
    """What the reader said, and what they ticked, over an INCLUSIVE range.

    Two sources, and they are not interchangeable:

    * `symptom_logs` — anything mentioned in chat. Written by `log_symptom`
      (ordinary symptoms, always `risk_level = "none"`) and by `open_or_touch`
      (red-flag terms, `high` or `emergency`). **Most rows are `none`, and that
      is normal** — a headache is not a reason to tell somebody to seek care.
      Filtering on severity would leave this empty for most readers most days,
      so severity ORDERS and never excludes.
    * `period_day_log.symptoms` — codes the reader ticked in cycle tracking.
      These never reach chat, so they are invisible to the table above.

    `symptom_logs` is append-only, one row per mention, so a reader who said
    "headache" four times yesterday has four rows. De-duplicated here, or the
    card would name it four times.

    **The two sources spell things differently and are merged on the spelling.**
    mhn-spring stores `PeriodSymptom` CODES ("lower_back_pain"); chat stores the
    free phrase somebody typed ("lower back pain"). Both are normalised to the
    same lowercase, spaced form and then keyed on it, so one complaint logged
    both ways is named once rather than appearing twice in two spellings. Any
    caller merging these elsewhere would have to solve that again, which is the
    reason this returns finished phrases rather than rows.

    ``limit`` defaults to `MAX_SYMPTOMS`, which is sized for a two-line card.
    The health summary passes a larger one: it is a full readout of the record
    rather than a glance, and silently dropping the fourth symptom a reader
    logged would be the summary asserting something it did not check.
    """
    from app.models.chat import SymptomLog
    from app.triage.red_flags import LEVEL_ORDER

    # phrase -> (rank, when). Lower rank first, then most recently said.
    ranked: dict[str, tuple[int, datetime]] = {}

    said = (
        await db.execute(
            select(SymptomLog.symptom, SymptomLog.risk_level, SymptomLog.created_at)
            .where(
                SymptomLog.user_id == user_id,
                SymptomLog.created_at >= tracking_day_bounds(since)[0],
                SymptomLog.created_at < tracking_day_bounds(until)[1],
            )
        )
    ).all()
    for row in said:
        phrase = (row.symptom or "").strip().lower()
        if not phrase:
            continue
        rank = 2 - LEVEL_ORDER.get(row.risk_level or "none", 0)
        when = row.created_at or _EPOCH
        seen = ranked.get(phrase)
        if seen is None or rank < seen[0] or (rank == seen[0] and when > seen[1]):
            ranked[phrase] = (rank, when)

    for phrase, rank in await ticked_between(db, user_id, since, until):
        seen = ranked.get(phrase)
        if seen is None:
            ranked[phrase] = (rank, _EPOCH)
        elif rank < seen[0]:
            ranked[phrase] = (rank, seen[1])

    ordered = sorted(ranked.items(), key=lambda kv: (kv[1][0], -kv[1][1].timestamp()))
    return tuple(phrase for phrase, _ in ordered[:limit])


async def gather_yesterday(db: AsyncSession, user_id, *, today: date | None = None):
    """Everything the card may say, for the last COMPLETE day.

    Yesterday rather than today, for the reason `window_bounds` gives: a day in
    progress is not a day, and the wearable rollups only catch up when Spring
    reconciles overnight.

    Every read is independent and every absence is honest. A reader with no
    wearable simply arrives with fewer signals, and the ladder then says less
    rather than inventing a figure to say it about. Each read also gets its own
    SAVEPOINT, like `attention` above: one missing rollup must not blank a card
    that four other sources could still fill.
    """
    from app.patterns.yesterday import YesterdayFacts

    # The reader's calendar, because the card is about THEIR day.
    end = today or tracking_today()
    day = end - timedelta(days=1)
    span = YESTERDAY_BASELINE_DAYS

    # The rollups are bucketed on a day mhn-spring resolved in
    # `app.tracking.zone`, which is unset and therefore UTC — so no bucket
    # matches the reader's day exactly. The bucket with the SAME DATE is still
    # far and away the closest one: a reader's 2 Sep runs 1 Sep 18:30Z to 2 Sep
    # 18:30Z, which the UTC bucket for 2 Sep covers for 18.5 of its 24 hours,
    # against 5.5 for the bucket before it. An earlier version of this reached
    # for the previous bucket on the grounds that the rollups are UTC; that is
    # true and the conclusion was backwards, and it put a Monday figure in a
    # sentence about Tuesday for the five and a half hours the anchors differ.
    #
    # So there is one day here, and `as_of` names the one that was read.

    series: dict[str, dict[date, float]] = {}
    for metric in YESTERDAY_METRICS:
        try:
            async with db.begin_nested():
                got = await daily_series(db, user_id, metric, days=span + 2, today=end)
            series[metric] = _by_day(got)
        except Exception:  # noqa: BLE001 — one metric must not blank the card
            logger.warning("yesterday metric failed: %s", metric, exc_info=True)
            record_fail_open("patterns_yesterday")
            series[metric] = {}

    try:
        async with db.begin_nested():
            habits = await _habit_totals(db, user_id, day - timedelta(days=span), day)
    except Exception:  # noqa: BLE001
        logger.warning("yesterday habits failed", exc_info=True)
        record_fail_open("patterns_yesterday")
        habits = {m: {} for m in YESTERDAY_HABITS}

    try:
        async with db.begin_nested():
            symptoms = await _symptoms_on(db, user_id, day)
    except Exception:  # noqa: BLE001 — the top rung must not 500 the card
        logger.warning("yesterday symptoms failed", exc_info=True)
        record_fail_open("patterns_yesterday")
        symptoms = ()

    def metric_signal(name: str):
        return _signal_for(
            series.get(name, {}), day,
            min_change=YESTERDAY_METRICS[name], span=span,
        )

    def habit_signal(name: str):
        return _signal_for(
            habits.get(name, {}), day,
            min_change=YESTERDAY_HABITS[name], span=span,
        )

    return YesterdayFacts(
        symptoms=symptoms,
        sleep_minutes=metric_signal("sleep_duration"),
        steps=metric_signal("steps"),
        resting_heart_rate=metric_signal("heart_rate_resting"),
        hrv=metric_signal("heart_rate_variability_sdnn"),
        spo2=metric_signal("spo2"),
        mood=metric_signal("mood"),
        water_ml=habit_signal("water"),
        caffeine_cups=habit_signal("coffee"),
        alcohol_ml=habit_signal("alcohol"),
    )

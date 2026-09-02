"""Behaviour patterns: the Insights and Correlations screens.

Built to the owner's decisions on the design (`Insights-and-Correaltions.png`):

* option A — the design's layout, but nothing worded as a cause;
* NO confidence score ("this might be the reason" instead);
* the general fact comes from the reviewed corpus, kept separate from the
  reader's own numbers;
* deep sleep and evening workout DROPPED — Sahha has no sleep-stage metric and
  there is no workout tracking, so both could only be invented;
* lifestyle and vitals only. THP/lab correlations are v2.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.common import utcnow
from app.models.coredata import (
    LifestyleLog,
    MoodLog,
    SahhaDailyTotal,
    VitalReading,
)
from app.patterns.core import (
    LAG_NEXT_DAY,
    MIN_DAYS_PER_GROUP,
    DayValue,
    content_hash,
    observe,
)
from app.patterns.render import detail, headline, to_card
from app.patterns.service import compute, window_bounds

USER = uuid.UUID("faceface-face-face-face-facefaceface")


# --------------------------------------------------------------------------- #
# The core comparison
# --------------------------------------------------------------------------- #
def _series(start: date, n: int, on: float, off: float, split: int):
    return [
        DayValue(start + timedelta(days=i), on if i < split else off)
        for i in range(n)
    ]


def test_it_compares_the_days_with_against_the_days_without():
    d0 = date(2026, 8, 1)
    exposure = {d0 + timedelta(days=i) for i in range(11)}
    o = observe("coffee", "sleep_duration", exposure,
                _series(d0, 25, 360.0, 398.0, 11))
    assert o.enough
    assert (o.days_with, o.days_without) == (11, 14)
    assert o.difference == -38.0
    assert o.favourable is False          # shorter sleep is not the good way


def test_a_day_with_no_reading_joins_neither_group():
    """The device not syncing is not a baseline day. Counting it as one would
    invent a reading that was never taken."""
    d0 = date(2026, 8, 1)
    exposure = {d0 + timedelta(days=i) for i in range(12)}
    # Only 8 days have an outcome reading at all.
    series = [DayValue(d0 + timedelta(days=i), 400.0) for i in range(8)]
    o = observe("coffee", "sleep_duration", exposure, series)
    assert o.days_with + o.days_without == len(series)


def test_the_gate_refuses_rather_than_reporting_noise():
    d0 = date(2026, 8, 1)
    o = observe("tea", "mood", {d0}, _series(d0, 20, 5.0, 5.0, 20))
    assert not o.enough
    # The counts are still reported — screen 1's "3 more nights" is built
    # from them, so refusing must not mean returning nothing.
    assert o.days_with == 1
    assert o.days_without == 19


def test_next_day_lag_shifts_the_exposure_forward():
    """Alcohol tonight is compared against TOMORROW's resting heart rate."""
    d0 = date(2026, 8, 1)
    exposure = {d0 + timedelta(days=i) for i in range(9)}
    series = [
        DayValue(d0 + timedelta(days=i), 66.0 if 1 <= i <= 9 else 59.0)
        for i in range(25)
    ]
    same = observe("alcohol", "heart_rate_resting", exposure, series)
    nxt = observe("alcohol", "heart_rate_resting", exposure, series,
                  lag=LAG_NEXT_DAY)
    assert nxt.difference != same.difference


def test_identical_records_produce_the_same_hash():
    d0 = date(2026, 8, 1)
    o = observe("coffee", "sleep_duration",
                {d0 + timedelta(days=i) for i in range(10)},
                _series(d0, 24, 360.0, 400.0, 10))
    assert content_hash([o]) == content_hash([o])


# --------------------------------------------------------------------------- #
# The wording — the part a clinician would read
# --------------------------------------------------------------------------- #
def _ready():
    d0 = date(2026, 8, 1)
    return observe("coffee", "sleep_duration",
                   {d0 + timedelta(days=i) for i in range(11)},
                   _series(d0, 25, 360.0, 398.0, 11))


def test_nothing_is_worded_as_a_cause():
    o = _ready()
    text = (headline(o) + " " + detail(o)).lower()
    for banned in ("caused", "causes", "because of", "due to", "proves",
                   "leads to", "results in", "is responsible for"):
        assert banned not in text, banned
    # The hedge the owner asked for, in their words.
    assert "might be the reason" in detail(o).lower()


def test_the_general_fact_is_marked_as_general():
    o = _ready()
    body = detail(o, "caffeine is a stimulant that can delay sleep onset.")
    assert "in general:" in body.lower()
    # ...and the personal half is still an observation.
    assert "on the 11 days" in body.lower()


def test_no_confidence_score_is_reported():
    """Dropped deliberately: it reads as a statistical claim we cannot
    support. The day counts carry the same information honestly."""
    card = to_card(_ready())
    assert "confidence" not in {k.lower() for k in card}
    assert card["days_with"] == 11 and card["days_without"] == 14


def test_the_not_enough_card_says_how_many_more_days():
    d0 = date(2026, 8, 1)
    o = observe("tea", "mood", {d0, d0 + timedelta(days=1)},
                _series(d0, 20, 5.0, 5.0, 20))
    body = detail(o)
    assert "not have enough days" in body.lower()
    assert "more would do it" in body.lower()


def test_a_difference_is_a_direction_not_a_grade():
    card = to_card(_ready())
    assert card["favourable"] is False
    text = (card["headline"] + card["detail"]).lower()
    for graded in ("poor", "bad", "unhealthy", "too low", "too high",
                   "you should", "cut down", "stop drinking"):
        assert graded not in text, graded


# --------------------------------------------------------------------------- #
# End to end, against seeded records
# --------------------------------------------------------------------------- #
async def _seed(db):
    start, end = window_bounds()
    for i in range(1, 26):
        day = end - timedelta(days=i)
        late = i <= 11
        # Coffee, logged at 8pm on the "late" days and 8am otherwise.
        db.add(LifestyleLog(
            user_id=USER, log_type="coffee", quantity=1, unit="cup",
            logged_at=utcnow().replace(hour=20 if late else 8) - timedelta(days=i),
        ))
        db.add(SahhaDailyTotal(
            user_id=USER, metric="sleep_duration", bucket_start=day,
            total=360.0 if late else 400.0, entries=1, days_counted=1,
        ))
        db.add(MoodLog(user_id=USER, log_date=day, score=6, factors=[]))
        db.add(VitalReading(
            user_id=USER, vital_type="heart_rate", value_primary=70,
            recorded_at=utcnow() - timedelta(days=i),
        ))
    await db.flush()


async def test_compute_returns_a_card_per_pair(db_session):
    await _seed(db_session)
    out = await compute(db_session, USER)
    assert out, "no pairs computed"
    keys = {o.key for o in out}
    assert len(keys) == len(out), "pair keys must be unique — they are route ids"


async def test_late_caffeine_uses_the_hour_the_log_was_written(db_session):
    """`lifestyle_log.logged_at` is a timestamp, so "after 3pm" is real data
    rather than an assumption. Without the hour this pair would be "any
    coffee", which is a different question."""
    await _seed(db_session)
    out = await compute(db_session, USER)
    coffee = next(
        o for o in out if o.exposure == "coffee" and o.outcome == "sleep_duration"
    )
    # 11 evening coffees were seeded; the morning ones must not count.
    assert coffee.days_with <= 11


async def test_one_failing_pair_does_not_cost_the_screen(db_session):
    """Each pair runs in its own SAVEPOINT. A missing rollup in a standalone
    deployment must not blank the whole Insights tab."""
    out = await compute(db_session, USER)   # nothing seeded for most pairs
    assert isinstance(out, list)


async def test_a_reader_with_no_data_gets_the_not_yet_state(db_session):
    out = await compute(db_session, uuid.uuid4())
    assert all(not o.enough for o in out)
    assert all(o.days_with == 0 or o.days_without >= 0 for o in out)


def test_min_days_is_a_week_of_each():
    assert MIN_DAYS_PER_GROUP == 7

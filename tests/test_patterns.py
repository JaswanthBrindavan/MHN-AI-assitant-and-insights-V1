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
from datetime import UTC, date, datetime, time, timedelta

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
    assert "cannot answer it yet" in body.lower()
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


# --------------------------------------------------------------------------- #
# Reads never compute — the nightly sweep writes, the route reads
# --------------------------------------------------------------------------- #
async def test_recompute_writes_one_artifact_per_pair(db_session):
    from app.patterns.engine import active_patterns, recompute_patterns

    await _seed(db_session)
    written = await recompute_patterns(db_session, USER, reason="test")
    assert written > 0
    rows = await active_patterns(db_session, USER)
    assert len(rows) == written
    assert all(r.card for r in rows), "the card is rendered at write time"


async def test_an_unchanged_pattern_writes_nothing_the_second_night(db_session):
    """`content_hash` covers the FINDING, not the moment it was taken.

    This is what makes day-wise history affordable: a row appears the day a
    pattern moves, not seven rows a night per reader.
    """
    from app.patterns.engine import active_patterns, recompute_patterns

    await _seed(db_session)
    first = await recompute_patterns(db_session, USER, reason="night-1")
    second = await recompute_patterns(db_session, USER, reason="night-2")
    assert first > 0
    assert second == 0, "identical records must not produce new rows"
    assert len(await active_patterns(db_session, USER)) == first


async def test_a_changed_pattern_supersedes_rather_than_duplicates(db_session):
    from sqlalchemy import select

    from app.models.rules import PatternArtifact
    from app.patterns.engine import active_patterns, recompute_patterns

    await _seed(db_session)
    await recompute_patterns(db_session, USER, reason="night-1")
    before = {r.pattern_key: r.id for r in await active_patterns(db_session, USER)}

    # A new night's sleep changes one finding.
    start, end = window_bounds()
    db_session.add(SahhaDailyTotal(
        user_id=USER, metric="sleep_duration", bucket_start=end - timedelta(days=26),
        total=120.0, entries=1, days_counted=1,
    ))
    await db_session.flush()
    await recompute_patterns(db_session, USER, reason="night-2")

    after = await active_patterns(db_session, USER)
    # Still one ACTIVE row per pair — the old one was superseded, not stacked.
    assert len({r.pattern_key for r in after}) == len(after)
    superseded = (
        await db_session.execute(
            select(PatternArtifact).where(PatternArtifact.status == "superseded")
        )
    ).scalars().all()
    assert superseded, "the previous finding should be superseded, not deleted"
    assert all(s.superseded_by is not None for s in superseded)
    assert before  # the first night really did write


async def test_the_read_path_serves_stored_rows(db_session):
    """If this ever starts computing, the invariant is broken again."""
    from app.patterns.engine import active_patterns, recompute_patterns

    await _seed(db_session)
    await recompute_patterns(db_session, USER, reason="test")
    rows = await active_patterns(db_session, USER)
    assert rows and all(r.card is not None for r in rows)


async def test_the_general_fact_is_stored_with_the_card(db_session):
    from app.patterns.engine import active_patterns, recompute_patterns

    await _seed(db_session)
    from app.models.chat import McpChunk

    # A reviewed profile sentence for the coffee/sleep pair.
    db_session.add(McpChunk(
        condition_code="MC287", chunk_type="lifestyle_triggers",
        content=(
            "Caffeine is a stimulant that blocks adenosine receptors and can "
            "delay sleep onset and shorten total sleep time when taken late "
            "in the day."
        ),
    ))
    await db_session.flush()
    await recompute_patterns(db_session, USER, reason="test")
    cards = [r.card or {} for r in await active_patterns(db_session, USER)]
    coffee = [c for c in cards if str(c.get("key", "")).startswith("coffee__sleep")]
    assert coffee
    card = coffee[0]
    if card.get("enough_data"):
        assert "in general:" in str(card.get("detail", "")).lower()


# --------------------------------------------------------------------------- #
# Screen 5 — a reading that moved against the reader's OWN baseline
# --------------------------------------------------------------------------- #
def _run(base: float, recent: float, n: int = 20):
    d0 = date(2026, 8, 1)
    return (
        [DayValue(d0 + timedelta(days=i), base) for i in range(n)]
        + [DayValue(d0 + timedelta(days=n + i), recent) for i in range(3)]
    )


def test_a_sustained_move_against_your_own_baseline_is_surfaced():
    from app.patterns.baseline import detect, to_card

    d = detect("heart_rate_resting", _run(62.0, 71.0))
    assert d is not None
    card = to_card(d)
    # Both figures stated. This is arithmetic on their own record, not a band.
    assert "71 bpm" in card["headline"] and "62 bpm" in card["headline"]
    assert "your usual" in card["headline"]
    assert "isn't a diagnosis" in card["note"]


def test_it_never_claims_a_norm():
    """The distinction that makes this card allowed: everywhere else a
    wearable number is never graded, because there are no reference ranges for
    sleep or HRV. Comparing someone to THEMSELVES is a different statement."""
    from app.patterns.baseline import detect, to_card

    found = detect("heart_rate_resting", _run(62.0, 71.0))
    assert found is not None
    card = to_card(found)
    text = (card["headline"] + " " + card["note"]).lower()
    for claim in ("normal range", "typical range", "too high", "too low",
                  "abnormal", "elevated for your age", "unhealthy"):
        assert claim not in text, claim


def test_only_the_direction_worth_a_look_fires():
    from app.patterns.baseline import detect

    # A resting heart rate FALLING, or an HRV RISING, is not a concern.
    assert detect("heart_rate_resting", _run(62.0, 54.0)) is None
    assert detect("heart_rate_variability_sdnn", _run(40.0, 52.0)) is None
    # ...but the other way round is.
    assert detect("heart_rate_variability_sdnn", _run(52.0, 40.0)) is not None


def test_a_single_odd_day_does_not_fire():
    """A run matters; one bad night does not. RUN_DAYS is why."""
    from app.patterns.baseline import detect

    d0 = date(2026, 8, 1)
    series = [DayValue(d0 + timedelta(days=i), 62.0) for i in range(22)]
    series.append(DayValue(d0 + timedelta(days=22), 80.0))
    assert detect("heart_rate_resting", series) is None


def test_the_baseline_excludes_the_run_that_is_being_judged():
    """Comparing a run against a mean it is part of drags the mean toward the
    run and hides the very change the card exists to notice."""
    from app.patterns.baseline import detect

    d = detect("heart_rate_resting", _run(62.0, 71.0))
    assert d is not None
    assert d.baseline_mean == 62.0        # not pulled up by the recent days


def test_too_little_history_is_not_a_baseline():
    from app.patterns.baseline import detect

    assert detect("heart_rate_resting", _run(62.0, 71.0, n=5)) is None


async def test_attention_runs_over_the_watched_metrics(db_session):
    from app.patterns.service import attention

    items = await attention(db_session, uuid.uuid4())
    assert isinstance(items, list)        # no data: no cards, no crash


# --------------------------------------------------------------------------- #
# Screen 2 — "This week"
# --------------------------------------------------------------------------- #
def test_the_trend_is_a_direction_not_a_verdict():
    """The design's caption reads "Recovery trending up - Great job! Your body
    is recovering well". The first half is a fact and is kept; the second is a
    grade on a wearable reading, which this product does not do."""
    from app.patterns.service import trend

    d0 = date(2026, 8, 1)
    rising = [DayValue(d0 + timedelta(days=i), 360.0) for i in range(7)] + [
        DayValue(d0 + timedelta(days=7 + i), 420.0) for i in range(7)
    ]
    t = trend(rising)
    assert t and t["direction"] == "up"
    assert set(t) == {"direction", "this_week_mean", "last_week_mean",
                      "days_this_week"}
    # No adjective anywhere in it.
    assert all(not isinstance(v, str) or v in ("up", "down", "steady")
               for v in t.values())


def test_a_flat_fortnight_is_steady_not_an_achievement():
    from app.patterns.service import trend

    d0 = date(2026, 8, 1)
    flat = [DayValue(d0 + timedelta(days=i), 400.0) for i in range(14)]
    t = trend(flat)
    assert t is not None and t["direction"] == "steady"


def test_a_short_history_has_no_trend():
    from app.patterns.service import trend

    d0 = date(2026, 8, 1)
    assert trend([DayValue(d0 + timedelta(days=i), 400.0) for i in range(4)]) is None


# --------------------------------------------------------------------------- #
# Screen 2 serves the charts the yesterday card asks for
#
# The card names the drivers its conclusion rested on and the client fetches
# each one from `/patterns/summary`. While that route knew only the four
# wearable series, a day explained by caffeine or hydration named those drivers
# and then 400'd every request for them — the reader was told why yesterday was
# like that and shown a chart of none of it.
# --------------------------------------------------------------------------- #
CHART_USER = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CHART_HDR = {"X-User-Id": str(CHART_USER)}


def test_the_weekly_chart_serves_every_driver_the_card_can_name():
    """The two lists are written by hand in different sections of one module,
    so nothing but this stops a tenth driver being added with no chart behind
    it — which is the exact shape of the bug this section exists for."""
    from app.patterns.service import (
        TREND_METRICS,
        YESTERDAY_HABITS,
        YESTERDAY_METRICS,
    )

    nameable = set(YESTERDAY_METRICS) | set(YESTERDAY_HABITS)
    assert nameable <= set(TREND_METRICS)


async def _seed_drivers(db):
    """One wearable, one vital and one manual tracker over the same days."""
    from app.models.coredata import LifestyleDailyTotal
    from app.patterns.service import tracking_today

    end = tracking_today()
    for i in range(1, 11):
        day = end - timedelta(days=i)
        db.add(SahhaDailyTotal(
            user_id=CHART_USER, metric="steps", bucket_start=day,
            total=8000.0, entries=1, days_counted=1,
        ))
        db.add(VitalReading(
            user_id=CHART_USER, vital_type="spo2", value_primary=97,
            recorded_at=datetime.combine(day, time(9, 0), tzinfo=UTC),
        ))
        # Two cups a day. Stored in cups, and that is what a chart must show.
        db.add(LifestyleDailyTotal(
            user_id=CHART_USER, metric="coffee", bucket_start=day,
            total=2, entries=2, days_counted=1,
        ))
    await db.commit()


async def _this_week(client, metric: str) -> dict:
    resp = await client.get(
        f"/api/v1/patterns/summary?metric={metric}", headers=CHART_HDR
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["this_week"]


async def test_a_wearable_driver_charts_through_the_route_the_client_uses(
    client, db_session,
):
    await _seed_drivers(db_session)
    week = await _this_week(client, "steps")
    assert week["metric"] == "steps"
    assert len(week["series"]) == 7
    assert {p["value"] for p in week["series"]} == {8000.0}


async def test_a_vital_driver_charts_in_the_unit_it_is_recorded_in(
    client, db_session,
):
    await _seed_drivers(db_session)
    week = await _this_week(client, "spo2")
    assert week["unit"] == "%"
    assert {p["value"] for p in week["series"]} == {97.0}


async def test_a_manual_tracker_charts_in_cups_rather_than_millilitres(
    client, db_session,
):
    """Caffeine is the driver the feature was asked for, and the one whose unit
    is easiest to lose: its sibling trackers are stored in ml, so a chart that
    took the family's unit rather than the metric's would read 2 ml of coffee.
    """
    await _seed_drivers(db_session)
    week = await _this_week(client, "coffee")
    assert week["unit"] == "cup"
    assert week["label"] == "caffeine"
    assert len(week["series"]) == 7
    assert {p["value"] for p in week["series"]} == {2.0}


async def test_a_driver_with_no_readings_answers_empty_rather_than_failing(
    client, db_session,
):
    """A reader who logs coffee but not water still gets a hydration chart
    asked for, because the card names drivers per day. Refusing it would put an
    error on a screen whose other charts drew fine."""
    await _seed_drivers(db_session)
    week = await _this_week(client, "water")
    assert week["series"] == []
    assert week["trend"] is None


async def test_a_metric_nobody_records_is_still_refused(client):
    resp = await client.get(
        "/api/v1/patterns/summary?metric=vibes", headers=CHART_HDR
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# While the personal data is still building, show the general fact
# --------------------------------------------------------------------------- #
def _waiting():
    d0 = date(2026, 8, 1)
    return observe("coffee", "sleep_duration", {d0, d0 + timedelta(days=1)},
                   [DayValue(d0 + timedelta(days=i), 400.0) for i in range(20)])


FACT = (
    "Caffeine is a stimulant that blocks adenosine receptors and can delay "
    "sleep onset when taken late in the day."
)


def test_a_waiting_card_leads_with_the_general_fact():
    """"No patterns yet" is a dead screen. The reviewed corpus already has
    something worth reading, and it is true whether or not this reader has
    logged anything — so the card opens with it rather than with an apology."""
    card = to_card(_waiting(), title="Late caffeine and your sleep", fact=FACT)
    assert card["enough_data"] is False
    assert card["headline"].startswith("In general:")
    assert "adenosine" in card["headline"]
    assert card["detail"].startswith("In general:")


def test_a_waiting_card_never_implies_the_fact_is_about_this_reader():
    """The whole reason the general half is allowed to be causal is that it is
    marked as general. Losing that prefix would turn reviewed guidance into a
    finding about someone who has logged two days."""
    card = to_card(_waiting(), fact=FACT)
    assert "in general:" in card["headline"].lower()
    assert "cannot answer it yet" in card["detail"].lower()
    # It must not claim anything about their own nights.
    for claim in ("your sleep was", "your recorded sleep averaged",
                  "on the 2 days you"):
        assert claim not in card["detail"].lower()


def test_a_waiting_card_still_says_how_many_days_are_left():
    card = to_card(_waiting(), fact=FACT)
    assert "more would do it" in card["detail"]
    assert card["days_with"] == 2


def test_without_a_fact_the_waiting_card_is_still_honest():
    """No corpus sentence for a pair is an ordinary outcome, not an error."""
    card = to_card(_waiting())
    assert "not enough days" in card["headline"].lower()
    assert "cannot answer it yet" in card["detail"].lower()


# --------------------------------------------------------------------------- #
# The corpus stores STRUCTURED RECORDS, not prose. Splitting them on ". " alone
# found no boundary, so whole records reached the reader as "the general fact":
# "In general: LHP: Alcohol; Influence Level: Medium; Influence Study: ..."
# These are the exact strings that shipped that way.
# --------------------------------------------------------------------------- #
def test_a_field_record_yields_its_sentence_and_not_its_labels():
    from app.patterns.facts import _first_sentence

    found = _first_sentence(
        "LHP: Alcohol; Influence Level: Medium; Influence Study: Regular or "
        "heavy alcohol adds empty calories, promotes central adiposity and "
        "raises blood pressure, worsening the insulin-resistant, dysglycaemic "
        "state.",
        ("alcohol", "blood pressure"),
    )
    assert found is not None
    assert found.startswith("Regular or heavy alcohol adds empty calories")
    # The labels name the field, not the finding, and read as a database row.
    for label in ("LHP:", "Influence Level:", "Influence Study:"):
        assert label not in found


def test_list_decoration_is_stripped_from_the_front_of_a_segment():
    from app.patterns.facts import _first_sentence

    found = _first_sentence(
        "/ •  Limit screen time and caffeine before bed to improve sleep "
        "quality.; Profile: •  Adults with metabolic syndrome / •  "
        "Obese individuals; Importance: Medium.",
        ("caffeine", "sleep"),
    )
    assert found == "Limit screen time and caffeine before bed to improve sleep quality."
    # The profile list and the importance grade are not the fact.
    assert "Profile" not in found and "Importance" not in found


def test_no_sentence_is_better_than_one_about_something_else():
    """This record is about impaired glucose utilisation in diabetes. It matched
    a hydration/mood pair only because "dehydration" contains "hydration", and
    it used to be stapled onto that card whole. A missing fact costs a line; a
    wrong one costs the reader's trust."""
    from app.patterns.facts import _first_sentence

    assert _first_sentence(
        "Symptom: Fatigue and weakness; Type: Common; Note: Impaired cellular "
        "glucose utilisation and dehydration cause tiredness and reduced "
        "stamina.",
        ("hydration", "fatigue"),
    ) is None


def test_ordinary_prose_still_reads_as_one_sentence():
    """The split must not damage a corpus chunk that IS written as prose."""
    from app.patterns.facts import _first_sentence

    found = _first_sentence(
        "Caffeine is a stimulant that blocks adenosine receptors and can delay "
        "sleep onset when taken late in the day. Other factors also matter.",
        ("caffeine", "sleep"),
    )
    assert found == (
        "Caffeine is a stimulant that blocks adenosine receptors and can delay "
        "sleep onset when taken late in the day."
    )

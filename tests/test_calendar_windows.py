"""Calendar time windows: yesterday, this week (to date), last week.

Three things these pin down, all of which ship a plausible wrong NUMBER rather
than an error:

* "yesterday" must not be answered with a week. Before this it parsed to the
  rolling week — or, for "water intake yesterday", did not parse at all and
  went to the model.
* "this week" is the CALENDAR week to date, so it is normally PARTIAL, and a
  three-day week reported as a week total is wrong. ``days_counted`` says so.
* "last week" is the PREVIOUS complete calendar week — the one thing the old
  rolling window could never be.

Dates are RELATIVE throughout. A fixed date here is a test that passes until
the calendar moves past it.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.chat.abilities import TRACKER_PERIODS, parse_tracker_query
from app.chat.data_handlers import handle_tracker_query
from app.chat.orchestrator import handle_chat
from app.chat.tools.definitions import GET_TRACKER_TOTAL
from app.coredata.service import calendar_window, week_start
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall, ToolResultMessage
from app.models.common import utcnow
from app.models.coredata import LifestyleDailyTotal, SahhaDailyTotal, SahhaWeeklyTotal

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

TODAY = utcnow().date()
YESTERDAY = TODAY - timedelta(days=1)
THIS_WEEK = week_start(TODAY)
LAST_WEEK = THIS_WEEK - timedelta(days=7)


def _daily(db, metric: str, day, total: float) -> None:
    db.add(
        LifestyleDailyTotal(
            user_id=USER, metric=metric, bucket_start=day,
            total=total, entries=1, days_counted=1,
        )
    )


def _sahha_day(db, metric: str, day, total: float, entries: int = 1) -> None:
    db.add(
        SahhaDailyTotal(
            user_id=USER, metric=metric, bucket_start=day,
            total=total, entries=entries, days_counted=1,
        )
    )


def _sahha_week(db, metric: str, start, total: float, entries: int, days: int) -> None:
    db.add(
        SahhaWeeklyTotal(
            user_id=USER, metric=metric, bucket_start=start,
            total=total, entries=entries, days_counted=days,
        )
    )


# --------------------------------------------------------------------------- #
# The window itself
# --------------------------------------------------------------------------- #
def test_week_start_opens_on_sunday():
    """mhn-spring's convention, not PostgreSQL's Monday: TrackingGrain uses
    previousOrSame(SUNDAY) and SahhaRollupDao spells it the same way in SQL."""
    assert week_start(TODAY).weekday() == 6          # Python: Sunday == 6
    assert (TODAY - week_start(TODAY)).days < 7


def test_this_week_is_to_date_and_last_week_is_the_one_before():
    # today included, so the week is normally PARTIAL
    assert calendar_window("this_week") == (THIS_WEEK, TODAY + timedelta(days=1))
    # complete, and disjoint from this week
    assert calendar_window("last_week") == (LAST_WEEK, THIS_WEEK)

    assert calendar_window("yesterday") == (YESTERDAY, TODAY)
    assert calendar_window("week") is None           # rolling, not calendar


# --------------------------------------------------------------------------- #
# One vocabulary, two engines
# --------------------------------------------------------------------------- #
def test_the_tool_enum_and_the_parser_share_one_period_vocabulary():
    enum = GET_TRACKER_TOTAL.input_schema["properties"]["period"]["enum"]
    assert set(enum) == set(TRACKER_PERIODS)
    assert {"yesterday", "this_week", "last_week"} <= set(enum)


@pytest.mark.parametrize(
    ("message", "period"),
    [
        ("how much water yesterday", "yesterday"),
        ("water intake yesterday", "yesterday"),       # no other framing cue
        ("how much water this week", "this_week"),
        ("how much water last week", "last_week"),
        ("how many steps yesterday", "yesterday"),
        ("my steps last week", "last_week"),
        ("how much water", "week"),                    # unchanged: rolling
        ("how much water this month", "month"),
    ],
)
def test_the_legacy_parser_produces_the_tool_period_values(message, period):
    query = parse_tracker_query(message)
    assert query is not None, message
    assert query.period == period
    assert query.period in TRACKER_PERIODS


# --------------------------------------------------------------------------- #
# Lifestyle — one per new period value
# --------------------------------------------------------------------------- #
async def test_lifestyle_yesterday_reads_only_yesterday(db_session):
    _daily(db_session, "water", YESTERDAY, 1500)
    _daily(db_session, "water", TODAY, 400)
    _daily(db_session, "water", LAST_WEEK, 9000)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much water yesterday")

    assert out is not None
    assert "1500 ml of water yesterday" in out["reply"]
    assert out["provenance"]["period"] == "yesterday"


async def test_lifestyle_this_week_is_the_calendar_week_to_date(db_session):
    _daily(db_session, "water", THIS_WEEK, 1000)
    _daily(db_session, "water", LAST_WEEK, 9000)     # must not be counted
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much water this week")

    assert out is not None
    assert "1000 ml of water so far this week" in out["reply"]
    assert "9000" not in out["reply"] and "10000" not in out["reply"]


async def test_lifestyle_last_week_is_the_previous_complete_week(db_session):
    _daily(db_session, "water", LAST_WEEK, 2000)
    _daily(db_session, "water", LAST_WEEK + timedelta(days=1), 1000)
    _daily(db_session, "water", THIS_WEEK, 500)      # this week, not last
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much water last week")

    assert out is not None
    assert "3000 ml of water last week" in out["reply"]
    assert "3500" not in out["reply"]


async def test_a_partial_lifestyle_week_says_how_many_days_it_covers(db_session):
    _daily(db_session, "water", THIS_WEEK, 2000)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much water this week")

    assert out is not None
    assert "covers 1 day" in out["reply"]
    assert "the week is not over" in out["reply"]


async def test_nothing_in_the_daily_totals_is_not_a_claim_that_nothing_was_logged(
    db_session,
):
    """The daily totals are compiled overnight, so a row written here today is
    genuinely absent from them. "You logged no water" would be a wrong answer."""
    out = await handle_tracker_query(db_session, USER, "how much water this week")

    assert out is not None
    assert "have not logged" not in out["reply"]
    assert "compiled overnight" in out["reply"]


# --------------------------------------------------------------------------- #
# Wearable — one per new period value
# --------------------------------------------------------------------------- #
async def test_wearable_yesterday_reads_the_daily_bucket_not_the_week(db_session):
    _sahha_day(db_session, "steps", YESTERDAY, 6000)
    _sahha_week(db_session, "steps", THIS_WEEK, 70000, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps yesterday")

    assert out is not None
    assert "6,000 steps yesterday" in out["reply"]
    assert "70,000" not in out["reply"]
    assert "week of" not in out["reply"]
    # A one-day figure must not carry a chart of the seven days that FOLLOW it.
    assert "visual" not in out


async def test_wearable_this_week_reads_the_current_weekly_bucket(db_session):
    _sahha_week(db_session, "steps", THIS_WEEK, 21000, 3, 3)
    _sahha_week(db_session, "steps", LAST_WEEK, 70000, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")

    assert out is not None
    assert "21,000 steps so far this week" in out["reply"]
    assert "70,000" not in out["reply"]


async def test_wearable_last_week_reads_the_previous_weekly_bucket(db_session):
    _sahha_week(db_session, "steps", THIS_WEEK, 21000, 3, 3)
    _sahha_week(db_session, "steps", LAST_WEEK, 70000, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps last week")

    assert out is not None
    assert "70,000 steps last week" in out["reply"]
    assert LAST_WEEK.strftime("%d %b %Y") in out["reply"]
    assert "21,000" not in out["reply"]


async def test_a_partial_wearable_week_is_labelled_partial(db_session):
    """3 days of a week reported as a week total is a wrong number. `entries`
    is READINGS and cannot say this; `days_counted` is the rollup's own answer."""
    _sahha_week(db_session, "steps", THIS_WEEK, 21000, 9, 3)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")

    assert out is not None
    assert "covers 3 of 7 days" in out["reply"]
    assert "part-week" in out["reply"]
    assert "9 readings" in out["reply"]      # readings and days stay distinct


async def test_a_complete_week_is_not_labelled_partial(db_session):
    _sahha_week(db_session, "steps", LAST_WEEK, 70000, 21, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps last week")

    assert out is not None
    assert "part-week" not in out["reply"]


async def test_yesterday_does_not_silently_answer_with_a_week(db_session):
    """The whole point. A weekly bucket exists and a daily one does not, so the
    only wrong answer available is the week's total under a "yesterday" ask.
    (Steps then fall through to the manual log, which has nothing either.)"""
    _sahha_week(db_session, "steps", THIS_WEEK, 70000, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps yesterday")

    assert out is not None
    assert "70,000" not in out["reply"]
    assert "no steps entries yesterday" in out["reply"]


# --------------------------------------------------------------------------- #
# Both engines
# --------------------------------------------------------------------------- #
class _CallsTrackerTool(FakeProvider):
    """Calls get_tracker_total and answers with the tool's own wording, so the
    equality breaks the moment the engines read different windows."""

    def __init__(self, args: dict) -> None:
        super().__init__()
        self.args = args

    async def generate_turn(self, *, system, messages, tools):
        for message in messages:
            if isinstance(message, ToolResultMessage):
                payload = json.loads(message.results[0].content)
                return LLMTurn(
                    text=payload["deterministic_reply"], stop_reason="end_turn"
                )
        return LLMTurn(
            tool_calls=(ToolCall("t1", "get_tracker_total", self.args),),
            stop_reason="tool_use",
        )


@pytest.mark.parametrize(
    ("question", "period"),
    [
        ("how many steps did I do yesterday", "yesterday"),
        ("how many steps this week", "this_week"),
        ("how many steps last week", "last_week"),
    ],
)
async def test_both_engines_answer_a_calendar_window_identically(
    db_session, monkeypatch, question, period
):
    from app.config import get_settings

    _sahha_day(db_session, "steps", YESTERDAY, 6000)
    _sahha_week(db_session, "steps", THIS_WEEK, 21000, 9, 3)
    _sahha_week(db_session, "steps", LAST_WEEK, 70000, 21, 7)
    await db_session.flush()

    parsed = parse_tracker_query(question)
    assert parsed is not None and parsed.period == period

    monkeypatch.setattr(get_settings(), "chat_engine", "legacy")
    legacy = await handle_chat(
        db_session, USER, question, FakeProvider(), uuid.uuid4()
    )

    monkeypatch.setattr(get_settings(), "chat_engine", "agentic")
    agentic = await handle_chat(
        db_session, USER, question,
        _CallsTrackerTool({"metric": "steps", "period": period}),
        uuid.uuid4(),
    )

    assert legacy.response_message == agentic.response_message


# --------------------------------------------------------------------------- #
# "last night", "today", and the day before yesterday
#
# Three phrasings that all became the ROLLING WEEK, silently. The window
# vocabulary was the only thing missing: `_day_offset` already reads "last
# night" as yesterday for WRITES, so "drank 2 glasses of water last night"
# logged to yesterday while "how much water last night" read seven days.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "period"),
    [
        ("how did I sleep last night", "yesterday"),
        ("how much sleep did I get last night", "yesterday"),
        ("my sleep last night", "yesterday"),
        ("how many steps last night", "yesterday"),
        ("how much water today", "today"),
        ("how much water so far today", "today"),
        ("water intake today", "today"),
        # Not confidently answered with the wrong day: `\byesterday\b` matched
        # inside the longer phrase and the ladder returned on the first hit.
        ("how much water the day before yesterday", "week"),
    ],
)
def test_a_natural_day_phrase_is_not_silently_a_rolling_week(message, period):
    query = parse_tracker_query(message)
    assert query is not None, message
    assert query.period == period, message
    assert query.period in TRACKER_PERIODS


async def test_today_reads_the_log_not_the_overnight_rollup(db_session):
    """The daily totals are Spring's and compile overnight, so today's bucket
    is empty or partial all day. "How much water today" is the one window
    where the rollup is the wrong table."""
    from app.coredata.service import add_lifestyle_log

    await add_lifestyle_log(db_session, USER, "water", 2, "glass")
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much water today")

    assert out is not None
    assert out["provenance"]["period"] == "today"
    assert "500 ml of water today" in out["reply"]
    assert "past 7 days" not in out["reply"]


async def test_the_overnight_excuse_is_only_for_a_window_holding_today(db_session):
    """It was written for today's lag and applied to CLOSED windows, so a
    reader who genuinely drank nothing last week was told their data might
    still be pending."""
    for message in ("how much water yesterday", "how much water last week"):
        out = await handle_tracker_query(db_session, USER, message)
        assert out is not None, message
        assert "compiled overnight" not in out["reply"], message
        assert "have not logged any water" in out["reply"], message

    # ...and the window that DOES still hold today keeps the honest caveat.
    out = await handle_tracker_query(db_session, USER, "how much water this week")
    assert out is not None and "compiled overnight" in out["reply"]


async def test_a_lapsed_device_is_reported_as_lapsed_not_absent(db_session):
    """`_fresh_buckets` drops the stale bucket correctly; nothing downstream
    told "dropped as stale" from "never existed", so a device that stopped
    syncing three weeks ago produced "there is nothing here until one is
    linked" — both clauses false, and the one wording rule 1 forbids."""
    for weeks in range(3, 30):
        _sahha_week(
            db_session, "heart_rate_variability_sdnn",
            THIS_WEEK - timedelta(weeks=weeks), 45.0, 7, 7,
        )
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "what is my hrv this week")

    assert out is not None
    assert "nothing here until one is linked" not in out["reply"]
    assert "I don't have any" not in out["reply"]
    # The DATE and the FIGURE: an empty window still names the last reading
    # there was, so the reader does not have to ask a second question.
    assert "most recent is 6.43 ms from the week of" in out["reply"]


async def test_a_lapsed_device_also_rescues_the_manual_fallthrough(db_session):
    """steps and sleep fall through to the manual log when the device has
    nothing in the window; with no manual rows either, that branch said "you
    have no steps entries", which is the same false absence."""
    for weeks in range(3, 30):
        _sahha_week(db_session, "steps", THIS_WEEK - timedelta(weeks=weeks),
                    52300.0, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how many steps did i take this week"
    )

    assert out is not None
    assert "no steps entries" not in out["reply"]
    assert "most recent is 52,300 steps from the week of" in out["reply"]


async def test_a_daily_gap_beside_a_live_weekly_bucket_is_not_a_lapsed_device(
    db_session,
):
    """The stale note must use the SAME grain the ask used, or a "yesterday"
    question with no daily bucket would accuse a syncing device of stopping."""
    _sahha_week(db_session, "steps", THIS_WEEK, 70000, 7, 7)
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps yesterday")

    assert out is not None
    assert "has not synced" not in out["reply"]


# --------------------------------------------------------------------------- #
# A manual tracker gets the same daily chart the wearable does. It had none:
# `handle_tracker_query` set `visual` only on its wearable branch, so "how did
# I sleep this week" drew a graph and "how much water this week" drew nothing.
# --------------------------------------------------------------------------- #
async def test_a_manual_tracker_week_carries_its_daily_chart(db_session):
    for offset, ml in ((0, 1200.0), (1, 900.0), (2, 1500.0)):
        _daily(db_session, "water", THIS_WEEK + timedelta(days=offset), ml)
    # A day in the NEXT week must not appear on this week's chart.
    _daily(db_session, "water", THIS_WEEK + timedelta(days=7), 9999.0)
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much water did i drink this week"
    )

    assert out is not None
    visual = out["visual"]
    assert visual["source"] == "lifestyle"
    # The client routes on this: without it the chart cannot open the tracker.
    assert visual["metric"] == "water"
    assert visual["labels"] == [
        (THIS_WEEK + timedelta(days=i)).strftime("%d %b") for i in range(7)
    ]
    # An unlogged day is None, never a logged zero.
    assert visual["values"] == [1200.0, 900.0, 1500.0, None, None, None, None]
    assert 9999.0 not in [v for v in visual["values"] if v is not None]
    assert visual["title"] == THIS_WEEK.strftime("water — week of %d %b %Y")


async def test_the_manual_bars_add_up_to_the_sentence_above_them(db_session):
    """Both read `lifestyle_daily_total`, so a reader who adds the bars up
    gets the figure the reply printed. Reading either side from `lifestyle_log`
    would put a late-evening glass on a different day and break that."""
    for offset in range(3):
        _daily(db_session, "water", THIS_WEEK + timedelta(days=offset), 500.0)
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much water did i drink this week"
    )

    assert out is not None
    assert sum(v for v in out["visual"]["values"] if v is not None) == 1500.0
    assert "1,500 ml" in out["reply"] or "1500 ml" in out["reply"]


async def test_one_logged_day_is_not_charted(db_session):
    """One bar is not a trend, and chart_payload pads a flat single-bar series
    by +/-1, which renders as data the reader never logged."""
    _daily(db_session, "water", THIS_WEEK, 1200.0)
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much water did i drink this week"
    )

    assert out is not None
    assert "visual" not in out
    # The answer itself still stands; only the chart is withheld.
    assert "1,200 ml" in out["reply"] or "1200 ml" in out["reply"]


async def test_a_single_day_ask_gets_no_week_chart(db_session):
    """`yesterday` is one day. A chart titled as a week would not be the days
    the number above it came from."""
    _daily(db_session, "water", TODAY - timedelta(days=1), 1200.0)
    _daily(db_session, "water", TODAY - timedelta(days=2), 800.0)
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much water did i drink yesterday"
    )

    assert out is not None
    assert "visual" not in out

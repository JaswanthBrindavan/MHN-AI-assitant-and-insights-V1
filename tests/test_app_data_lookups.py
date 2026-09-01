"""Family-connect / doctor-consult abilities, routing precedence, MMR rerank,
and the no-model-name trace guarantee."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from app.chat.abilities import (
    parse_doctor_consult_query,
    parse_family_list_query,
)
from app.chat.data_handlers import (
    handle_doctor_consult_query,
    handle_family_list_query,
    handle_tracker_query,
)
from app.chat.orchestrator import handle_chat
from app.chat.validation import validate_reply
from app.coredata.service import format_wearable, wearable_totals
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall, ToolResultMessage
from app.models.common import utcnow
from app.models.core import User
from app.models.coredata import (
    Doctor,
    DoctorConnect,
    DoctorSpecialization,
    FamilyConnect,
    ManualTracking,
    Relation,
    SahhaDailyTotal,
    SahhaWeeklyTotal,
    VitalReading,
)
from app.rag.ranking import mmr_rerank

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _user(uid: uuid.UUID, name: str) -> User:
    return User(
        id=uid, name=name, email=f"{name.lower().replace(' ', '')}@example.com",
        user_name=name.split()[0][:20], health_card_number=f"HC-{name[:6]}",
        hashcode="x",
    )
DAD = uuid.UUID("77777777-7777-7777-7777-777777777777")
DOC = uuid.UUID("88888888-8888-8888-8888-888888888888")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "Who all are there in my family connect?",
        "who is in my family connect",
        "List my family members",
        "my family connections",
        "Who am I connected with?",
    ],
)
def test_family_list_parses(message):
    assert parse_family_list_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "What does my family history say about heart disease?",
        "my family risk for diabetes",
        "tell me about diabetes",
    ],
)
def test_family_list_does_not_hijack(message):
    assert not parse_family_list_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "Whom did I last consult?",
        "which doctor did I last see",
        "Who is my doctor?",
        "my recent consultations",
        "my doctor connections",
    ],
)
def test_doctor_consult_parses(message):
    assert parse_doctor_consult_query(message)


def test_doctor_consult_does_not_hijack():
    assert not parse_doctor_consult_query("should I consult a doctor for this?")
    assert not parse_doctor_consult_query("what doctor treats diabetes")


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def _seed_family(db):
    db.add(_user(USER, "Asha"))
    db.add(_user(DAD, "Ramesh"))
    db.add(Relation(id=1, name="father", inverse="son"))
    db.add(
        FamilyConnect(
            requester_id=USER, acceptor_id=DAD, accepted=True,
            relation_id=1, req_file_share=True, acc_file_share=True,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_family_list_handler_lists_members(db_session):
    await _seed_family(db_session)
    out = await handle_family_list_query(
        db_session, USER, "who all are in my family connect?"
    )
    assert out is not None
    assert out["provenance"]["path"] == "family_connections"
    assert "Ramesh" in out["reply"]
    assert "your father" in out["reply"]
    assert validate_reply(out["reply"], "none").ok


@pytest.mark.asyncio
async def test_family_list_handler_empty(db_session):
    out = await handle_family_list_query(db_session, USER, "list my family members")
    assert out is not None
    assert "don't have any family connections" in out["reply"]


@pytest.mark.asyncio
async def test_doctor_consult_handler(db_session):
    db_session.add(_user(DOC, "Dr Meera Nair"))
    db_session.add(DoctorSpecialization(id=1, name="Cardiology"))
    db_session.add(Doctor(id=5, user_id=DOC, verified=True, specialization_id=1))
    db_session.add(
        DoctorConnect(
            id=9, user_id=USER, doctor_id=5,
            doctor_acceptance=True, user_acceptance=True,
        )
    )
    await db_session.flush()
    out = await handle_doctor_consult_query(
        db_session, USER, "whom did I last consult?"
    )
    assert out is not None
    assert out["provenance"]["path"] == "doctor_consults"
    assert "Dr Meera Nair" in out["reply"]
    assert "Cardiology" in out["reply"]
    assert validate_reply(out["reply"], "none").ok


@pytest.mark.asyncio
async def test_doctor_consult_handler_empty(db_session):
    out = await handle_doctor_consult_query(db_session, USER, "who is my doctor?")
    assert out is not None
    assert "couldn't find any doctor consultations" in out["reply"]


# --------------------------------------------------------------------------- #
# Routing precedence: a precise metric parse beats the generic data path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_show_me_my_bp_hits_metric_not_insights(db_session):
    result = await handle_chat(
        db_session, USER, "Show me my last BP reading.", FakeProvider()
    )
    assert result.provenance["path"] == "metric_query"


@pytest.mark.asyncio
async def test_generic_data_query_still_served(db_session):
    result = await handle_chat(
        db_session, USER, "show me my insights", FakeProvider()
    )
    assert result.provenance["path"] == "data_query"


# --------------------------------------------------------------------------- #
# Trace never names the model/provider
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_trace_never_names_model(db_session, set_grounding_mode):
    set_grounding_mode("log")
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "what helps with blood pressure?", provider
    )
    blob = " ".join(
        f"{s.get('step','')} {s.get('detail','')}" for s in result.trace
    ).lower()
    assert provider.model_name not in blob
    assert "fake" not in blob  # FakeProvider.model_name


# --------------------------------------------------------------------------- #
# MMR rerank (pure)
# --------------------------------------------------------------------------- #
def test_mmr_prefers_diverse_over_duplicate():
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 0.0],   # duplicate of a
        "c": [0.0, 1.0],   # different information
    }
    order = mmr_rerank(["a", "b", "c"], vectors, k=2)
    assert order == ["a", "c"]


def test_mmr_keeps_relevance_order_without_vectors():
    assert mmr_rerank(["a", "b", "c"], {}, k=2) == ["a", "b"]


def test_mmr_k_bounds():
    assert mmr_rerank([], {}, k=3) == []
    assert mmr_rerank(["a"], {}, k=0) == []
    assert mmr_rerank(["a"], {}, k=5) == ["a"]


# --------------------------------------------------------------------------- #
# Wearable (Sahha) rollups
#
# Three traps, one test each, because each one ships a plausible-looking wrong
# number rather than an error:
#   * the rollup `total` is a plain SUM even for AVERAGE metrics, so a week of
#     resting heart rate reads ~420 bpm unless it is divided by `entries`;
#   * sleep_duration is stored in MINUTES while every renderer here says hours;
#   * an absent bucket is an UNMEASURED day, never a measured zero.
#
# Dates are RELATIVE. The rollups are only read inside the window the reader
# asked about, so a fixed date here is a test that passes until the calendar
# moves past it.
# --------------------------------------------------------------------------- #
def _week_start(day: date) -> date:
    """The SUNDAY that opens the rollup week holding ``day`` — the rollups'
    own convention, not PostgreSQL's Monday and not ``date.weekday()``."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _sahha(metric: str, day: date, total: float, entries: int) -> SahhaDailyTotal:
    return SahhaDailyTotal(
        user_id=USER, metric=metric, bucket_start=day,
        total=total, entries=entries, days_counted=1,
    )


def _weekly(
    metric: str, start: date, total: float, entries: int, days: int = 7
) -> SahhaWeeklyTotal:
    return SahhaWeeklyTotal(
        user_id=USER, metric=metric, bucket_start=start,
        total=total, entries=entries, days_counted=days,
    )


THIS_WEEK = _week_start(utcnow().date())


async def test_average_metric_is_the_mean_not_the_sum(db_session):
    """total=180 across entries=3 is 60 bpm. 180 bpm is the failure."""
    for offset in (0, 1, 2):
        db_session.add(
            _sahha("heart_rate_resting", THIS_WEEK + timedelta(days=offset), 180, 3)
        )
    await db_session.flush()

    points = await wearable_totals(
        db_session, USER, "heart_rate_resting", grain="day", limit=7
    )
    assert [p.value for p in points] == [60.0, 60.0, 60.0]
    assert [p.bucket_start for p in points] == [
        THIS_WEEK, THIS_WEEK + timedelta(days=1), THIS_WEEK + timedelta(days=2)
    ]  # oldest first


async def test_sum_metric_passes_through_untouched(db_session):
    db_session.add(_sahha("steps", THIS_WEEK, 12000, 3))
    await db_session.flush()

    points = await wearable_totals(db_session, USER, "steps", grain="day")
    assert [p.value for p in points] == [12000.0]


async def test_sleep_minutes_render_as_hours(db_session):
    db_session.add(_sahha("sleep_duration", THIS_WEEK, 421, 1))
    await db_session.flush()

    points = await wearable_totals(db_session, USER, "sleep_duration", grain="day")
    assert points[0].value == 421.0            # stored value is untouched
    assert format_wearable("sleep_duration", points[0].value) == "7.0 h"


def test_format_wearable_units():
    assert format_wearable("sleep_duration", 421) == "7.0 h"
    assert format_wearable("steps", 12000) == "12,000 steps"
    assert format_wearable("heart_rate_resting", 60.0) == "60 bpm"
    assert format_wearable("heart_rate_variability_sdnn", 42.5) == "42.5 ms"


async def test_an_absent_bucket_is_a_gap_not_a_zero(db_session):
    for offset in (0, 1, 3):
        db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=offset), 8000, 1))
    await db_session.flush()

    points = await wearable_totals(db_session, USER, "steps", grain="day")
    assert [p.bucket_start for p in points] == [
        THIS_WEEK, THIS_WEEK + timedelta(days=1), THIS_WEEK + timedelta(days=3)
    ]


# --------------------------------------------------------------------------- #
# Finding 11 — the mean must round the way mhn-spring's BigDecimal rounds
# --------------------------------------------------------------------------- #
async def test_the_mean_rounds_half_up_like_bigdecimal(db_session):
    """421/8 is 52.625. Java's HALF_UP gives 52.63; Python's round() gives the
    banker's 52.62, and chat then prints a different number from the app's own
    trend screen for the same week."""
    db_session.add(_weekly("heart_rate_resting", THIS_WEEK, 421, 8))
    await db_session.flush()

    points = await wearable_totals(
        db_session, USER, "heart_rate_resting", grain="week", limit=1
    )
    assert points[0].value == 52.63


def test_headline_half_up_on_the_reachable_ties():
    """`entries` a power of two is ordinary, and every one of these is a tie."""
    from app.coredata.service import _headline

    assert _headline("heart_rate_resting", 481, 8) == 60.13
    assert _headline("heart_rate_resting", 181, 8) == 22.63
    assert _headline("heart_rate_resting", 421, 8) == 52.63


# --------------------------------------------------------------------------- #
# Finding 12 — a bucket with no readings carries no number
# --------------------------------------------------------------------------- #
async def test_a_zero_entry_bucket_is_dropped_not_shown_as_a_mean(db_session):
    """`entries <= 1: return total` showed a week's SUM of resting heart rate
    (420 bpm) as the average. A validator-passing 420 bpm shown to a patient."""
    db_session.add(_weekly("heart_rate_resting", THIS_WEEK, 420, 0))
    await db_session.flush()

    assert await wearable_totals(
        db_session, USER, "heart_rate_resting", grain="week", limit=1
    ) == []
    out = await handle_tracker_query(
        db_session, USER, "what is my resting heart rate this week"
    )
    assert out is None or "420" not in out["reply"]


# --------------------------------------------------------------------------- #
# Findings 14, 15 — the formatters tolerate what the readers tolerate
# --------------------------------------------------------------------------- #
def test_an_unknown_metric_degrades_to_a_bare_number(db_session):
    """`SAHHA_METRICS[metric]` raised KeyError. Sahha's vocabulary grows and an
    unknown metric must be a number, not a 500."""
    from app.coredata.service import wearable_display

    assert wearable_display("respiratory_rate", 14.0) == (14.0, "")
    assert format_wearable("respiratory_rate", 14.0) == "14"


def test_a_single_reading_is_not_quoted_to_six_significant_digits():
    """numeric(16,4) passed straight through at entries == 1, and `:g` printed
    '59.995 bpm' — instrument-grade precision no wrist device has.

    2 dp, not 1: that is what mhn-spring's own mean carries, and chat must not
    print a different number from the app's trend screen for the same week.
    """
    assert format_wearable("heart_rate_resting", 59.995) == "59.99 bpm"
    assert format_wearable("heart_rate_variability_sdnn", 42.4449) == "42.44 ms"
    assert format_wearable("heart_rate_resting", 52.63) == "52.63 bpm"


# --------------------------------------------------------------------------- #
# The legacy wearable answer
# --------------------------------------------------------------------------- #
async def test_tracker_query_answers_sleep_in_hours_from_the_wearable(db_session):
    db_session.add(_weekly("sleep_duration", THIS_WEEK, 2688, 7))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much sleep did I get this week"
    )
    assert out is not None
    # 2688 minutes over the week is 44.8 h. "2688 h" is the failure.
    assert "44.8 h" in out["reply"]
    assert "2688" not in out["reply"]
    assert out["provenance"]["source"] == "wearable"
    assert validate_reply(out["reply"], "none").ok


async def test_tracker_query_averages_resting_heart_rate(db_session):
    db_session.add(_weekly("heart_rate_resting", THIS_WEEK, 420, 7))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "what is my resting heart rate this week"
    )
    assert out is not None
    assert "60 bpm" in out["reply"] and "420" not in out["reply"]
    assert validate_reply(out["reply"], "none").ok


async def test_no_wearable_falls_back_to_the_manual_entry(db_session):
    """An account with no connected device keeps the answer it had."""
    db_session.add(ManualTracking(
        user_id=USER, type="sleep", value=5.5, unit="h",
        effective_from=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much sleep this week")
    assert out is not None
    assert "5.5 h" in out["reply"]
    assert out["provenance"]["source"] == "manual"


async def test_hrv_without_a_device_says_so_rather_than_guessing(db_session):
    out = await handle_tracker_query(db_session, USER, "what is my hrv this week")
    assert out is not None
    assert "connected wearable" in out["reply"]
    assert validate_reply(out["reply"], "none").ok


# --------------------------------------------------------------------------- #
# Finding 13 — "across 1 readings"
# --------------------------------------------------------------------------- #
async def test_the_reading_count_is_pluralised(db_session):
    """A single-reading week is the normal case for a newly-linked device —
    i.e. the first wearable sentence many readers ever see."""
    db_session.add(_weekly("heart_rate_resting", THIS_WEEK, 61, 1, days=1))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "what is my resting heart rate this week"
    )
    assert out is not None
    assert "across 1 reading from" in out["reply"]
    assert "1 readings" not in out["reply"]


# --------------------------------------------------------------------------- #
# Finding 1 — HRV is two different measures, and the device picks which
# --------------------------------------------------------------------------- #
async def test_an_rmssd_only_device_is_answered_and_named(db_session):
    """`heart_rate_variability_rmssd` was in the catalogue and reachable from
    nowhere, so a device reporting it was told it had no HRV at all."""
    db_session.add(_weekly("heart_rate_variability_rmssd", THIS_WEEK, 299, 7))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "what is my hrv this week")
    assert out is not None
    assert "RMSSD" in out["reply"], out["reply"]
    assert "42.71 ms" in out["reply"]          # 299/7, HALF_UP
    assert "SDNN" not in out["reply"]          # never merged, never mislabelled
    assert out["provenance"]["metric"] == "heart_rate_variability_rmssd"
    assert validate_reply(out["reply"], "none").ok


async def test_sdnn_still_wins_when_both_are_present(db_session):
    """The sibling is a fallback, not a merge: the asked-for measure answers."""
    db_session.add(_weekly("heart_rate_variability_sdnn", THIS_WEEK, 280, 7))
    db_session.add(_weekly("heart_rate_variability_rmssd", THIS_WEEK, 299, 7))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "what is my hrv this week")
    assert out is not None
    assert "SDNN" in out["reply"] and "40 ms" in out["reply"]


# --------------------------------------------------------------------------- #
# Findings 5, 7 — a stale rollup is not this week, and must not outrank a
# fresh manual log
# --------------------------------------------------------------------------- #
async def test_a_year_old_bucket_is_not_presented_as_this_week(db_session):
    db_session.add(_weekly("sleep_duration", THIS_WEEK - timedelta(days=371), 2800, 7))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much sleep did I get this week"
    )
    assert out is not None
    assert "46.7 h" not in out["reply"]
    assert out["provenance"]["source"] != "wearable"


async def test_one_stale_wearable_row_does_not_suppress_a_fresh_manual_log(db_session):
    """The fallback fired only on "no rows at all", so any device row of any
    age won. An account that once had a wearable lost its manual answer."""
    db_session.add(_weekly("sleep_duration", THIS_WEEK - timedelta(days=210), 2688, 7))
    db_session.add(ManualTracking(
        user_id=USER, type="sleep", value=8, unit="h",
        effective_from=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "how much sleep did I get this week"
    )
    assert out is not None
    assert out["provenance"]["source"] == "manual"
    assert "8 h" in out["reply"]
    assert "44.8" not in out["reply"]


async def test_the_named_week_always_carries_its_year(db_session):
    db_session.add(_weekly("steps", THIS_WEEK, 70000, 7))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    assert THIS_WEEK.strftime("in the week of %d %b %Y") in out["reply"]


# --------------------------------------------------------------------------- #
# Finding 4 — `period` is accepted, advertised and was ignored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("period_phrase", ["this month", "this year"])
async def test_a_month_or_year_ask_says_it_only_has_a_week(db_session, period_phrase):
    """Answering a month with a week is defensible; doing it silently, with a
    receipt that says "month", is not."""
    db_session.add(_weekly("steps", THIS_WEEK, 14300, 2, days=2))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, f"how many steps did I take {period_phrase}"
    )
    assert out is not None
    assert "only have weekly totals" in out["reply"]
    assert out["provenance"]["period"] == "week"   # never a window it did not read
    assert validate_reply(out["reply"], "none").ok


async def test_a_week_ask_carries_no_apology(db_session):
    db_session.add(_weekly("steps", THIS_WEEK, 14300, 2, days=2))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    assert "only have weekly totals" not in out["reply"]


# --------------------------------------------------------------------------- #
# Findings 2, 6 and the client contract — the chart is the week the sentence
# names, one slot per day, `None` where nothing was recorded
# --------------------------------------------------------------------------- #
async def test_the_chart_covers_exactly_the_week_the_sentence_names(db_session):
    """The headline read the latest WEEKLY row and the chart read the last 7
    DAILY rows, which straddle two weeks on every day but the week's last — so
    the bars did not add up to the number printed above them."""
    db_session.add(_weekly("steps", THIS_WEEK, 21000, 3, days=3))
    for offset in (0, 1, 2):
        db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=offset), 7000, 1))
    # A day in the NEXT week must not appear on this week's chart.
    db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=7), 9999, 1))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    visual = out["visual"]
    assert visual["labels"] == [
        (THIS_WEEK + timedelta(days=i)).strftime("%d %b") for i in range(7)
    ]
    assert visual["values"] == [7000, 7000, 7000, None, None, None, None]
    assert 9999 not in [v for v in visual["values"] if v is not None]
    # The bars add up to the sentence's total.
    assert sum(v for v in visual["values"] if v is not None) == 21000


async def test_the_chart_title_states_the_window_it_actually_holds(db_session):
    """"last 7 days" over the last 7 EXISTING rows put three months of data
    under a seven-day claim — and chart text passes through no validator."""
    db_session.add(_weekly("steps", THIS_WEEK, 16000, 2, days=2))
    for offset in (0, 1):
        db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=offset), 8000, 1))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    assert out["visual"]["title"] == THIS_WEEK.strftime(
        "steps — week of %d %b %Y"
    )
    assert "last 7 days" not in out["visual"]["title"]
    # A month in every label: "Tue 03" is byte-identical 28 days apart.
    assert all(len(label.split()) == 2 for label in out["visual"]["labels"])


async def test_the_payload_carries_the_descriptors_a_client_routes_on(db_session):
    db_session.add(_weekly("sleep_duration", THIS_WEEK, 840, 2, days=2))
    for offset in (0, 1):
        db_session.add(
            _sahha("sleep_duration", THIS_WEEK + timedelta(days=offset), 420, 1)
        )
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how much sleep this week")
    assert out is not None
    visual = out["visual"]
    assert visual["source"] == "wearable"
    assert visual["metric"] == "sleep_duration"
    assert visual["grain"] == "day"
    assert visual["window_days"] == 7
    assert len(visual["values"]) == len(visual["labels"]) == 7
    # Davi names the subject, never the screen.
    assert not {"route", "screen", "deeplink", "deep_link"} & set(visual)
    # The bars are in the same units as the sentence.
    assert visual["values"][:2] == [7.0, 7.0]
    assert visual["unit"] == "h"
    assert "7 h" in out["reply"] or "14.0 h" in out["reply"]


async def test_a_measured_zero_is_kept_and_an_absent_day_is_null(db_session):
    """Phone left at home is 0 steps and is a real reading; a day the device
    never synced is not. Both must be expressible and they are not the same."""
    db_session.add(_weekly("steps", THIS_WEEK, 8000, 2, days=2))
    db_session.add(_sahha("steps", THIS_WEEK, 8000, 1))
    db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=1), 0, 1))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    assert out["visual"]["values"][:3] == [8000, 0, None]


async def test_a_null_slot_draws_no_bar(db_session):
    """A stub is the client's job; the SVG must not draw an unmeasured day as
    a zero-height bar with a "0" label on it."""
    from app.charts.svg import bar_chart

    svg = bar_chart("t", ["a", "b", "c"], [10.0, None, 30.0])
    assert svg.count("<rect") == 3          # 1 background + 2 bars, not 3
    assert ">10<" in svg and ">30<" in svg


async def test_one_bucket_is_not_charted(db_session):
    """chart_payload pads a flat single-bar series by +/-1, which looks like
    data. The metric-trend caller has always guarded on two points."""
    db_session.add(_weekly("steps", THIS_WEEK, 8000, 1, days=1))
    db_session.add(_sahha("steps", THIS_WEEK, 8000, 1))
    await db_session.flush()

    out = await handle_tracker_query(db_session, USER, "how many steps this week")
    assert out is not None
    assert "visual" not in out


# --------------------------------------------------------------------------- #
# Findings 3, 8, 9, 10 — the tool surface
# --------------------------------------------------------------------------- #
async def test_no_wearable_resting_hr_answers_from_the_logged_pulse(db_session):
    """Returning None is a FALL-THROUGH, which only works for a caller that has
    a chain underneath it. The tool has none and turned it into "nothing on
    file" while a real reading sat one table away."""
    db_session.add(VitalReading(
        user_id=USER, vital_type="heart_rate", value_primary=72,
        unit="bpm", recorded_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_tracker_query(
        db_session, USER, "what is my resting heart rate"
    )
    assert out is not None
    assert "72 bpm" in out["reply"]
    assert out["provenance"]["path"] == "metric_query"


async def test_the_resting_hr_tool_never_asserts_a_false_absence(db_session):
    from app.chat.tools.registry import execute_tool

    db_session.add(VitalReading(
        user_id=USER, vital_type="heart_rate", value_primary=72,
        unit="bpm", recorded_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    result = await execute_tool(
        db_session, USER,
        ToolCall(id="c1", name="get_tracker_total",
                 arguments={"metric": "resting heart rate"}),
        None,
    )
    payload = json.loads(result.content)
    assert payload.get("found") is not False, payload
    assert "72 bpm" in payload["deterministic_reply"]


async def test_no_data_at_all_reports_a_real_absence(db_session):
    """No wearable AND no logged pulse. This is the same reply the legacy chain
    already produced from handle_metric_query one slot below, so legacy does not
    move — but the tool now says it too, instead of the bare "nothing on file"
    it used to emit whether or not a reading existed."""
    out = await handle_tracker_query(
        db_session, USER, "what is my resting heart rate"
    )
    assert out is not None
    assert "couldn't find any heart rate readings" in out["reply"]
    assert out["provenance"] == {"path": "metric_query", "found": False}


async def test_an_out_of_enum_metric_is_not_reported_as_no_data(db_session):
    """"heart rate", "blood pressure" and "weight" are all data this app holds,
    and all three resolved to None and were reported as "Nothing on file"."""
    from app.chat.tools.registry import execute_tool

    for metric in ("heart rate", "blood pressure", "weight"):
        result = await execute_tool(
            db_session, USER,
            ToolCall(id="c1", name="get_tracker_total",
                     arguments={"metric": metric}),
            None,
        )
        payload = json.loads(result.content)
        assert "Nothing on file" not in result.content, metric
        assert "get_latest_metric" in payload["note"], metric


async def test_a_wearable_number_is_never_graded(db_session):
    """Davi has no reference ranges for wearable metrics and the contract
    forbids a band, grade or traffic light on one. A sentence in the tool
    description is not a guard."""
    from app.chat.tools.registry import execute_tool

    for metric in ("resting heart rate", "hrv", "sleep", "steps"):
        result = await execute_tool(
            db_session, USER,
            ToolCall(id="c1", name="check_value_against_range",
                     arguments={"metric": metric, "value": 48}),
            None,
        )
        payload = json.loads(result.content)
        assert payload.get("graded") is False, metric
        assert "within the typical range" not in result.content, metric
        assert validate_reply(payload["deterministic_reply"], "none").ok


async def test_the_typed_phrasing_is_not_graded_either(db_session):
    """The executor synthesises "my {metric} is {value}" and hands it to this
    handler, so one guard closes both engines and the reader's own words."""
    from app.chat.data_handlers import handle_value_check

    out = await handle_value_check(db_session, USER, "my resting heart rate is 48")
    assert out is not None
    assert out["provenance"]["declined"] == "wearable_no_range"
    assert "typical range" not in out["reply"]

    # A clinic pulse is untouched.
    clinic = await handle_value_check(db_session, USER, "my heart rate is 48")
    assert clinic is not None
    assert clinic["provenance"].get("declined") is None


async def test_a_lookup_is_not_mistaken_for_a_grade(db_session):
    """The guard is gated on a PARSED value: "how much sleep did I get this
    week" is a lookup and must reach the tracker handler."""
    from app.chat.data_handlers import handle_value_check

    assert await handle_value_check(
        db_session, USER, "how much sleep did I get this week"
    ) is None


async def test_the_svg_never_reaches_the_model(db_session):
    """~3.3 KB of SVG (roughly 900 prompt tokens) the model cannot read, on a
    tool the description tells it to always prefer — and its numbers are a
    fidelity trap besides: the SVG stores a bar as `58</text>`."""
    from app.chat.tools.registry import execute_tool

    db_session.add(_weekly("steps", THIS_WEEK, 16000, 2, days=2))
    for offset in (0, 1):
        db_session.add(_sahha("steps", THIS_WEEK + timedelta(days=offset), 8000, 1))
    await db_session.flush()

    visuals: list[dict] = []
    result = await execute_tool(
        db_session, USER,
        ToolCall(id="c1", name="get_tracker_total",
                 arguments={"metric": "steps", "period": "week"}),
        None, visuals=visuals,
    )
    assert "<svg" not in result.content
    assert "_visual" not in result.content
    assert len(result.content) < 1000
    # ...but the caller still gets the chart.
    assert visuals and visuals[0]["svg"].startswith("<svg")


# --------------------------------------------------------------------------- #
# Both engines, one number — and one chart
# --------------------------------------------------------------------------- #
class _CallsTrackerTool(FakeProvider):
    """Calls get_tracker_total, then answers with the tool's own wording.

    The tool's ``deterministic_reply`` IS the legacy handler's sentence, so the
    equality below breaks the moment the two engines compute different numbers
    for the same question.
    """

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


async def test_both_engines_report_the_same_wearable_number_and_chart(
    db_session, monkeypatch
):
    from app.config import get_settings

    db_session.add(_weekly("sleep_duration", THIS_WEEK, 2688, 7))
    for offset in range(7):
        db_session.add(
            _sahha("sleep_duration", THIS_WEEK + timedelta(days=offset), 384, 1)
        )
    await db_session.flush()
    question = "how much sleep did I get this week"

    monkeypatch.setattr(get_settings(), "chat_engine", "legacy")
    legacy = await handle_chat(
        db_session, USER, question, FakeProvider(), uuid.uuid4()
    )

    monkeypatch.setattr(get_settings(), "chat_engine", "agentic")
    provider = _CallsTrackerTool({"metric": "sleep", "period": "week"})
    agentic = await handle_chat(
        db_session, USER, question, provider, uuid.uuid4()
    )

    assert "44.8 h" in legacy.response_message
    assert legacy.response_message == agentic.response_message
    # The chart was built, paid for in tokens and then DROPPED on agentic.
    assert legacy.visual is not None
    assert agentic.visual == legacy.visual

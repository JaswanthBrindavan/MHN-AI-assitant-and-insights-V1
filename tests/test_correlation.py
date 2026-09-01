"""Co-occurrence: what the record shows together, and what it refuses to say.

The invariants a confident wrong answer would break: a pattern over three days
presented as a finding, a day the device never measured counted as a zero, a
causal verb in the sentence, a medication on either side of the comparison,
and the routing bug that made the whole feature unreachable -- "does coffee
affect my sleep" was answered as a week's coffee total, so a handler placed at
or after the tracker slot would never have run.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.chat.abilities import parse_correlation_query, parse_tracker_query
from app.chat.correlation import (
    MIN_DAYS_PER_GROUP,
    READ_FAILED,
    WINDOW_DAYS,
    co_occurrence,
    render_co_occurrence,
)
from app.chat.data_handlers import handle_correlation_query
from app.chat.orchestrator import handle_chat
from app.chat.replies import safe_reply
from app.chat.validation import validate_reply
from app.llm.fake import FakeProvider
from app.models.common import utcnow
from app.models.coredata import LifestyleDailyTotal, SahhaDailyTotal
from app.triage.red_flags import EMERGENCY, HIGH, NONE

USER = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
# asyncio_mode = "auto" (pyproject) — no per-test marker needed, and a module
# marker would warn on the pure (sync) half of this file.

# Language that would turn an observation into a claim. None of it may ever
# appear in a rendered sentence, on any branch.
CAUSAL_WORDS = (
    "caused", "causes", "causing", "because", "due to", "is affecting",
    "leads to", "responsible for", "you should", "try cutting", "cut down",
    "reduce your", "is why", "proves", "this means you",
)


def _day(n: int) -> date:
    """N days before today. Today itself is never in the window."""
    return utcnow().date() - timedelta(days=n)


# --------------------------------------------------------------------------- #
# The pure engine
# --------------------------------------------------------------------------- #
def test_a_three_day_pattern_is_refused_not_reported():
    """Refusing is the feature. Three days is noise with a sentence attached."""
    outcome = {_day(i): 400.0 for i in range(1, 21)}
    logged = {_day(1), _day(2), _day(3)}

    f = co_occurrence("coffee", "sleep_duration", logged, outcome)

    assert not f.enough
    assert f.days_with == 3
    assert f.mean_with is None and f.mean_without is None
    text = render_co_occurrence(f, label="sleep", unit="minute")
    assert "I do not have enough days to say" in text
    assert "7" in text  # it says what it would have needed


def test_the_minimum_is_seven_days_on_each_side():
    """Exactly at the threshold it answers; one day short on either side it does not."""
    outcome = {_day(i): 400.0 for i in range(1, 15)}
    seven = {_day(i) for i in range(1, 8)}

    assert co_occurrence("coffee", "sleep_duration", seven, outcome).enough
    assert MIN_DAYS_PER_GROUP == 7

    six = {_day(i) for i in range(1, 7)}
    assert not co_occurrence("coffee", "sleep_duration", six, outcome).enough
    thirteen = {_day(i) for i in range(1, 14)}
    assert not co_occurrence("coffee", "sleep_duration", thirteen, outcome).enough


def test_a_day_the_device_never_measured_enters_neither_group():
    """Missing is not zero. A gap must not drag a mean down."""
    outcome = {_day(i): 400.0 for i in range(1, 8)}          # 7 measured days
    logged = {_day(i) for i in range(1, 21)}                  # logged on 20

    f = co_occurrence("coffee", "sleep_duration", logged, outcome)

    assert f.measured_days == 7
    assert f.days_with == 7 and f.days_without == 0
    assert not f.enough  # nothing to compare against


def test_the_difference_is_reported_in_minutes_with_its_direction():
    outcome = {}
    for i in range(1, 10):
        outcome[_day(i)] = 360.0          # 9 coffee days, 6 h
    for i in range(10, 24):
        outcome[_day(i)] = 402.0          # 14 non-coffee days, 6 h 42
    logged = {_day(i) for i in range(1, 10)}

    f = co_occurrence("coffee", "sleep_duration", logged, outcome)
    text = render_co_occurrence(f, label="sleep", unit="minute")

    assert "On the 9 days" in text
    assert "42 minutes less" in text
    assert "the 14 days you did not" in text
    assert "not evidence that one caused the other" in text


def test_the_other_direction_and_a_rate_unit_read_correctly():
    outcome = {_day(i): 66.0 for i in range(1, 9)}
    outcome.update({_day(i): 62.0 for i in range(9, 20)})
    logged = {_day(i) for i in range(1, 9)}

    f = co_occurrence("alcohol", "heart_rate_resting", logged, outcome)
    text = render_co_occurrence(f, label="resting heart rate", unit="bpm")

    assert "4 bpm higher" in text


def test_a_difference_that_rounds_away_is_not_dressed_up_as_one():
    outcome = {_day(i): 400.0 for i in range(1, 11)}
    outcome.update({_day(i): 400.2 for i in range(11, 22)})
    logged = {_day(i) for i in range(1, 11)}

    f = co_occurrence("water", "sleep_duration", logged, outcome)
    text = render_co_occurrence(f, label="sleep", unit="minute")

    assert "about the same" in text
    assert "0 minutes" not in text


def test_no_branch_of_the_wording_is_causal_or_advisory():
    """Every reachable sentence, checked against the validator and by hand."""
    outcome = {_day(i): 360.0 for i in range(1, 10)}
    outcome.update({_day(i): 402.0 for i in range(10, 24)})
    cases = [
        co_occurrence("coffee", "sleep_duration", {_day(i) for i in range(1, 10)},
                      outcome),                                  # a finding
        co_occurrence("coffee", "sleep_duration", {_day(1)}, outcome),  # too few
        co_occurrence("coffee", "sleep_duration", set(), {}),    # nothing at all
    ]
    for f in cases:
        text = render_co_occurrence(f, label="sleep", unit="minute")
        # The caveat clause is ALLOWED to say "caused" -- it says the record is
        # not evidence of it. The finding itself may not, so the words are
        # checked against what comes before the caveat.
        finding_clause = text.split("That is what your records show")[0].lower()
        for word in CAUSAL_WORDS:
            assert word not in finding_clause, f"{word!r} in {text!r}"
        verdict = validate_reply(text, NONE)
        assert verdict.ok, f"{verdict.reason}: {text}"
    reported = render_co_occurrence(cases[0], label="sleep", unit="minute")
    assert "not evidence that one caused the other" in reported


# --------------------------------------------------------------------------- #
# The routing bug — the reason placement matters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("does coffee affect my sleep", ("coffee", "sleep_duration")),
        ("is my hrv lower when i drink", ("alcohol", "heart_rate_variability_sdnn")),
        ("does alcohol affect my sleep", ("alcohol", "sleep_duration")),
        ("is my resting heart rate higher on days i drink alcohol",
         ("alcohol", "heart_rate_resting")),
        ("does tea affect my sleep", ("tea", "sleep_duration")),
    ],
)
def test_the_real_phrasings_parse_and_the_tracker_lets_them_go(message, expected):
    """Both halves. Parsing is useless if `parse_tracker_query` still claims it."""
    q = parse_correlation_query(message)
    assert q is not None
    assert (q.input_key, q.outcome_metric) == expected
    assert parse_tracker_query(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "how much coffee did i drink this week",
        "how much water did i drink this week",
        "how did i sleep this week",
        "how much sleep should i get",
        "why am i so tired lately?",
    ],
)
def test_ordinary_lookups_are_untouched(message):
    assert parse_correlation_query(message) is None


def test_a_medication_is_never_one_side_of_a_comparison():
    """`CORRELATION_INPUTS` is four lifestyle log types. No phrasing reaches a
    drug -- and a message that NAMES a medication is declined by name rather
    than answered about something else."""
    for message in (
        "does my blood pressure tablet affect my resting heart rate",
        # The one that used to slip through: a habit term also matched, so the
        # medication check (which lived inside the bare-"drink" branch)
        # guarded nothing and the reader got a coffee-vs-steps readout.
        "does my blood pressure tablet affect my steps when i drink coffee",
    ):
        parsed = parse_correlation_query(message)
        assert parsed is not None, message
        assert parsed.declined == "medication", message
        assert parsed.input_key == ""
    # A bare generic or brand name with no medication NOUN is beyond a pure
    # parser -- it has no catalogue -- and those return None as before, which
    # keeps them off this path.
    for message in (
        "does my metformin affect my sleep",
        "is my hrv lower when i take amlodipine",
    ):
        assert parse_correlation_query(message) is None, message


async def test_a_medication_question_is_declined_not_answered_about_coffee(
    db_session,
):
    _seed(db_session, coffee_days=9)
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER,
        "does my blood pressure tablet affect my steps when i drink coffee",
    )

    assert out is not None
    assert out["provenance"]["declined"] == "medication"
    assert "coffee" not in out["reply"]
    assert validate_reply(out["reply"], NONE).ok


async def test_days_before_the_reader_started_tracking_are_not_a_group(db_session):
    """The likeliest real input: someone who started logging a week ago asks
    the question the feature was built for. 21 days of "no coffee row" are 21
    days before they had the feature, and counting them manufactured a
    finding out of nothing."""
    for i in range(1, 29):
        db_session.add(SahhaDailyTotal(
            user_id=USER, metric="sleep_duration", bucket_start=_day(i),
            total=360.0 if i <= 7 else 420.0, entries=1, days_counted=1,
        ))
    for i in range(1, 8):
        db_session.add(LifestyleDailyTotal(
            user_id=USER, metric="coffee", bucket_start=_day(i),
            total=2.0, entries=2, days_counted=1,
        ))
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )

    assert out is not None
    assert out["provenance"]["days_without"] == 0
    assert out["provenance"]["enough"] is False
    assert "I do not have enough days to say" in out["reply"]
    assert "60 minutes" not in out["reply"]


# --------------------------------------------------------------------------- #
# The handler, over real rows
# --------------------------------------------------------------------------- #
def _seed(db, *, coffee_days: int, sleep_metric="sleep_duration", days=24):
    """`days` measured days; the first `coffee_days` of them carry a coffee log.

    A water row on EVERY one of those days too: the reader has been tracking
    for the whole window. Without it the window is clamped at the reader's
    first lifestyle row -- which is the point of that clamp, and is covered by
    `test_days_before_the_reader_started_tracking_are_not_a_group` below.
    """
    for i in range(1, days + 1):
        db.add(SahhaDailyTotal(
            user_id=USER, metric=sleep_metric, bucket_start=_day(i),
            total=360.0 if i <= coffee_days else 402.0,
            entries=1, days_counted=1,
        ))
        db.add(LifestyleDailyTotal(
            user_id=USER, metric="water", bucket_start=_day(i),
            total=2000.0, entries=2, days_counted=1,
        ))
    for i in range(1, coffee_days + 1):
        db.add(LifestyleDailyTotal(
            user_id=USER, metric="coffee", bucket_start=_day(i),
            total=2.0, entries=2, days_counted=1,
        ))


async def test_the_handler_answers_from_the_two_rollups(db_session):
    _seed(db_session, coffee_days=9)
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )

    assert out is not None
    assert out["provenance"]["path"] == "correlation_query"
    assert out["provenance"]["days_with"] == 9
    assert out["provenance"]["days_without"] == 15
    assert out["provenance"]["enough"] is True
    assert "42 minutes less" in out["reply"]
    assert "visual" not in out


async def test_a_sparse_account_gets_the_refusal_not_a_number(db_session):
    _seed(db_session, coffee_days=3, days=12)
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )

    assert out is not None
    assert out["provenance"]["enough"] is False
    assert "I do not have enough days to say" in out["reply"]
    assert "minutes" not in out["reply"]


async def test_today_is_not_in_the_window(db_session):
    """A day in progress has partial steps and no sleep yet."""
    today = utcnow().date()
    db_session.add(SahhaDailyTotal(
        user_id=USER, metric="sleep_duration", bucket_start=today,
        total=1.0, entries=1, days_counted=1,
    ))
    _seed(db_session, coffee_days=9)
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )

    assert out is not None
    assert out["provenance"]["days_with"] + out["provenance"]["days_without"] == 24


async def test_hrv_falls_back_to_the_sibling_measure(db_session):
    """A device reports SDNN or RMSSD. Asking for one and finding nothing is
    not an answer -- and the reply must name the one that answered."""
    for i in range(1, 25):
        db_session.add(SahhaDailyTotal(
            user_id=USER, metric="heart_rate_variability_rmssd",
            bucket_start=_day(i), total=40.0 if i <= 9 else 48.0,
            entries=1, days_counted=1,
        ))
        db_session.add(LifestyleDailyTotal(
            user_id=USER, metric="water", bucket_start=_day(i),
            total=2000.0, entries=2, days_counted=1,
        ))
        if i <= 9:
            db_session.add(LifestyleDailyTotal(
                user_id=USER, metric="alcohol", bucket_start=_day(i),
                total=1.0, entries=1, days_counted=1,
            ))
    await db_session.flush()

    out = await handle_correlation_query(
        db_session, USER, "is my hrv lower when i drink"
    )

    assert out is not None
    assert out["provenance"]["outcome"] == "heart_rate_variability_rmssd"
    assert "HRV (RMSSD)" in out["reply"]
    assert "8 ms lower" in out["reply"]


async def test_nothing_on_record_says_so_without_inventing_a_pattern(db_session):
    out = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )

    assert out is not None
    assert out["provenance"]["days_with"] == 0
    assert "nothing to compare" in out["reply"]


# --------------------------------------------------------------------------- #
# Both engines, and the triage floor
# --------------------------------------------------------------------------- #
async def test_both_engines_give_the_same_answer(db_session, monkeypatch):
    """Byte-identical. The handler runs in the SHARED prologue, above the
    engine branch, so neither engine can answer this from model weights."""
    from app.config import get_settings

    _seed(db_session, coffee_days=9)
    await db_session.flush()

    replies = {}
    for engine in ("legacy", "agentic"):
        get_settings.cache_clear()
        monkeypatch.setenv("CHAT_ENGINE", engine)
        try:
            result = await handle_chat(
                db_session, USER, "does coffee affect my sleep", FakeProvider()
            )
        finally:
            get_settings.cache_clear()
        assert result.provenance.get("path") == "correlation_query"
        replies[engine] = result.response_message

    assert replies["legacy"] == replies["agentic"]
    assert "42 minutes less" in replies["legacy"]
    assert replies["legacy"] != safe_reply(NONE, None)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("does coffee affect my sleep, also I can't breathe", EMERGENCY),
        ("does coffee affect my sleep, and my chest hurts", HIGH),
    ],
)
async def test_the_triage_floor_wins(db_session, message, expected):
    """A reassuring four-week average beside a red flag is a reason to delay care."""
    _seed(db_session, coffee_days=9)
    await db_session.flush()

    result = await handle_chat(db_session, USER, message, FakeProvider())

    assert result.risk_level == expected
    assert result.provenance.get("path") != "correlation_query"
    assert "42 minutes" not in result.response_message


async def test_a_broken_read_says_so_instead_of_handing_it_to_the_model(
    db_session, monkeypatch
):
    """The one fail-open that must NOT fall through.

    Returning None on a read failure would send "does coffee affect my sleep"
    down the RAG path, and a model answers that causally from its own weights.
    That is the bypass this whole slot exists to close, so the failure has to
    be a sentence, not a hand-off.
    """
    from app.chat import orchestrator

    async def _boom(*_a, **_k):
        raise RuntimeError("rollup table missing")

    monkeypatch.setattr(orchestrator, "handle_correlation_query", _boom)

    result = await handle_chat(
        db_session, USER, "does coffee affect my sleep", FakeProvider()
    )

    assert result.provenance["path"] == "correlation_query"
    assert result.provenance["degraded"] == "read_failed"
    assert result.response_message == READ_FAILED
    assert validate_reply(READ_FAILED, NONE).ok


def test_the_window_is_four_whole_weeks():
    assert WINDOW_DAYS == 28


# --------------------------------------------------------------------------- #
# The gates the review found open. Each was reproduced end to end through
# `handle_chat` before the fix, so each is pinned end to end here.
# --------------------------------------------------------------------------- #
def _seed_everything(db, *, days=24, logged=9):
    """Both sides of every supported pair, for one seeded reader."""
    for i in range(1, days + 1):
        for metric, total in (
            ("sleep_duration", 360.0), ("steps", 8000.0),
            ("heart_rate_resting", 62.0),
        ):
            db.add(SahhaDailyTotal(
                user_id=USER, metric=metric, bucket_start=_day(i),
                total=total, entries=1, days_counted=1,
            ))
    for habit in ("coffee", "tea", "alcohol", "smoking"):
        for i in range(1, logged + 1):
            db.add(LifestyleDailyTotal(
                user_id=USER, metric=habit, bucket_start=_day(i),
                total=2.0, entries=2, days_counted=1,
            ))


@pytest.mark.parametrize(
    "message",
    [
        # A question about the WORLD, which the validated corpus answers. Each
        # of these was claimed by the slot and answered out of the reader's
        # private log, above the scope guard and above RAG.
        "does coffee affect sleep",
        "is coffee linked to poor sleep",
        "what is the relationship between alcohol and sleep",
    ],
)
async def test_a_general_knowledge_question_never_reads_the_private_log(
    db_session, message
):
    _seed_everything(db_session)
    await db_session.flush()

    result = await handle_chat(db_session, USER, message, FakeProvider())

    assert result.provenance.get("path") != "correlation_query"
    assert "you logged" not in result.response_message.lower()


@pytest.mark.parametrize(
    ("message", "input_key", "outcome"),
    [
        # Smoking is a lifestyle_log type with a daily rollup and was simply
        # absent from CORRELATION_INPUTS: this came back as a cigarette count.
        ("does smoking affect my sleep", "smoking", "sleep_duration"),
        # Steps is a first-class Sahha metric and was absent from the outcomes.
        ("does tea affect my steps", "tea", "steps"),
        # `_TRACKER_TERMS` has no bare "heart rate" (it protects
        # handle_metric_query), so the most natural phrasing there is could
        # not reach the guard at all and the model answered from its weights.
        ("does alcohol affect my heart rate", "alcohol", "heart_rate_resting"),
        ("is my pulse higher when i drink", "alcohol", "heart_rate_resting"),
    ],
)
async def test_the_bypass_is_closed_for_every_family(
    db_session, message, input_key, outcome
):
    _seed_everything(db_session)
    await db_session.flush()

    result = await handle_chat(db_session, USER, message, FakeProvider())

    assert result.provenance["path"] == "correlation_query"
    assert result.provenance["input"] == input_key
    assert result.provenance["outcome"] == outcome


async def test_an_unpairable_outcome_is_declined_not_handed_to_the_tracker(
    db_session,
):
    """Silence here is what made "does coffee affect my blood pressure" come
    back as a coffee total. Saying which pairs exist is the fix."""
    _seed_everything(db_session)
    await db_session.flush()

    result = await handle_chat(
        db_session, USER, "does coffee affect my blood pressure", FakeProvider()
    )

    assert result.provenance["path"] == "correlation_query"
    assert result.provenance["declined"] == "unpairable_outcome"
    assert "cups of coffee" not in result.response_message
    assert validate_reply(result.response_message, NONE).ok


@pytest.mark.parametrize(
    "message",
    [
        # A question about what a person NEEDS, which 28 days of the reader's
        # own nights does not address at all.
        "how many hours of sleep do i need when i drink coffee",
        "how much sleep should i get when i drink coffee",
    ],
)
async def test_a_normative_question_is_not_answered_from_personal_data(
    db_session, message
):
    _seed_everything(db_session)
    await db_session.flush()

    result = await handle_chat(db_session, USER, message, FakeProvider())

    assert result.provenance.get("path") != "correlation_query"


async def test_a_medication_question_is_not_guessed_as_alcohol(db_session):
    """The bare-drink heuristic reached across the medication boundary."""
    _seed_everything(db_session)
    await db_session.flush()

    parsed = parse_correlation_query(
        "does my medicine affect my sleep when i drink"
    )
    assert parsed is not None and parsed.declined == "medication"
    result = await handle_chat(
        db_session, USER, "does my medicine affect my sleep when i drink",
        FakeProvider(),
    )
    # Claimed and DECLINED, never answered as a drinking comparison.
    assert result.provenance.get("declined") == "medication"
    assert "alcohol" not in result.response_message


async def test_the_phrasing_the_module_exists_for_still_works(db_session):
    """"lower" is in the advice guard, which is why this parser skips it."""
    _seed_everything(db_session)
    await db_session.flush()

    result = await handle_chat(
        db_session, USER, "is my hrv lower when i drink", FakeProvider()
    )

    assert result.provenance["path"] == "correlation_query"
    assert result.provenance["input"] == "alcohol"


def test_steps_are_rendered_as_a_count_not_a_unit():
    outcome = {_day(i): (7000.0 if i <= 9 else 9000.0) for i in range(1, 25)}
    logged = {_day(i) for i in range(1, 10)}

    text = render_co_occurrence(
        co_occurrence("coffee", "steps", logged, outcome),
        label="steps", unit="count",
    )

    assert "2,000 fewer" in text
    assert "count" not in text

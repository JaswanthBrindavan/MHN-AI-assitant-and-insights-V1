"""The card that says what kind of day yesterday was.

Three properties matter more than the wording, and every one of them is easy to
lose in a later edit:

* **nothing is claimed about a number that was never recorded**
* **nothing is presented as a cause** — one day cannot separate two orderings
* a symptom somebody typed **outranks** anything a wrist strap measured,
  however dramatic the wrist strap was being

The ladder came from an Android implementation with an equivalent suite; these
are the same cases, so the two cannot drift without one of them going red.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.models.chat import SymptomLog
from app.models.coredata import PeriodDayLog
from app.patterns.service import _symptoms_on
from app.patterns.yesterday import (
    DaySignal,
    Trend,
    YesterdayFacts,
    YesterdaySummary,
    baseline_of,
    day_signal,
    summarise_yesterday,
)

USER = uuid.UUID("aaaa1111-2222-3333-4444-555566667777")


def summarised(facts: YesterdayFacts) -> YesterdaySummary:
    """`summarise_yesterday` narrowed to non-None.

    Every test below has already established there is something to say; asking
    each of them to re-assert it would be 30 lines of noise. The one case that
    is ABOUT the None keeps calling the real function directly.
    """
    said = summarise_yesterday(facts)
    assert said is not None
    return said



def above(value: float, baseline: float) -> DaySignal:
    return DaySignal(value, baseline, Trend.ABOVE)


def below(value: float, baseline: float) -> DaySignal:
    return DaySignal(value, baseline, Trend.BELOW)


def usual(value: float, baseline: float | None = None) -> DaySignal:
    return DaySignal(value, value if baseline is None else baseline, Trend.USUAL)


# --------------------------------------------------------------------------- #
# Nothing to say
# --------------------------------------------------------------------------- #

def test_no_data_at_all_draws_no_card():
    assert summarise_yesterday(YesterdayFacts()) is None


def test_an_ordinary_day_is_called_ordinary_rather_than_dressed_up():
    said = summarised(
        YesterdayFacts(
            sleep_minutes=usual(452), steps=usual(8420),
            resting_heart_rate=usual(58),
        )
    )
    assert said.headline == "Yesterday was a steady day with no major changes."
    assert "close to your usual levels" in said.detail


def test_a_steady_day_only_names_what_was_actually_recorded():
    """Water logged by hand and no wearable at all. Claiming steady vitals here
    would be a lie the reader has no way of catching."""
    said = summarised(YesterdayFacts(water_ml=usual(1800)))

    assert "vitals" not in said.detail
    assert "sleep" not in said.detail
    assert "daily logs" in said.detail


# --------------------------------------------------------------------------- #
# Priority
# --------------------------------------------------------------------------- #

def test_a_reported_symptom_outranks_every_measured_signal():
    said = summarised(
        YesterdayFacts(
            symptoms=("headache",),
            # Loud wearable news that must NOT win the headline.
            hrv=above(88, 60),
            resting_heart_rate=below(48, 58),
            sleep_minutes=below(320, 450),
            water_ml=below(700, 1900),
        )
    )
    assert said.headline == "Yesterday was mostly stable, but you reported a headache."
    assert "HRV" not in said.headline


def test_several_symptoms_make_it_a_tougher_day():
    said = summarised(
        YesterdayFacts(
            symptoms=("fatigue", "headache"),
            sleep_minutes=below(330, 450),
            steps=below(2100, 8000),
            water_ml=below(700, 1900),
        )
    )
    assert said.headline == (
        "Yesterday was a tougher day. You reported fatigue and a headache."
    )
    assert said.detail == (
        "Your sleep, activity and hydration were all below your usual levels."
    )


def test_vitals_outrank_sleep_activity_and_lifestyle():
    said = summarised(
        YesterdayFacts(
            hrv=above(88, 60), resting_heart_rate=below(48, 58),
            sleep_minutes=above(468, 430), steps=above(14000, 8000),
        )
    )
    assert said.headline == (
        "Your recovery looked better yesterday. HRV was higher and resting "
        "heart rate was lower than your recent baseline."
    )


def test_vitals_pointing_both_ways_say_nothing_and_fall_through():
    """HRV up and resting heart rate ALSO up is not a recovery story. Rather
    than pick a side the rung declines, and sleep gets the headline."""
    said = summarised(
        YesterdayFacts(
            hrv=above(88, 60), resting_heart_rate=above(66, 58),
            sleep_minutes=below(320, 450),
        )
    )
    assert said.headline == "Yesterday your sleep was shorter than usual."


def test_an_oxygen_drop_outranks_the_recovery_reading():
    said = summarised(
        YesterdayFacts(spo2=below(93, 97), hrv=above(88, 60))
    )
    assert "oxygen saturation" in said.headline
    assert "93%" in said.detail


# --------------------------------------------------------------------------- #
# Co-occurrence, never causation
# --------------------------------------------------------------------------- #

def test_caffeine_and_short_sleep_are_side_by_side_not_cause_and_effect():
    said = summarised(
        YesterdayFacts(sleep_minutes=below(352, 450), caffeine_cups=above(3, 1))
    )
    assert said.headline == (
        "Yesterday was a little off. Your sleep was shorter than usual, and "
        "you had more caffeine than your typical intake."
    )
    assert said.detail == "You slept 5h 52m after 3 cups of coffee."


@pytest.mark.parametrize(
    "facts",
    [
        YesterdayFacts(symptoms=("headache",), sleep_minutes=below(320, 450)),
        YesterdayFacts(sleep_minutes=below(352, 450), caffeine_cups=above(3, 1)),
        YesterdayFacts(sleep_minutes=below(352, 450), alcohol_ml=above(53, 8)),
        YesterdayFacts(sleep_minutes=below(352, 450), mood=below(3, 7)),
        YesterdayFacts(water_ml=below(600, 1900), mood=below(3, 7)),
        YesterdayFacts(spo2=below(93, 97)),
        YesterdayFacts(hrv=below(40, 60)),
    ],
)
def test_no_rung_ever_claims_a_cause(facts):
    """The whole point of rung 3, applied to every rung. One day cannot
    separate the orderings, so nothing may imply one."""
    said = summarised(facts)
    text = f"{said.headline} {said.detail}".lower()
    for word in ("because", "caused", "due to", "led to", "result of", "so you"):
        assert word not in text, f"said {word!r}: {text}"


def test_a_pairing_needs_both_halves_to_have_moved():
    """Caffeine is up but sleep was normal: no pair, so this falls to the
    lifestyle rung rather than inventing a night that was never short."""
    said = summarised(
        YesterdayFacts(sleep_minutes=usual(450), caffeine_cups=above(3, 1))
    )
    assert "sleep was shorter" not in said.headline
    assert "caffeine" in said.headline


# --------------------------------------------------------------------------- #
# Sleep
# --------------------------------------------------------------------------- #

def test_a_good_night_says_how_much_better_it_was():
    said = summarised(
        YesterdayFacts(sleep_minutes=above(485, 440), resting_heart_rate=usual(58))
    )
    assert said.headline == (
        "Yesterday was a good recovery day. You slept better than usual and "
        "your resting heart rate remained stable."
    )
    assert said.detail == (
        "You got 8h 5m of sleep, about 45 minutes above your recent average."
    )


def test_the_steady_heart_rate_clause_is_dropped_when_there_is_no_heart_rate():
    said = summarised(YesterdayFacts(sleep_minutes=above(485, 440)))
    assert "heart rate" not in said.headline


def test_a_heart_rate_with_no_baseline_is_not_called_stable():
    """USUAL only because there was nothing to compare against. Calling that
    "remained stable" is exactly the invented reassurance to avoid."""
    said = summarised(
        YesterdayFacts(
            sleep_minutes=above(485, 440),
            resting_heart_rate=DaySignal(58, None, Trend.USUAL),
        )
    )
    assert "remained stable" not in said.headline


# --------------------------------------------------------------------------- #
# Signals and baselines
# --------------------------------------------------------------------------- #

def test_a_baseline_needs_more_than_two_days_behind_it():
    assert baseline_of([400.0, 420.0]) is None
    assert baseline_of([400.0, 420.0, 440.0]) is not None


def test_blank_days_are_skipped_rather_than_averaged_in_as_zero():
    """Four logged days around 2 L and three untouched. Counting the blanks as
    zero would put the baseline near 1 L and make an ordinary day a triumph."""
    baseline = baseline_of([2000.0, None, 1900.0, 0.0, 2100.0, None, 2000.0])
    assert baseline is not None
    assert baseline > 1900


def test_a_change_under_the_floor_is_not_a_change():
    assert day_signal(8011, 8000, min_change=1500).trend is Trend.USUAL
    assert day_signal(9800, 8000, min_change=1500).trend is Trend.ABOVE


def test_no_baseline_means_no_opinion():
    signal = day_signal(8000, None, min_change=1500)
    assert signal.trend is Trend.USUAL
    assert signal.delta is None
    assert not signal.notable


# --------------------------------------------------------------------------- #
# Never inventing a figure
# --------------------------------------------------------------------------- #

def test_a_day_with_only_manual_logs_never_mentions_sleep_or_steps():
    said = summarised(
        YesterdayFacts(water_ml=below(600, 1900), mood=below(3, 7))
    )
    text = f"{said.headline} {said.detail}"
    assert "sleep" not in text
    assert "steps" not in text
    assert "0h 0m" not in text


@pytest.mark.parametrize(
    "facts",
    [
        YesterdayFacts(symptoms=("headache",)),
        YesterdayFacts(hrv=above(88, 60)),
        YesterdayFacts(sleep_minutes=below(352, 450), caffeine_cups=above(3, 1)),
        YesterdayFacts(sleep_minutes=above(485, 440)),
        YesterdayFacts(steps=above(14000, 8000)),
        YesterdayFacts(water_ml=below(600, 1900)),
        YesterdayFacts(steps=usual(8000)),
    ],
)
def test_every_rung_produces_two_finished_lines(facts):
    said = summarised(facts)
    assert said.headline.strip().endswith(".")
    assert said.detail.strip().endswith(".")
    # A sentence that trails off is a formatter that lost an argument.
    assert "  " not in said.detail
    assert "None" not in said.headline + said.detail


# --------------------------------------------------------------------------- #
# The two symptom sources
# --------------------------------------------------------------------------- #

DAY = date(2026, 9, 2)


#: The app's calendar zone. Fixtures are written in WALL-CLOCK time, because
#: that is how a reader experiences "yesterday evening" — and because writing
#: them as naive UTC is what made an earlier draft of this file assert the wrong
#: day. `_at(20)` is 8pm as the reader saw it, 14:30 UTC, inside the day. Stored
#: as UTC it would have been 01:30 the NEXT morning and left the window.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _at(hour: int) -> datetime:
    """`hour` o'clock on DAY in the reader's own zone, stored as UTC."""
    return datetime(2026, 9, 2, hour, tzinfo=IST).astimezone(UTC)


async def test_a_symptom_said_four_times_is_named_once(db_session):
    """`symptom_logs` is append-only, one row per mention. Without the
    de-duplication the card names the same headache four times."""
    for hour in (9, 11, 14, 20):
        db_session.add(SymptomLog(
            user_id=USER, symptom="headache", risk_level="none",
            created_at=_at(hour),
        ))
    await db_session.flush()

    assert await _symptoms_on(db_session, USER, DAY) == ("headache",)


async def test_ordinary_symptoms_are_not_filtered_out_by_severity(db_session):
    """`risk_level` is none/high/emergency, and MOST rows are `none` — an
    ordinary symptom deliberately does not raise the triage floor. Filtering on
    severity would leave this card empty for most readers on most days."""
    db_session.add(SymptomLog(
        user_id=USER, symptom="acidity", risk_level="none", created_at=_at(10),
    ))
    await db_session.flush()

    assert await _symptoms_on(db_session, USER, DAY) == ("acidity",)


async def test_severity_orders_the_list_without_excluding_anything(db_session):
    db_session.add(SymptomLog(
        user_id=USER, symptom="headache", risk_level="none", created_at=_at(20),
    ))
    db_session.add(SymptomLog(
        user_id=USER, symptom="chest pain", risk_level="emergency",
        created_at=_at(9),
    ))
    await db_session.flush()

    # Said EARLIER, and still first: severity outranks recency.
    assert await _symptoms_on(db_session, USER, DAY) == ("chest pain", "headache")


async def test_ticked_period_symptoms_are_read_and_humanised(db_session):
    """The other source entirely: codes ticked in cycle tracking, which never
    reach chat and so appear in no `symptom_logs` row."""
    db_session.add(PeriodDayLog(
        user_id=USER, log_date=DAY, symptoms=["lower_back_pain", "cramps"],
    ))
    await db_session.flush()

    found = await _symptoms_on(db_session, USER, DAY)
    assert set(found) == {"lower back pain", "cramps"}


async def test_a_spring_red_flag_outranks_an_ordinary_mention(db_session):
    db_session.add(SymptomLog(
        user_id=USER, symptom="bloating", risk_level="none", created_at=_at(9),
    ))
    db_session.add(PeriodDayLog(user_id=USER, log_date=DAY, symptoms=["flooding"]))
    await db_session.flush()

    assert (await _symptoms_on(db_session, USER, DAY))[0] == "flooding"


async def test_only_yesterdays_symptoms_are_read(db_session):
    db_session.add(SymptomLog(
        user_id=USER, symptom="headache", risk_level="none",
        created_at=datetime(2026, 8, 28, 9, tzinfo=UTC),
    ))
    db_session.add(PeriodDayLog(
        user_id=USER, log_date=DAY - timedelta(days=3), symptoms=["cramps"],
    ))
    await db_session.flush()

    assert await _symptoms_on(db_session, USER, DAY) == ()


async def test_another_readers_symptoms_are_never_read(db_session):
    other = uuid.UUID("aaaa1111-2222-3333-4444-555566667778")
    db_session.add(SymptomLog(
        user_id=other, symptom="headache", risk_level="none", created_at=_at(9),
    ))
    db_session.add(PeriodDayLog(user_id=other, log_date=DAY, symptoms=["cramps"]))
    await db_session.flush()

    assert await _symptoms_on(db_session, USER, DAY) == ()


async def test_the_two_sources_are_merged_on_one_spelling(db_session):
    """mhn-spring stores codes ("lower_back_pain"); chat stores the phrase the
    reader typed ("lower back pain"). One complaint logged both ways must be
    named once, not shown twice in two spellings."""
    db_session.add(SymptomLog(
        user_id=USER, symptom="lower back pain", risk_level="none",
        created_at=_at(9),
    ))
    db_session.add(PeriodDayLog(
        user_id=USER, log_date=DAY, symptoms=["lower_back_pain"],
    ))
    await db_session.flush()

    assert await _symptoms_on(db_session, USER, DAY) == ("lower back pain",)


async def test_a_range_read_spans_its_whole_window(db_session):
    """The sibling the chat summary reads through. Same de-duplication, wider
    window — a symptom said on three days in the range is still named once."""
    from app.patterns.service import symptoms_between

    for offset in (0, 1, 2):
        db_session.add(SymptomLog(
            user_id=USER, symptom="headache", risk_level="none",
            created_at=_at(9) - timedelta(days=offset),
        ))
    db_session.add(SymptomLog(
        user_id=USER, symptom="nausea", risk_level="none",
        created_at=_at(9) - timedelta(days=10),
    ))
    await db_session.flush()

    found = await symptoms_between(db_session, USER, DAY - timedelta(days=3), DAY)
    assert found == ("headache",)


async def test_a_symptom_logged_late_evening_lands_on_the_right_day(db_session):
    """The trap: `symptom_logs.created_at` is a UTC timestamp, while the reader's
    calendar days are cut at +05:30, which is always AHEAD.

    23:00 IST on 2 Sep is 17:30 UTC on 2 Sep, so this one happens to agree. The
    one that does NOT is below: 00:30 IST on 3 Sep is 19:00 UTC on 2 Sep, which
    `date(created_at)` would file under the 2nd — a day the reader had already
    finished. Filtering on the UTC calendar date reads the wrong day for five
    and a half hours every evening.
    """
    from app.patterns.service import _reader_day_bounds

    # 00:30 IST on the 3rd — belongs to the 3rd, carries a UTC date of the 2nd.
    db_session.add(SymptomLog(
        user_id=USER, symptom="cough", risk_level="none",
        created_at=datetime(2026, 9, 3, 0, 30, tzinfo=IST).astimezone(UTC),
    ))
    await db_session.flush()

    # Asked about the 2nd: it must NOT appear, however its UTC date reads.
    assert await _symptoms_on(db_session, USER, DAY) == ()
    # Asked about the 3rd: there it is.
    assert await _symptoms_on(db_session, USER, DAY + timedelta(days=1)) == ("cough",)

    # And the bounds themselves are the zone's, not UTC's midnight.
    start, end = _reader_day_bounds(DAY)
    assert start == datetime(2026, 9, 1, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 18, 30, tzinfo=UTC)

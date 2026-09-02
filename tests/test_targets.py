"""Targets the reader set for themselves were invisible to the chat.

`lifestyle_limit`, `body_measurement_goal` and `sahha_goal` are three tables
with one shape, all written by the app, and NOTHING in Davi read any of them.
A reader who had set a daily water target and a weight goal in the app, and
then asked about them here, was told there was nothing on record.

They are histories, not settings — a row per change — so "my goal" is the
newest row whose `effective_from` has arrived, not simply the newest row.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.coredata.service import target_phrase, targets
from app.models.common import utcnow
from app.models.coredata import BodyMeasurementGoal, LifestyleLimit, SahhaGoal

USER = uuid.UUID("77778888-9999-aaaa-bbbb-ccccdddd0001")
OTHER = uuid.UUID("77778888-9999-aaaa-bbbb-ccccdddd0002")
TODAY = utcnow().date()


async def test_all_three_tables_are_read(db_session):
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY - timedelta(days=10),
        limit_value=2, unit="cup"))
    db_session.add(BodyMeasurementGoal(
        user_id=USER, type="weight", effective_from=TODAY - timedelta(days=30),
        goal_value=72.5, direction="lose", unit="kg"))
    db_session.add(SahhaGoal(
        user_id=USER, metric="steps", effective_from=TODAY - timedelta(days=5),
        goal_value=8000, unit="steps"))
    await db_session.flush()

    got = await targets(db_session, USER)
    assert {t.metric for t in got} == {"coffee", "weight", "steps"}
    assert {t.kind for t in got} == {"limit", "goal"}


async def test_a_goal_dated_in_the_future_is_not_todays_goal(db_session):
    """A plan is not a target. Taking the newest row would report a limit the
    reader deliberately scheduled to start next week as one in force now."""
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY - timedelta(days=10),
        limit_value=3, unit="cup"))
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY + timedelta(days=7),
        limit_value=1, unit="cup"))
    await db_session.flush()

    got = await targets(db_session, USER)
    assert len(got) == 1
    assert got[0].value == 3


async def test_the_newest_effective_row_wins(db_session):
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY - timedelta(days=90),
        limit_value=5, unit="cup"))
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY - timedelta(days=2),
        limit_value=2, unit="cup"))
    await db_session.flush()

    got = await targets(db_session, USER)
    assert [t.value for t in got] == [2]


async def test_targets_are_owner_scoped(db_session):
    db_session.add(LifestyleLimit(
        user_id=OTHER, metric="coffee", effective_from=TODAY, limit_value=2,
        unit="cup"))
    await db_session.flush()
    assert await targets(db_session, USER) == []


async def test_a_limit_reads_as_a_ceiling_and_a_goal_as_a_target(db_session):
    """The wording has to keep the two apart: a coffee limit is a ceiling, a
    weight goal is something aimed at, and stating either as the other is
    wrong about what the reader chose."""
    db_session.add(LifestyleLimit(
        user_id=USER, metric="coffee", effective_from=TODAY, limit_value=2,
        unit="cup"))
    db_session.add(BodyMeasurementGoal(
        user_id=USER, type="weight", effective_from=TODAY, goal_value=72.5,
        direction="lose", unit="kg"))
    await db_session.flush()

    said = {t.metric: target_phrase(t) for t in await targets(db_session, USER)}
    assert said["coffee"] == "coffee no more than 2 cup a day"
    assert said["weight"] == "weight lose to 72.5 kg"


async def test_a_row_with_no_value_is_skipped(db_session):
    db_session.add(SahhaGoal(
        user_id=USER, metric="steps", effective_from=TODAY, goal_value=None))
    await db_session.flush()
    assert await targets(db_session, USER) == []

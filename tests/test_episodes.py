"""Symptom episodes — so "still not better" means something.

ActiveSymptomState existed in the models from the start and nothing ever wrote
to it. These tests pin the behaviour that closes that gap, and the safety rule
that comes with it: severity within an episode only ever goes UP, mirroring the
triage floor's own rule that downstream may raise a level but never lower it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.chat.episodes import (
    STALE_AFTER,
    Episode,
    open_episodes,
    open_or_touch,
    purge_stale,
    render_for_prompt,
    resolve,
    worst_level,
)
from app.models.chat import ActiveSymptomState
from app.models.common import utcnow
from app.triage.red_flags import EMERGENCY, HIGH, NONE


# --------------------------------------------------------------------------- #
# Opening and touching
# --------------------------------------------------------------------------- #
async def test_a_new_symptom_opens_an_episode(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "fever", NONE)
    episodes = await open_episodes(db_session, user_id)
    assert [e.symptom for e in episodes] == ["fever"]


async def test_mentioning_it_again_touches_rather_than_duplicates(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "fever", NONE)
    await open_or_touch(db_session, user_id, "fever", NONE)

    rows = (
        (
            await db_session.execute(
                select(ActiveSymptomState).where(
                    ActiveSymptomState.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_severity_only_ever_rises_within_an_episode(db_session):
    """Mirrors the triage floor: downstream may raise a level, never lower it.
    A reader whose cough became severe must not be quietly downgraded because a
    later message sounded calmer."""
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "cough", NONE)
    await open_or_touch(db_session, user_id, "cough", HIGH)
    await open_or_touch(db_session, user_id, "cough", NONE)

    episodes = await open_episodes(db_session, user_id)
    assert episodes[0].risk_level == HIGH


async def test_symptoms_are_normalised_so_casing_is_not_a_new_episode(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "Fever", NONE)
    await open_or_touch(db_session, user_id, "  fever  ", NONE)
    assert len(await open_episodes(db_session, user_id)) == 1


async def test_an_empty_symptom_is_ignored(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "   ", NONE)
    assert await open_episodes(db_session, user_id) == []


async def test_episodes_do_not_leak_between_users(db_session):
    a, b = uuid.uuid4(), uuid.uuid4()
    await open_or_touch(db_session, a, "fever", NONE)
    assert await open_episodes(db_session, b) == []


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #
async def test_resolving_closes_the_episode(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "fever", NONE)
    assert await resolve(db_session, user_id, "fever")
    assert await open_episodes(db_session, user_id) == []


async def test_resolving_something_that_was_never_open_is_harmless(db_session):
    assert not await resolve(db_session, uuid.uuid4(), "fever")


# --------------------------------------------------------------------------- #
# Staleness
# --------------------------------------------------------------------------- #
async def test_a_stale_episode_is_not_recalled(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "old ache", NONE)

    row = (
        (
            await db_session.execute(
                select(ActiveSymptomState).where(
                    ActiveSymptomState.user_id == user_id
                )
            )
        )
        .scalars()
        .first()
    )
    row.last_seen_at = utcnow() - STALE_AFTER - timedelta(days=1)
    await db_session.flush()

    assert await open_episodes(db_session, user_id) == []


async def test_purge_removes_stale_rows(db_session):
    """Cleanup belongs in the nightly sweep, not in a chat turn."""
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "old ache", NONE)
    row = (
        (
            await db_session.execute(
                select(ActiveSymptomState).where(
                    ActiveSymptomState.user_id == user_id
                )
            )
        )
        .scalars()
        .first()
    )
    row.last_seen_at = utcnow() - STALE_AFTER - timedelta(days=1)
    await db_session.flush()

    assert await purge_stale(db_session) == 1


async def test_purge_leaves_fresh_rows_alone(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "current ache", NONE)
    assert await purge_stale(db_session) == 0
    assert len(await open_episodes(db_session, user_id)) == 1


# --------------------------------------------------------------------------- #
# Recall ordering and rendering
# --------------------------------------------------------------------------- #
async def test_the_most_recently_mentioned_comes_first(db_session):
    user_id = uuid.uuid4()
    await open_or_touch(db_session, user_id, "first", NONE)
    await open_or_touch(db_session, user_id, "second", NONE)
    episodes = await open_episodes(db_session, user_id)
    assert episodes[0].symptom == "second"


async def test_recall_is_capped(db_session):
    user_id = uuid.uuid4()
    for i in range(12):
        await open_or_touch(db_session, user_id, f"symptom {i}", NONE)
    assert len(await open_episodes(db_session, user_id, limit=3)) == 3


def test_nothing_renders_when_there_are_no_episodes():
    assert render_for_prompt([]) == ""


def test_the_render_frames_episodes_as_reported_not_diagnosed():
    now = utcnow()
    rendered = render_for_prompt(
        [Episode("fever", NONE, now - timedelta(days=3), now)]
    )
    assert "raised in earlier turns" in rendered
    assert "fever" in rendered
    assert "3 day(s) ago" in rendered
    # It must push toward escalation on duration, not reassurance.
    assert "get it looked at" in rendered


def test_the_render_names_the_severity_when_it_is_not_none():
    now = utcnow()
    rendered = render_for_prompt([Episode("chest pain", HIGH, now, now)])
    assert "high" in rendered


def test_worst_level_reports_the_highest_open_severity():
    now = utcnow()
    assert (
        worst_level(
            [
                Episode("a", NONE, now, now),
                Episode("b", HIGH, now, now),
                Episode("c", NONE, now, now),
            ]
        )
        == HIGH
    )
    assert worst_level([]) == NONE
    assert worst_level([Episode("x", EMERGENCY, now, now)]) == EMERGENCY

"""Cycle tracking: own data only, gated the way the owning team gated it.

mhn-spring's V5 made this the ONE class of data in the schema that is
default-private, and argued why in the DDL itself: the family model is
default-ALLOW, so shipping cycle data on it would have handed every accepted
connection — spouse, parent, sibling, in-law — visibility of contraception and
pregnancy status nobody opted into.

These tests hold Davi to that decision rather than to its own judgement.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.coredata.service import (
    cycle_snapshot,
    pregnancy_safety_flag,
    render_cycle,
)
from app.models.common import utcnow
from app.models.coredata import PeriodSettings, PeriodStatus, PeriodTracking

USER = uuid.UUID("00000000-0000-0000-0000-000000000c1e")


async def _enable(db, **overrides):
    db.add(
        PeriodSettings(
            user_id=USER,
            enabled=overrides.pop("enabled", True),
            private=True,
            share_with_doctor=False,
            predict_enabled=overrides.pop("predict_enabled", True),
            show_fertile_window=overrides.pop("show_fertile_window", False),
            **overrides,
        )
    )
    await db.flush()


async def _status(db, **fields):
    db.add(
        PeriodStatus(
            user_id=USER,
            effective_from=fields.pop("effective_from", date(2026, 1, 1)),
            **fields,
        )
    )
    await db.flush()


async def _cycle(db, days_ago: int, length: int | None = 28):
    db.add(
        PeriodTracking(
            user_id=USER,
            start_date=utcnow() - timedelta(days=days_ago),
            cycle_length=length,
        )
    )
    await db.flush()


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
async def test_no_settings_row_means_tracking_is_off(db_session):
    """Absent is off. A reader who never opened cycle tracking has none."""
    snapshot = await cycle_snapshot(db_session, USER)
    assert snapshot.tracking_enabled is False
    assert snapshot.has_anything is False
    assert render_cycle(snapshot) == ""


async def test_disabled_tracking_is_respected(db_session):
    """An explicit off is an answer. Reading past it surfaces what the reader
    switched off."""
    await _enable(db_session, enabled=False)
    await _cycle(db_session, days_ago=10)
    await _status(db_session, stage="premenopause")

    snapshot = await cycle_snapshot(db_session, USER)
    assert snapshot.tracking_enabled is False
    assert snapshot.last_period_start is None
    assert render_cycle(snapshot) == ""


async def test_another_users_cycle_is_never_read(db_session):
    await _enable(db_session)
    await _cycle(db_session, days_ago=5)

    snapshot = await cycle_snapshot(db_session, uuid.uuid4())
    assert snapshot.tracking_enabled is False


async def test_the_read_takes_no_viewer_so_family_access_is_impossible():
    """The privacy model is enforced by the signature, not by a check.

    A `viewer_id` parameter here would be one refactor away from a family
    member's cycle data reaching a prompt.
    """
    import inspect

    params = set(inspect.signature(cycle_snapshot).parameters)
    assert params == {"db", "user_id"}, (
        f"cycle_snapshot grew a parameter: {sorted(params)}"
    )


async def test_only_recorded_cycles_can_be_in_this_table(db_session):
    """A prediction is an estimate the app drew, not something that happened.

    This used to seed `PeriodTracking(is_predicted=True)` and assert the row
    was filtered out. That column exists in NO environment — not in the Flyway
    chain, not in production — so the test only ever passed against the sqlite
    schema the ORM built from its own (wrong) model, while the filter it was
    exercising raised UndefinedColumn in production and left the read empty.

    What is actually true: predictions are a SETTINGS-level concept
    (`period_settings.predict_enabled`, `period_status.predictions_suppressed`)
    and are never written as rows here, so every row is something that
    happened. The guarantee holds by construction rather than by a filter.
    """
    await _enable(db_session)
    await _cycle(db_session, days_ago=2)

    snapshot = await cycle_snapshot(db_session, USER)
    assert snapshot.last_period_start is not None
    assert snapshot.recent_cycles == 1

    mapped = {c.name for c in PeriodTracking.__table__.columns}
    assert "is_predicted" not in mapped, (
        "production has no such column; mapping it broke the cycle read"
    )


async def test_the_fertile_window_stays_off_unless_turned_on(db_session):
    """"A fertile window is an estimate, it is not contraception, and
    defaulting it on would put a claim in front of people who never asked for
    one." — mhn-spring V5."""
    await _enable(db_session, show_fertile_window=False)
    assert (await cycle_snapshot(db_session, USER)).may_show_fertile_window is False


async def test_the_fertile_window_needs_predictions_too(db_session):
    await _enable(db_session, show_fertile_window=True, predict_enabled=False)
    assert (await cycle_snapshot(db_session, USER)).may_show_fertile_window is False


# --------------------------------------------------------------------------- #
# What it reports
# --------------------------------------------------------------------------- #
async def test_the_latest_status_wins(db_session):
    """period_status is temporal — the current row is the newest effective."""
    await _enable(db_session)
    await _cycle(db_session, days_ago=10)
    await _status(db_session, effective_from=date(2026, 1, 1), pregnancy="not_pregnant")
    await _status(db_session, effective_from=date(2026, 6, 1), pregnancy="pregnant")

    snapshot = await cycle_snapshot(db_session, USER)
    assert snapshot.pregnancy == "pregnant"


async def test_it_reports_what_is_recorded(db_session):
    await _enable(db_session)
    for days, length in ((5, 29), (34, 28), (62, 30)):
        await _cycle(db_session, days_ago=days, length=length)

    text = render_cycle(await cycle_snapshot(db_session, USER))
    assert "last recorded period" in text
    assert "average length" in text
    assert "not a diagnosis" in text


async def test_it_never_predicts_a_next_period(db_session):
    """Prediction is the app's job — it has the model, and
    predictions_suppressed exists because there are states where predicting
    would be wrong."""
    await _enable(db_session)
    await _cycle(db_session, days_ago=3)

    text = render_cycle(await cycle_snapshot(db_session, USER))
    for claim in ("next period", "expected on", "due on", "fertile"):
        assert claim not in text.lower()


# --------------------------------------------------------------------------- #
# What travels in every prompt, and what does not
# --------------------------------------------------------------------------- #
def test_only_pregnancy_and_breastfeeding_travel():
    """These change what is safe to say about many medicines, so they follow
    the reader. Contraception, stage and PCOS do not — they are not needed to
    answer a headache question."""
    from app.coredata.service import CycleSnapshot

    pregnant = CycleSnapshot(tracking_enabled=True, pregnancy="pregnant")
    assert "pregnant" in pregnancy_safety_flag(pregnant).lower()

    nursing = CycleSnapshot(tracking_enabled=True, breastfeeding=True)
    assert "breastfeeding" in pregnancy_safety_flag(nursing).lower()


def test_contraception_never_travels_in_the_safety_flag():
    """The most sensitive field in the table must not be in every prompt."""
    from app.coredata.service import CycleSnapshot

    snapshot = CycleSnapshot(
        tracking_enabled=True,
        pregnancy="not_pregnant",
        stage="premenopause",
        diagnosed_pcos=True,
    )
    flag = pregnancy_safety_flag(snapshot)
    assert flag == ""


def test_an_ordinary_reader_gets_no_flag():
    from app.coredata.service import CycleSnapshot

    assert pregnancy_safety_flag(CycleSnapshot()) == ""
    assert pregnancy_safety_flag(
        CycleSnapshot(tracking_enabled=True, pregnancy="not_pregnant")
    ) == ""

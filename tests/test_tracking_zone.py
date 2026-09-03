"""The day-bucket anchor is configuration, not a constant.

mhn-spring stamps a calendar day at WRITE time on every rollup, resolved in its
own `app.tracking.zone`. Davi has to read those buckets in the SAME zone or it
asks for the wrong day for every hour the two disagree.

The whole class of bug that produced this module came from this repo assuming a
value for another service's property — first that it was IST (it was not: the
property is `${TRACKING_ZONE:}` and Spring warns on every boot that it fell
back to `Etc/UTC`), then that setting it in a dashboard meant the running
process had read it (it did not: the deployment was awaiting approval). So the
value lives in settings and can move when Spring's moves, without a code change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import get_settings
from app.models.common import tracking_day_bounds, tracking_today, tracking_zone


@pytest.fixture
def set_offset(monkeypatch):
    def _set(minutes: int) -> None:
        monkeypatch.setenv("TRACKING_ZONE_OFFSET_MINUTES", str(minutes))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def test_the_offset_comes_from_settings_not_a_constant(set_offset):
    """If this ever stops reading settings, the anchor silently freezes at
    whatever the last hardcoded guess was — which is how this started."""
    set_offset(330)
    assert tracking_zone().utcoffset(None) == timedelta(minutes=330)
    set_offset(0)
    assert tracking_zone().utcoffset(None) == timedelta(0)


def test_a_day_boundary_is_the_zones_midnight_not_utcs(set_offset):
    """+05:30 midnight is 18:30 UTC the previous day. Getting this backwards is
    what dropped a symptom ticked today for five and a half hours every
    evening."""
    set_offset(330)
    start, end = tracking_day_bounds(date(2026, 9, 3))
    assert start == datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 9, 3, 18, 30, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_a_zero_offset_is_the_utc_day(set_offset):
    """The value the deployed service is ACTUALLY resolving today, until the
    pending deployment is approved. It must be expressible, not just IST."""
    set_offset(0)
    start, end = tracking_day_bounds(date(2026, 9, 3))
    assert start == datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    assert tracking_today() == datetime.now(UTC).date()


def test_a_negative_offset_works_too(set_offset):
    """Nothing here may assume the zone is east of UTC. A deployment in the
    Americas is a different sign, not a different magnitude."""
    set_offset(-300)
    start, _ = tracking_day_bounds(date(2026, 9, 3))
    assert start == datetime(2026, 9, 3, 5, 0, tzinfo=UTC)

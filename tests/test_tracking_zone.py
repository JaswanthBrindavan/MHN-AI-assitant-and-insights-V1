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


# --------------------------------------------------------------------------- #
# Which readers use the anchor, and which deliberately do not
# --------------------------------------------------------------------------- #
def test_calendar_window_follows_the_configured_zone(set_offset):
    """`calendar_window` reads Spring-resolved day buckets, so it must reckon
    the day the way Spring does. It anchored on UTC until `TRACKING_ZONE` was
    pinned and live — correctly, because the property was unset and Spring was
    falling back to Etc/UTC."""
    # A day where the two zones disagree: 18:45 UTC on the 2nd is 00:15 on the
    # 3rd at +05:30.
    from datetime import datetime as _dt

    from app.coredata.service import calendar_window

    at = _dt(2026, 9, 2, 18, 45, tzinfo=UTC)
    set_offset(330)
    ist_day = at.astimezone(tracking_zone()).date()
    set_offset(0)
    utc_day = at.astimezone(tracking_zone()).date()
    assert ist_day != utc_day, "fixture must straddle the boundary"

    set_offset(330)
    span = calendar_window("today", today=ist_day)
    assert span is not None, "'today' is a calendar period, not a rolling one"
    since, until = span
    assert since == ist_day and until == ist_day + timedelta(days=1)


def test_age_deliberately_does_not_follow_the_zone(set_offset):
    """A birthday is a fact about a person, not a bucket mhn-spring wrote.
    Moving it into this zone would add a dependency to shift an integer number
    of years by at most a day, so both age readers stay on UTC — and that is a
    decision, not an oversight."""
    import pathlib
    import re

    for path in ("app/health/reference.py", "app/chat/data_handlers.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r"today = utcnow\(\)\.date\(\)", src):
            window = src[max(0, m.start() - 400):m.start()]
            assert "dob" in window.lower() or "age" in window.lower(), (
                f"{path}: a non-age reader still anchors on the UTC date — "
                "day-bucketed reads must use tracking_today()"
            )

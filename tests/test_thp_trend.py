"""A lab parameter asked as a trend must come back as a trend.

Reported from the deployed app: "unable to pull the graphs for individual
THPs". `handle_report_param_ask` matched the parameter and then `return`ed on
the FIRST report it found, so a reader asking for a graph got a single reading
and no chart — every later report on file was read and thrown away.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.chat.data_handlers import handle_report_param_ask
from app.models.common import utcnow
from app.models.coredata import Report, UserThpSeries

USER = uuid.UUID("bbbbcccc-dddd-eeee-ffff-000011112222")


def _report(days_ago: int, value: float, flag: str = ""):
    return Report(
        user_id=USER,
        filepath=f"reports/labs-{days_ago}.pdf",
        private=False,
        created_at=utcnow() - timedelta(days=days_ago),
        content={
            "ai": {
                "extraction": {
                    "results": [
                        {
                            "test_name": "Ferritin",
                            "value_numeric": value,
                            "unit": "ng/mL",
                            "abnormal_flag": flag,
                        }
                    ]
                }
            }
        },
    )


async def _seed(db, values):
    for days_ago, v in values:
        db.add(_report(days_ago, v))
    await db.flush()


async def test_a_single_report_answers_without_a_chart(db_session):
    """One reading is not a trend, and a two-point line off one point is a
    drawing rather than a chart."""
    await _seed(db_session, [(3, 7.2)])
    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    assert "7.2" in out["reply"]
    assert out.get("visual") is None


async def test_several_reports_produce_a_chart(db_session):
    await _seed(db_session, [(90, 8.1), (60, 7.6), (30, 7.2), (3, 6.8)])
    out = await handle_report_param_ask(db_session, USER, "show my ferritin graph")
    assert out is not None
    visual = out["visual"]
    assert visual is not None, "the reader asked for a graph"
    assert len(visual["values"]) == 4
    assert visual["unit"] == "ng/mL"


async def test_the_chart_runs_oldest_to_newest(db_session):
    """A trend read right to left is not a trend."""
    await _seed(db_session, [(90, 8.1), (60, 7.6), (30, 7.2), (3, 6.8)])
    out = await handle_report_param_ask(db_session, USER, "my ferritin trend")
    assert out is not None
    assert out["visual"]["values"] == [8.1, 7.6, 7.2, 6.8]


async def test_the_reply_still_leads_with_the_latest_reading(db_session):
    await _seed(db_session, [(90, 8.1), (3, 6.8)])
    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    assert "6.8" in out["reply"], "the newest result is the answer"
    assert out["provenance"]["results"] == 2


async def test_the_change_is_stated_as_a_change_not_a_verdict(db_session):
    await _seed(db_session, [(90, 8.1), (3, 6.8)])
    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    low = out["reply"].lower()
    assert "gone from 8.1 to 6.8" in low
    # Never a judgement about the direction or what it means.
    for banned in ("improving", "getting worse", "well controlled", "good",
                   "you should", "keep it up", "under control"):
        assert banned not in low, banned


async def test_an_unmatched_parameter_returns_nothing(db_session):
    await _seed(db_session, [(3, 7.2)])
    assert await handle_report_param_ask(
        db_session, USER, "my vitamin b12"
    ) is None


def test_a_bare_my_parameter_is_deliberately_not_claimed():
    """"my ferritin" on its own is not supported, and that is a choice.

    A bare `my <word>` would claim "my father", "my reports" and "my
    medications" as lab parameters. It needs either a lead-in ("what is my
    ferritin") or a chart word ("my ferritin trend"), both of which say the
    reader means a value rather than a person or a document.
    """
    from app.chat.abilities import parse_report_param_ask

    assert parse_report_param_ask("my ferritin") is None
    assert parse_report_param_ask("what is my ferritin") == "ferritin"
    assert parse_report_param_ask("my ferritin trend") == "ferritin"


# --------------------------------------------------------------------------- #
# The series feed is the SAME source the mobile graphs use
# --------------------------------------------------------------------------- #
# Deriving lab history by walking `reports` disagreed with the app twice over:
# it saw only the newest 20 documents, and it grouped on the raw printed test
# name, so "HbA1c" and "HBA1C" were two parameters here and one there. Same
# biomarker, two different graphs. mhn-spring's V31 `user_thp_series` is the
# materialised feed `GET /files/biomarkers` returns, so read that instead —
# falling back to the per-document walk when the upstream ingester has not
# filled it.
def _series(readings, name="Ferritin", key="ferritin"):
    return UserThpSeries(
        user_id=USER, thp_key=key, name=name, unit="ng/mL",
        reference_range="30-400", readings=readings,
    )


def _reading(day: str, value: float, status: str = "normal"):
    return {"reportId": 1, "reportName": "Labs", "date": day, "value": value,
            "unit": "ng/mL", "referenceRange": "30-400", "status": status,
            "markerName": "Ferritin"}


async def test_the_series_answers_past_the_twenty_report_window(db_session):
    """25 readings: the per-document path caps at 20 documents, so a reader
    with a long history got a truncated graph that stopped mid-record."""
    days = [f"2024-{m:02d}-01" for m in range(1, 13)] + [
        f"2025-{m:02d}-01" for m in range(1, 13)
    ] + ["2026-01-01"]
    db_session.add(_series([_reading(d, 7.0 + i / 10) for i, d in enumerate(days)]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "show my ferritin graph")
    assert out is not None
    assert len(out["visual"]["values"]) == 25, "the series was truncated"
    assert out["provenance"]["results"] == 25


async def test_the_series_is_preferred_over_the_documents(db_session):
    """Both sources present. The feed wins, so the chat and the app agree."""
    await _seed(db_session, [(30, 1.1), (3, 2.2)])
    db_session.add(_series([_reading("2025-01-01", 9.9), _reading("2025-06-01", 8.8)]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "my ferritin trend")
    assert out is not None
    assert out["visual"]["values"] == [9.9, 8.8]
    assert "8.8" in out["reply"], "the newest series reading is the answer"


async def test_an_empty_feed_falls_back_to_the_documents(db_session):
    """The ingester is scheduled, so it can be behind or not yet run. An empty
    or absent series must not turn a working answer into 'no record'."""
    await _seed(db_session, [(30, 1.1), (3, 2.2)])
    db_session.add(_series([]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "my ferritin trend")
    assert out is not None
    assert out["visual"]["values"] == [1.1, 2.2]


async def test_one_readers_series_is_never_another_readers(db_session):
    """`thp_series` takes an id; owner scoping has to be in the WHERE clause."""
    other = uuid.UUID("99990000-1111-2222-3333-444455556666")
    row = _series([_reading("2025-01-01", 9.9)])
    row.user_id = other
    db_session.add(row)
    await db_session.flush()

    assert await handle_report_param_ask(
        db_session, USER, "my ferritin trend") is None


async def test_a_non_numeric_reading_is_dropped_not_guessed(db_session):
    """"Not detected" is a real lab line and not a point on a graph."""
    db_session.add(_series([
        _reading("2025-01-01", 7.0),
        {**_reading("2025-03-01", 0), "value": "not detected"},
        _reading("2025-06-01", 7.4),
    ]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "my ferritin trend")
    assert out is not None
    assert out["visual"]["values"] == [7.0, 7.4]


async def test_an_ideal_reading_is_not_reported_as_flagged(db_session):
    """The feed's zones are warning_low / ideal / warning_high / unknown, and
    `_is_abnormal_flag` calls anything it does not recognise abnormal. Without
    a translation an "ideal" result — a good one — came back as "flagged ideal
    against the printed reference range"."""
    db_session.add(_series([_reading("2025-06-01", 120.0, status="ideal")]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    assert "flagged" not in out["reply"]
    assert "within the printed reference range" in out["reply"]
    assert out["action"] == "review_with_clinician"


async def test_a_warning_zone_is_reported_as_a_side(db_session):
    db_session.add(_series([_reading("2025-06-01", 900.0, status="warning_high")]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    assert "flagged high" in out["reply"]
    assert out["action"] == "discuss_with_clinician"


async def test_an_unknown_zone_claims_nothing(db_session):
    """Their explicit "unknown" means they could not place the value. Saying
    either "within range" or "flagged" would be inventing a verdict."""
    db_session.add(_series([_reading("2025-06-01", 120.0, status="unknown")]))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, USER, "what is my ferritin")
    assert out is not None
    assert "flagged" not in out["reply"]
    assert "reference range" not in out["reply"]

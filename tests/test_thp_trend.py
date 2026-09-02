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
from app.models.coredata import Report

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

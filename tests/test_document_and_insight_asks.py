"""Asking for documents, their insights, and a parameter's graph.

Four reader-reported failures, each with its own root cause:

1. Document cards were dated by UPLOAD time, so a scan taken in March and
   uploaded last night listed as last night's — and sorted above a test
   actually taken this week.
2. "Insights from my full body checkup" resolved to whatever was newest,
   because the parser detected the ask and captured nothing.
3. A family member's insights could not be asked for at all: every path in
   the handler was scoped to the reader's own id.
4. "Show my HbA1c graph" answered with one number and no chart whenever the
   materialised series had ingested one report and not the rest — the
   per-document walk was skipped whenever the series returned anything.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.chat.abilities import parse_ai_result_query
from app.chat.data_handlers import (
    _resolve_named_document,
    handle_document_query,
    handle_report_param_ask,
)
from app.coredata.service import _ai_document_date, latest_documents
from app.models.common import utcnow
from app.models.coredata import (
    FamilyConnect,
    Relation,
    Report,
    ScanImaging,
    UserThpSeries,
)

READER = uuid.UUID("11111111-1111-1111-1111-111111111111")
MEMBER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _content(*, document_id: int, title: str, report_date: str | None):
    """The mhn-ai envelope, in the shape assembly.build_content writes."""
    extraction: dict = {"results": [], "patient_age": "45"}
    if report_date is not None:
        extraction["report_date"] = report_date
    return {
        "ai": {
            "schema_version": "2.1",
            "state": "complete",
            "document_id": document_id,
            "classification": {
                "section": "reports", "title": title, "confidence": 0.95,
            },
            "extraction": extraction,
            "insights": None,
        }
    }


# --------------------------------------------------------------------------- #
# 1. The date on the document, not the date it was uploaded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        # THE SHAPE PRODUCTION ACTUALLY HOLDS. mhn-ai writes a report's
        # `report_date` straight through from the model with no format asked
        # for, so it is whatever the lab printed. Read as ISO, this parsed as
        # nothing and every report fell back to its upload time — the exact bug
        # the doc_date field was added to fix, still present after the fix.
        ("02 Sep 2026", date(2026, 9, 2)),
        ("2026-09-02", date(2026, 9, 2)),
        ("18-Mar-2026", date(2026, 3, 18)),
        ("02/09/2026", date(2026, 9, 2)),
        ("28th July 2026", date(2026, 7, 28)),
        ("2026-09-02T17:44:50.475613+00:00", date(2026, 9, 2)),
        ("20260902", date(2026, 9, 2)),
        # Unreadable stays unreadable: the caller falls back to the upload time
        # rather than inventing a date.
        ("last Tuesday", None),
        ("", None),
        # Ambiguous by construction and deliberately refused: "%m/%d/%Y" is not
        # in the table, because it cannot be told from "%d/%m/%Y" for the first
        # twelve days of a month and guessing wrong misdates a document by up
        # to eleven days with nothing looking wrong.
        ("2026-13-45", None),
    ],
)
def test_a_printed_date_is_read_in_whatever_shape_it_was_printed(printed, expected):
    content = _content(document_id=1, title="Lab", report_date=printed)
    assert _ai_document_date("report", content) == expected


def test_the_printed_date_is_read_from_both_envelopes():
    """Reports carry ``extraction.report_date``; every other section carries
    its own field under ``section_extraction``."""
    report = _content(document_id=1, title="Full Body Checkup",
                      report_date="2026-03-04")
    assert _ai_document_date("report", report) == date(2026, 3, 4)

    scan = {"ai": {"section_extraction": {"scan_date": "2026-01-09"}}}
    assert _ai_document_date("scan", scan) == date(2026, 1, 9)

    # A document with no printed date is a real case, not a failure.
    assert _ai_document_date("report", _content(
        document_id=2, title="Untitled", report_date=None)) is None
    assert _ai_document_date("report", None) is None
    # A malformed date must not take out the listing it appears in.
    assert _ai_document_date("report", {
        "ai": {"extraction": {"report_date": "last Tuesday"}}}) is None


@pytest.mark.asyncio
async def test_documents_sort_by_when_they_were_taken(db_session):
    """The March scan uploaded last night is a March scan.

    Both rows are uploaded seconds apart, so upload order says nothing; only
    the printed dates distinguish them, and they run the other way.
    """
    now = utcnow()
    db_session.add(Report(
        user_id=READER, filepath="s3/old-upload.pdf", private=False,
        created_at=now - timedelta(seconds=30),
        content=_content(document_id=11, title="July Checkup",
                         report_date="2026-07-20"),
    ))
    db_session.add(Report(
        user_id=READER, filepath="s3/new-upload.pdf", private=False,
        created_at=now,
        content=_content(document_id=12, title="March Scan",
                         report_date="2026-03-04"),
    ))
    await db_session.flush()

    hits = await latest_documents(db_session, READER, ["report"])
    # Newest UPLOAD is the March document; newest DOCUMENT is the July one.
    assert [h.title for h in hits] == ["July Checkup", "March Scan"]
    assert hits[0].doc_date == date(2026, 7, 20)
    assert hits[0].when == date(2026, 7, 20)


@pytest.mark.asyncio
async def test_a_dateless_document_falls_back_to_its_upload(db_session):
    """It still lists, and it cannot displace a dated document."""
    now = utcnow()
    db_session.add(Report(
        user_id=READER, filepath="s3/undated.pdf", private=False,
        created_at=now,
        content=_content(document_id=13, title="Undated", report_date=None),
    ))
    await db_session.flush()

    hits = await latest_documents(db_session, READER, ["report"])
    assert hits[0].doc_date is None
    # Compared against the row's own timestamp rather than against `now`:
    # falling back to the upload is the whole claim, and sqlite hands back a
    # NAIVE datetime for a timestamptz column, so the two would differ by a
    # tzinfo that says nothing about the behaviour under test.
    assert hits[0].when == hits[0].created_at
    assert hits[0].created_at is not None


@pytest.mark.asyncio
async def test_document_cards_carry_the_printed_date(db_session):
    db_session.add(Report(
        user_id=READER, filepath="s3/card.pdf", private=False,
        created_at=utcnow(),
        content=_content(document_id=14, title="Full Body Checkup",
                         report_date="2026-03-04"),
    ))
    await db_session.flush()

    out = await handle_document_query(db_session, READER, "show my reports")
    assert out is not None
    card = out["documents"][0]
    assert card["date"].startswith("2026-03-04")
    # The card is openable: the id the client addresses Spring with.
    assert isinstance(card["id"], int)
    assert "04 Mar 2026" in out["reply"]


# --------------------------------------------------------------------------- #
# 2. Which document the reader named
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "handle", "latest"),
    [
        ("pull my insights from full body checkup doc", "full body checkup", False),
        ("pull my insights from the full body checkup doc", "full body checkup", False),
        ("need insights for latest uploaded doc", None, True),
        ("get insights for this report", None, False),
    ],
)
def test_the_insights_ask_says_which_document(message, handle, latest):
    query = parse_ai_result_query(message)
    assert query is not None, message
    assert query.handle == handle
    assert query.wants_latest is latest


def test_a_topic_is_not_a_document():
    """"Insights on diabetes" is a condition question. Reading it as a
    document ask answers about some unrelated file instead."""
    assert parse_ai_result_query("give me insights on diabetes") is None


def test_the_member_is_not_part_of_the_document_name():
    """"My father's full body checkup" names a person AND a document; leaving
    the person in searches document titles for "father"."""
    query = parse_ai_result_query("insights for my father's full body checkup")
    assert query is not None
    assert query.relation == "father"
    assert query.handle == "full body checkup"


@pytest.mark.asyncio
async def test_a_named_document_resolves_to_its_pipeline_id(db_session):
    db_session.add(Report(
        user_id=READER, filepath="s3/a.pdf", private=False, created_at=utcnow(),
        content=_content(document_id=9302, title="Full Body Checkup",
                         report_date="2026-07-20"),
    ))
    db_session.add(ScanImaging(
        user_id=READER, filepath="s3/b.pdf", private=False, created_at=utcnow(),
        content=_content(document_id=9303, title="Chest X-Ray",
                         report_date="2026-07-21"),
    ))
    await db_session.flush()

    query = parse_ai_result_query("insights from the full body checkup doc")
    resolved = await _resolve_named_document(db_session, READER, query)
    assert resolved == (9302, "Full Body Checkup")


@pytest.mark.asyncio
async def test_an_ambiguous_name_asks_rather_than_guesses(db_session):
    """Two documents match. Picking one reads the wrong report back as theirs."""
    for n, path in ((1, "s3/c1.pdf"), (2, "s3/c2.pdf")):
        db_session.add(Report(
            user_id=READER, filepath=path, private=False, created_at=utcnow(),
            content=_content(document_id=800 + n, title=f"Blood Panel {n}",
                             report_date=f"2026-0{n}-01"),
        ))
    await db_session.flush()

    query = parse_ai_result_query("insights from the blood panel report")
    resolved = await _resolve_named_document(db_session, READER, query)
    assert isinstance(resolved, dict)
    assert resolved["provenance"]["ambiguous"] is True
    assert "Blood Panel 1" in resolved["reply"]


@pytest.mark.asyncio
async def test_an_unknown_name_says_so(db_session):
    db_session.add(Report(
        user_id=READER, filepath="s3/d.pdf", private=False, created_at=utcnow(),
        content=_content(document_id=810, title="Full Body Checkup",
                         report_date="2026-07-20"),
    ))
    await db_session.flush()

    query = parse_ai_result_query("insights from the thyroid panel report")
    resolved = await _resolve_named_document(db_session, READER, query)
    assert isinstance(resolved, dict)
    assert "thyroid panel" in resolved["reply"]
    assert resolved["provenance"]["found"] == 0


# --------------------------------------------------------------------------- #
# 3. A family member's insights are as consent-bound as their documents
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_members_insights_need_the_read_grant(db_session):
    """No accepted link with the owner-side grant, no insights."""
    db_session.add(Report(
        user_id=MEMBER, filepath="s3/theirs.pdf", private=False,
        created_at=utcnow(),
        content=_content(document_id=900, title="Full Body Checkup",
                         report_date="2026-07-20"),
    ))
    await db_session.flush()

    query = parse_ai_result_query("insights for my father's latest report")
    resolved = await _resolve_named_document(db_session, READER, query)
    assert isinstance(resolved, dict)
    assert "family connection" in resolved["reply"]
    assert resolved["provenance"]["resolved"] is False


@pytest.mark.asyncio
async def test_a_shared_members_insights_resolve(db_session):
    # MEMBER sent the request and calls READER "Son"; from READER's side —
    # the acceptor's — the relation is the INVERSE, which is what the reader
    # says out loud when they ask about "my father".
    db_session.add(Relation(id=1, name="Son", inverse="Father"))
    db_session.add(FamilyConnect(
        requester_id=MEMBER, acceptor_id=READER, accepted=True,
        # The grant sits on the OWNER's side, and here the owner is the
        # requester, so it is req_read that shares their documents.
        req_read=True, acc_read=False, relation_id=1,
    ))
    db_session.add(Report(
        user_id=MEMBER, filepath="s3/shared.pdf", private=False,
        created_at=utcnow(),
        content=_content(document_id=901, title="Lipid Profile",
                         report_date="2026-07-20"),
    ))
    # Private documents stay private even inside a sharing connection.
    db_session.add(Report(
        user_id=MEMBER, filepath="s3/private.pdf", private=True,
        created_at=utcnow(),
        content=_content(document_id=902, title="Private Note",
                         report_date="2026-08-01"),
    ))
    await db_session.flush()

    query = parse_ai_result_query("insights for my father's latest report")
    resolved = await _resolve_named_document(db_session, READER, query)
    assert resolved == (901, "Lipid Profile")


# --------------------------------------------------------------------------- #
# 4. A parameter's graph
# --------------------------------------------------------------------------- #
def _series_reading(day: str, value: str):
    return {"reportId": None, "reportName": "Lab", "date": day,
            "value": value, "unit": "%", "status": "warning_high",
            "markerName": "HbA1c"}


@pytest.mark.asyncio
async def test_a_half_ingested_series_still_draws_a_graph(db_session):
    """The reported "cannot pull the individual THP graphs".

    The materialised series had folded in ONE of the reader's reports; the
    rest were still only in ``reports``. The walk was skipped whenever the
    series returned anything at all, so the answer was a single number and no
    chart — while the app, reading the same data differently, drew a line.
    """
    db_session.add(UserThpSeries(
        user_id=READER, thp_key="hba1c", name="HbA1c", unit="%",
        readings=[_series_reading("2026-07-20T00:00:00+00:00", "6.1")],
    ))
    db_session.add(Report(
        user_id=READER, filepath="s3/older.pdf", private=False,
        created_at=utcnow() - timedelta(days=200),
        content={
            "ai": {
                "document_id": 700,
                "classification": {"section": "reports", "title": "Older"},
                "extraction": {"results": [{
                    "test_name": "HbA1c", "value": "5.4", "unit": "%",
                    "value_numeric": 5.4, "abnormal_flag": "",
                }]},
            }
        },
    ))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, READER, "show my hba1c graph")
    assert out is not None
    assert out["visual"] is not None, "a second reading exists; it should plot"
    assert out["visual"]["values"] == [5.4, 6.1], "oldest first"
    assert out["provenance"]["results"] == 2


@pytest.mark.asyncio
async def test_one_reading_says_so_instead_of_dropping_the_chart(db_session):
    """Silently returning a number reads as the chart having failed."""
    db_session.add(UserThpSeries(
        user_id=READER, thp_key="ferritin", name="Ferritin", unit="ng/mL",
        readings=[{"date": "2026-07-20T00:00:00+00:00", "value": "45",
                   "unit": "ng/mL", "markerName": "Ferritin"}],
    ))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, READER, "my ferritin trend")
    assert out is not None
    assert out["visual"] is None
    assert "no trend to plot yet" in out["reply"]


@pytest.mark.asyncio
async def test_the_same_reading_is_not_plotted_twice(db_session):
    """The series and the walk can both hold the same report."""
    day = utcnow() - timedelta(days=3)
    db_session.add(UserThpSeries(
        user_id=READER, thp_key="ferritin", name="Ferritin", unit="ng/mL",
        readings=[{"date": day.date().isoformat(), "value": "45",
                   "unit": "ng/mL", "markerName": "Ferritin"}],
    ))
    db_session.add(Report(
        user_id=READER, filepath="s3/same.pdf", private=False, created_at=day,
        content={
            "ai": {
                "document_id": 701,
                "classification": {"section": "reports", "title": "Same day"},
                "extraction": {"results": [{
                    "test_name": "Ferritin", "value": "45", "unit": "ng/mL",
                    "value_numeric": 45.0, "abnormal_flag": "",
                }]},
            }
        },
    ))
    await db_session.flush()

    out = await handle_report_param_ask(db_session, READER, "my ferritin trend")
    assert out is not None
    assert out["provenance"]["results"] == 1, "one reading, counted once"
    assert out["visual"] is None

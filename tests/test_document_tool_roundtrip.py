"""The ``get_documents`` TOOL must not round-trip through English.

The agentic engine reaches documents through the ``get_documents`` tool. That
executor used to take the model's structured arguments, rebuild a sentence
("show me report") and hand it back to ``parse_document_query`` — which
requires an ownership marker (my/our/all/the/every). A synthesised phrase
carries none, so EVERY document tool call parsed to None and returned nothing.
On ``CHAT_ENGINE=agentic`` that is every document request a reader makes:
"show my latest lab reports" answered as though the records did not exist.

This is the repo's recurring bypass class — a handler whose reachability
depends on a parser succeeding — so these tests pin the executor's behaviour
rather than the parser's.
"""

from __future__ import annotations

import uuid

from app.chat.abilities import (
    ALL_DOCUMENT_KINDS,
    normalize_document_kinds,
    parse_document_query,
)
from app.chat.tools.executors import OUT_OF_BAND_DOCUMENTS, get_documents
from app.models.common import utcnow
from app.models.coredata import Report, ScanImaging

READER = uuid.UUID("cccccccc-cccc-cccc-cccc-ccccccccccc1")


async def _seed(db):
    now = utcnow()
    db.add(Report(user_id=READER, filepath="reports/lab.pdf",
                  private=False, created_at=now))
    db.add(ScanImaging(user_id=READER, filepath="scans/chest.jpg",
                       private=False, created_at=now))
    await db.flush()


# --------------------------------------------------------------------------- #
# The bug itself
# --------------------------------------------------------------------------- #
async def test_tool_call_for_reports_returns_the_reader_s_reports(db_session):
    """The exact call the model makes for "show my latest lab reports"."""
    await _seed(db_session)
    out = await get_documents(db_session, READER, {"kinds": ["report"]}, None)
    assert out is not None, "the document tool returned nothing"
    kinds = {d["kind"] for d in out[OUT_OF_BAND_DOCUMENTS]}
    assert kinds == {"report"}
    assert "lab.pdf" in out["deterministic_reply"]


async def test_tool_call_for_scans_returns_scans(db_session):
    await _seed(db_session)
    out = await get_documents(db_session, READER, {"kinds": ["scan"]}, None)
    assert out is not None
    kinds = {d["kind"] for d in out[OUT_OF_BAND_DOCUMENTS]}
    assert kinds == {"scan"}, "a scan request must not answer with reports"
    assert "chest.jpg" in out["deterministic_reply"]


async def test_tool_call_with_no_kinds_returns_every_kind(db_session):
    """Omitting `kinds` is documented as "all kinds", not "none"."""
    await _seed(db_session)
    out = await get_documents(db_session, READER, {}, None)
    assert out is not None
    assert {d["kind"] for d in out[OUT_OF_BAND_DOCUMENTS]} == {"report", "scan"}


# --------------------------------------------------------------------------- #
# Why the executor must not rebuild a sentence — pinned so it is not undone
# --------------------------------------------------------------------------- #
def test_a_rebuilt_phrase_still_does_not_parse():
    """The trap that caused the outage. If this ever starts passing, the
    ownership rule changed and the comment in executors.get_documents should
    be revisited — but the executor must STILL pass structured arguments."""
    for phrase in (
        "show me report",
        "show me scan",
        "show me document",
        "show me report scan",
        "show me father report",
    ):
        assert parse_document_query(phrase) is None, phrase


# --------------------------------------------------------------------------- #
# Kind normalisation reuses the message parser's own vocabulary
# --------------------------------------------------------------------------- #
def test_kinds_are_canonicalised_not_guessed():
    assert normalize_document_kinds(["report"]) == ("report",)
    assert normalize_document_kinds(["scan"]) == ("scan",)
    assert normalize_document_kinds(["lab_report"]) == ("report",)
    assert normalize_document_kinds(["blood test"]) == ("report",)
    assert normalize_document_kinds(["report", "scan"]) == ("report", "scan")
    assert normalize_document_kinds(["prescription"]) == ("prescription",)


def test_unrecognised_or_missing_kinds_over_answer_rather_than_return_nothing():
    for value in (None, [], ["nonsense_kind"], ["document"], "report"):
        assert normalize_document_kinds(value), value
    assert normalize_document_kinds(["nonsense_kind"]) == ALL_DOCUMENT_KINDS
    assert normalize_document_kinds(None) == ALL_DOCUMENT_KINDS

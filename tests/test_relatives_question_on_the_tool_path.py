"""A relative's question must not be answered from the reader's own rows.

Audit H7, part two. Part one guarded the deterministic parsers, which read the
reader's MESSAGE. This path never sees one: every executor rebuilds a
first-person sentence out of the model's structured arguments --
``get_latest_metric`` sends "what is my latest blood pressure" -- so by the
time a parser runs, "my mother's" is gone.

That made the fix engine-shaped without anyone intending it: the refusal held
on ``legacy`` and was bypassed entirely on ``agentic``, which is what
production runs.

Only ``latest_documents`` and ``can_view_document`` take a ``viewer_id`` in
``app/coredata/service.py``, so there is no consented read behind any of the
tools guarded here. Declining is the whole of what is available -- but the
decline resolves the relation first, because a family member's DOCUMENTS are
real and fetchable, and "I can pull up her reports" is a different answer from
"no".
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.chat.tools.registry import READER_ONLY_TOOLS, execute_tool
from app.llm.tools import ToolCall

USER = uuid.UUID("3df2d022-e21a-40c6-93c0-66c8ce0b7240")

#: One representative call per guarded tool, with arguments the model really
#: would send for a question about somebody else.
CALLS = {
    "get_latest_metric": {"metric": "blood_pressure"},
    "get_report_parameter": {"parameter": "hba1c"},
    "get_section_details": {"kind": "insurance"},
    "get_tracker_total": {"metric": "water", "period": "week"},
    "get_health_summary": {"period": "week"},
    # Added to READER_ONLY_TOOLS after the coverage invariant found them
    # unclassified — see tests/test_reader_scoped_reads_decline_family.py.
    "list_medications": {},
    "get_medication_adherence": {"name": "metformin"},
    "get_doctor_consults": {},
    "get_trends_and_patterns": {"focus": "trend", "metric": "sleep"},
}

RELATIVE_ASKS = [
    "what is my mother's blood pressure",
    "what is my father's hba1c",
    "how much water did my father drink this week",
    "what is her latest blood pressure",
    "show bhargava's insurance details",
]


def _payload(result) -> dict:
    return json.loads(result.content)


@pytest.mark.parametrize("tool", sorted(READER_ONLY_TOOLS))
@pytest.mark.parametrize("asked", RELATIVE_ASKS)
async def test_a_reader_only_tool_declines_a_question_about_someone_else(
    db_session, tool, asked
):
    result = await execute_tool(
        db_session,
        USER,
        ToolCall(id="c1", name=tool, arguments=CALLS[tool]),
        None,
        asked=asked,
    )
    body = _payload(result)
    assert body.get("found") is False, (tool, asked, body)
    # It must not read as "you have no data" -- that is a different wrong
    # answer, and the note the registry supplies for a None payload.
    assert "Nothing on file for that" not in body.get("note", "")
    assert "NOT a statement" in body["note"], body


@pytest.mark.parametrize("tool", sorted(READER_ONLY_TOOLS))
async def test_the_readers_own_question_is_untouched(db_session, tool):
    """The guard must only fire on somebody else. A first-person ask still runs."""
    result = await execute_tool(
        db_session,
        USER,
        ToolCall(id="c2", name=tool, arguments=CALLS[tool]),
        None,
        asked="what is my latest blood pressure",
    )
    body = _payload(result)
    assert "NOT a statement that anyone has no data" not in body.get("note", "")


async def test_a_family_ask_points_at_what_is_actually_available(db_session):
    """The decline names the subject and offers documents, not just 'no'.

    Documents are the one thing MHN shares with a connected member, so the
    honest decline is a redirect rather than a dead end.
    """
    result = await execute_tool(
        db_session,
        USER,
        ToolCall(id="c3", name="get_latest_metric",
                 arguments={"metric": "blood_pressure"}),
        None,
        asked="what is my mother's blood pressure",
    )
    body = _payload(result)
    assert body["about"] == "the reader's mother", body
    assert "get_documents" in body["note"] or "Family Connect" in body["note"], body


async def test_a_tool_that_can_represent_a_relative_is_not_guarded():
    """`get_documents` resolves the member under the sharing gate itself.

    Guarding it would break the one path that answers this question properly.
    """
    assert "get_documents" not in READER_ONLY_TOOLS
    assert "get_document_ai_result" not in READER_ONLY_TOOLS
    assert "get_family_members" not in READER_ONLY_TOOLS


# --------------------------------------------------------------------------
# Family history is not a change of subject
# --------------------------------------------------------------------------
HISTORY_ASKS = [
    # The one an existing agentic test caught: the reader's own HbA1c, with a
    # relative as the REASON for asking. Refusing this refuses them their own
    # data, and family history is the commonest way a relative appears at all.
    "my hba1c came back - should I worry given my father has diabetes?",
    "my father has diabetes, is my sugar ok",
    "my mother had breast cancer, what should i watch for",
    "my brother is diabetic - what is my latest hba1c",
]


@pytest.mark.parametrize("asked", HISTORY_ASKS)
async def test_family_history_still_reaches_the_readers_own_data(db_session, asked):
    result = await execute_tool(
        db_session,
        USER,
        ToolCall(id="c4", name="get_latest_metric",
                 arguments={"metric": "blood_pressure"}),
        None,
        asked=asked,
    )
    body = _payload(result)
    assert "NOT a statement that anyone has no data" not in body.get("note", ""), (
        asked, body
    )


async def test_history_plus_a_question_about_them_still_declines(db_session):
    """Mentioning history does not license reading the reader's rows FOR them."""
    result = await execute_tool(
        db_session,
        USER,
        ToolCall(id="c5", name="get_latest_metric",
                 arguments={"metric": "hba1c"}),
        None,
        asked="my father has diabetes, what is his hba1c",
    )
    assert _payload(result)["found"] is False

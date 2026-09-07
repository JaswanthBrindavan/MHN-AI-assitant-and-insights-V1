""""What health issues does my father have" is not a question about you.

Audit H7, found live in staging on 2026-09-07 after #72, #83 and #85 had all
shipped. The reply was:

    "Your records list: Short Term Memory Loss (controlled); Sugar (active).
     This is your own recorded data, not medical advice..."

— the READER's two conditions, offered as their father's, and explicitly
labelled as the reader's own data.

``_MY_CONDITIONS_RE`` matches the bare phrase "what health issues"; it never
required first-person framing. ``handle_about_me_query`` runs at step 3.49 of
the shared prologue, BEFORE the family handler at 3.50, so it claimed the turn
and the family read never ran.

Three near-identical asks behaved three different ways in the same session,
which is what makes the phrasing coverage worth pinning:

    "what conditions does my father have?"     -> his shared record (correct)
    "what health issues does my father have?"  -> the READER's record (wrong)
    "what medical conditions does Charan have?"-> his shared record (correct)
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.abilities import is_my_conditions_query, names_another_person
from app.chat.data_handlers import handle_about_me_query

USER = uuid.uuid4()

#: Phrasings that reach the reader-scoped matcher AND name somebody else.
#: Each was a wrong-person answer.
ABOUT_SOMEBODY_ELSE = [
    "what health issues does my father have?",
    "what health problems does my mother have",
    "what health conditions does my brother have",
    "what health issues does my cousin have",
]

#: The reader asking about themselves. These must keep working — the handler
#: exists because "what health do I have" was being composed by a model
#: instead of read off the record.
ABOUT_THE_READER = [
    "what health issues do I have?",
    "what conditions do i have",
    "what is my medical history",
    "who am i",
    "what am i diagnosed with",
    # Family HISTORY is still the reader's question (see names_another_person).
    "my father has diabetes - what health issues do I have?",
]


@pytest.mark.parametrize("message", ABOUT_SOMEBODY_ELSE)
async def test_a_relatives_conditions_are_not_read_off_the_readers_record(
    db_session, message
):
    """The handler declines, so the family handler at 3.50 can take the turn."""
    assert is_my_conditions_query(message), (
        f"{message!r} no longer reaches this matcher — if that is deliberate "
        "the guard below is still correct, but this test is no longer pinning "
        "what it was written for"
    )
    assert names_another_person(message)
    assert await handle_about_me_query(db_session, USER, message) is None


@pytest.mark.parametrize("message", ABOUT_THE_READER)
async def test_the_reader_still_gets_their_own_record(db_session, message):
    """A guard that eats the reader's own question would be the worse bug.

    The handler returns None here only because this test seeds nothing; what
    matters is that it is not the GUARD that declined.
    """
    assert not names_another_person(message), message


# --------------------------------------------------------------------------
# A summary is not a lab parameter
# --------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "what's my health summary of last week?",
    "what is my health summary for last week",
    "give me an overview of my health",
])
def test_a_summary_ask_does_not_masquerade_as_a_report_parameter(message):
    """Measured in production: the reader got the safe reply for a question
    with an exact answer on file.

    parse_report_param_ask matched the "my X of Y" shape and returned the
    parameter name "health summary of last week". The deterministic summary
    step stands down whenever another parser claims the turn, so it did -- and
    the question went to the model, which composed a summary containing a value
    that did not match the record. The numeric fidelity guard caught it and
    substituted the safe reply. Every layer behaved correctly; the routing was
    wrong.
    """
    from app.chat.abilities import parse_report_param_ask, parse_summary_query

    assert parse_report_param_ask(message) is None, message
    assert parse_summary_query(message) is not None, message


@pytest.mark.parametrize(("message", "expected"), [
    ("what is my hba1c", "hba1c"),
    ("show my cholesterol trend", "cholesterol"),
])
def test_a_real_parameter_ask_still_parses(message, expected):
    from app.chat.abilities import parse_report_param_ask

    assert parse_report_param_ask(message) == expected

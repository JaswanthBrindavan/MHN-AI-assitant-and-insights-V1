"""A question about a medicine must not be answered about something else.

`parse_correlation_query` is pure, so it declines on medication NOUNS ("my bp
tablet") and cannot see a bare brand or generic name. That gap had two live
consequences, both of which answered a question the reader did not ask:

    "does my metformin affect my sleep"
        -> parser returned None, the TRACKER slot claimed it, and the reader
           got "you have no sleep entries in the past 7 days"

    "does my metformin affect my sleep when i drink coffee"
        -> parsed as coffee-vs-sleep and answered about COFFEE, with the drug
           never mentioned

Closing it needs the medicine catalogue, so the check lives in the handler.
These tests pin the handler, not the parser — the parser is expected to keep
returning None for a bare drug name.
"""

from __future__ import annotations

import uuid

from app.chat.abilities import medication_candidates, parse_correlation_query
from app.chat.data_handlers import handle_correlation_query
from app.models.coredata import MedicineTracking

USER = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeee1")


async def _on_metformin(db):
    db.add(MedicineTracking(
        id=1, user_id=USER, name="Metformin", strength="500mg",
        private=False, is_prn=False,
    ))
    await db.flush()


# --------------------------------------------------------------------------- #
# The two live failures
# --------------------------------------------------------------------------- #
async def test_a_bare_drug_name_is_declined_not_answered_as_a_sleep_lookup(
    db_session,
):
    await _on_metformin(db_session)
    r = await handle_correlation_query(
        db_session, USER, "does my metformin affect my sleep"
    )
    assert r is not None, "fell through — the tracker slot would claim this"
    assert r["provenance"]["declined"] == "medication"
    assert r["action"] == "discuss_with_prescriber"
    assert "prescriber" in r["reply"]


async def test_a_drug_named_beside_a_habit_is_not_answered_about_the_habit(
    db_session,
):
    """The worse of the two: the subject was silently substituted."""
    await _on_metformin(db_session)
    r = await handle_correlation_query(
        db_session, USER,
        "does my metformin affect my sleep when i drink coffee",
    )
    assert r is not None
    assert r["provenance"]["declined"] == "medication", r["provenance"]
    assert "coffee" not in r["reply"].lower()


# --------------------------------------------------------------------------- #
# It must not turn ordinary questions into refusals
# --------------------------------------------------------------------------- #
async def test_a_habit_question_is_still_answered(db_session):
    await _on_metformin(db_session)
    r = await handle_correlation_query(
        db_session, USER, "does coffee affect my sleep"
    )
    # Either a real readout or the not-enough-days refusal — never the
    # medication decline.
    if r is not None:
        assert r["provenance"].get("declined") != "medication"


async def test_an_unrelated_message_is_not_claimed(db_session):
    await _on_metformin(db_session)
    assert await handle_correlation_query(
        db_session, USER, "what is diabetes"
    ) is None


# --------------------------------------------------------------------------- #
# Cost: the catalogue is only consulted for a first-person effect question
# --------------------------------------------------------------------------- #
def test_candidates_are_empty_unless_it_is_an_effect_question():
    """Empty candidates means zero database queries on that turn."""
    for quiet in (
        "how much water did i drink this week",
        "what is diabetes",
        "does coffee affect sleep",          # no first-person marker
        "summarise my health",
    ):
        assert medication_candidates(quiet) == (), quiet


def test_habit_vocabulary_is_never_a_candidate():
    """A catalogue lookup on "coffee" is a wasted query, and a match would be
    a refusal on an ordinary question."""
    for habit in (
        "does coffee affect my sleep",
        "does smoking affect my sleep",
        "is my hrv lower when i drink",
        "is my coffee the reason I sleep badly",
    ):
        assert medication_candidates(habit) == (), habit


def test_candidates_are_capped():
    """The cap is the query budget for this check."""
    noisy = (
        "does my metformin amlodipine atorvastatin ramipril bisoprolol "
        "affect my sleep"
    )
    assert len(medication_candidates(noisy)) <= 3


def test_the_parser_itself_still_cannot_see_a_drug_name():
    """Pins WHY the check is in the handler. If this ever starts passing, the
    parser learned drug names and the handler check may be redundant."""
    assert parse_correlation_query("does my metformin affect my sleep") is None

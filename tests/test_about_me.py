""""Who am I" must answer about the READER, not the assistant.

Reported from the deployed app: "who am i" came back with a description of the
assistant. It never matched `_IDENTITY_TERMS` — those are "who are YOU" — so no
deterministic handler claimed it, and the model filled the gap with the nearest
thing it knew about, which was itself.

"What health do I have" had the same shape: a question with an exact answer on
file, composed by a model instead of read from the record.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.chat.abilities import is_about_me_query, is_my_conditions_query
from app.chat.data_handlers import handle_about_me_query
from app.chat.router import is_identity_question
from app.models.core import User
from app.models.coredata import MedicalCondition

USER = uuid.UUID("aaaabbbb-cccc-dddd-eeee-ffff00001111")


async def _seed(db, *, with_conditions: bool = True):
    db.add(User(
        id=USER, name="Asha Rao", email="asha@example.com", user_name="asha",
        health_card_number="HC-ASHA", hashcode="x",
        dob=date(1985, 4, 2), gender="female",
    ))
    if with_conditions:
        db.add(MedicalCondition(
            user_id=USER, name="Type 2 diabetes", type="condition",
            status="active", private=False,
        ))
        db.add(MedicalCondition(
            user_id=USER, name="Hypertension", type="condition",
            status="active", private=False,
        ))
    await db.flush()


# --------------------------------------------------------------------------- #
# Routing: the reader's question is not the assistant's question
# --------------------------------------------------------------------------- #
def test_who_am_i_is_about_the_reader_not_the_assistant():
    for m in ("who am i", "who am I?", "tell me about myself",
              "what do you know about me"):
        assert is_about_me_query(m), m
        assert not is_identity_question(m), (
            f"{m!r} must not reach the assistant-identity reply"
        )


def test_who_are_you_still_reaches_the_assistant_reply():
    """The identity-privacy guard must be untouched: the model and provider
    are never disclosed, and that path is a different question."""
    for m in ("who are you", "what are you", "what can you do"):
        assert is_identity_question(m), m
        assert not is_about_me_query(m), m


def test_what_health_do_i_have_is_claimed():
    for m in ("what health do i have", "what conditions do i have",
              "what medical problems do i have", "what am i diagnosed with",
              "my medical history"):
        assert is_my_conditions_query(m), m


def test_it_does_not_claim_other_handlers_questions():
    for m in ("what documents do i have", "what reports do i have",
              "what medications do i have", "what is diabetes",
              "how much water this week"):
        assert not is_my_conditions_query(m), m
        assert not is_about_me_query(m), m


# --------------------------------------------------------------------------- #
# The answer comes from the record
# --------------------------------------------------------------------------- #
async def test_who_am_i_answers_from_the_record(db_session):
    await _seed(db_session)
    out = await handle_about_me_query(db_session, USER, "who am i")
    assert out is not None
    reply = out["reply"]
    assert "Asha Rao" in reply
    assert "Type 2 diabetes" in reply and "Hypertension" in reply
    assert out["provenance"]["path"] == "about_me"


async def test_what_health_do_i_have_lists_the_conditions(db_session):
    await _seed(db_session)
    out = await handle_about_me_query(
        db_session, USER, "what health do i have"
    )
    assert out is not None
    assert "Type 2 diabetes" in out["reply"]
    # A conditions question is not a profile question: no name needed.
    assert out["provenance"]["asked"] == "conditions"


async def test_an_empty_record_states_absence_of_a_RECORD(db_session):
    """"You have no conditions" is a claim about the reader's body made out of
    an empty table. "Nothing is on your record" is a claim about the table."""
    await _seed(db_session, with_conditions=False)
    out = await handle_about_me_query(db_session, USER, "what health do i have")
    assert out is not None
    low = out["reply"].lower()
    assert "no conditions on your record" in low
    assert "not a statement that you have none" in low


async def test_it_never_diagnoses_or_grades(db_session):
    await _seed(db_session)
    out = await handle_about_me_query(db_session, USER, "who am i")
    assert out is not None
    low = out["reply"].lower()
    for banned in ("you are diabetic", "you suffer from", "your condition is "
                   "serious", "you should", "this means you"):
        assert banned not in low, banned
    # Records framing, not assertion.
    assert "your records list" in low


async def test_an_unrelated_message_is_not_claimed(db_session):
    await _seed(db_session)
    assert await handle_about_me_query(
        db_session, USER, "what is diabetes"
    ) is None

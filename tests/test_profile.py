"""The user profile — consent-gated, viewable, erasable.

Three properties this file exists to pin:
  * nothing is stored without a recorded grant (fail CLOSED)
  * whatever is stored can be shown back and erased in one call
  * self-reported context is framed as such to the model, never as a diagnosis
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.profile import (
    PERSONALIZATION_PURPOSE,
    ProfileView,
    forget_everything,
    get_profile,
    grant_personalization,
    has_personalization_consent,
    render_for_prompt,
    revoke_personalization,
    update_profile,
)
from app.models.core import ConsentLedger


# --------------------------------------------------------------------------- #
# Consent gate
# --------------------------------------------------------------------------- #
async def test_a_fresh_user_has_no_consent_and_no_profile(db_session):
    view = await get_profile(db_session, uuid.uuid4())
    assert not view.has_consent
    assert view.is_empty


async def test_writing_without_consent_is_refused(db_session):
    """Fail CLOSED. Storing personal health details without a recorded grant
    is the one failure here that cannot be walked back."""
    with pytest.raises(PermissionError):
        await update_profile(
            db_session, uuid.uuid4(), {"chronic_conditions": ["asthma"]}
        )


async def test_granting_consent_is_idempotent(db_session):
    user_id = uuid.uuid4()
    first = await grant_personalization(db_session, user_id)
    second = await grant_personalization(db_session, user_id)
    assert first.id == second.id

    rows = (
        (
            await db_session.execute(
                select(ConsentLedger).where(
                    ConsentLedger.user_id == user_id,
                    ConsentLedger.purpose == PERSONALIZATION_PURPOSE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_revocation_is_appended_not_overwritten(db_session):
    """The ledger is append-only — a revocation is a new row, and the newest
    event wins."""
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    await revoke_personalization(db_session, user_id)

    rows = (
        (
            await db_session.execute(
                select(ConsentLedger)
                .where(ConsentLedger.user_id == user_id)
                .order_by(ConsentLedger.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [r.action for r in rows] == ["granted", "revoked"]
    assert not await has_personalization_consent(db_session, user_id)


async def test_revocation_also_erases_the_stored_data(db_session):
    """Recording a revocation while keeping the rows would be consent theatre."""
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    await update_profile(db_session, user_id, {"chronic_conditions": ["asthma"]})

    await revoke_personalization(db_session, user_id)

    view = await get_profile(db_session, user_id)
    assert view.is_empty


# --------------------------------------------------------------------------- #
# Storing and reading
# --------------------------------------------------------------------------- #
async def test_a_profile_round_trips(db_session):
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await update_profile(
        db_session,
        user_id,
        {
            "age_band": "30_44",
            "sex": "female",
            "chronic_conditions": ["asthma"],
            "current_medications": ["salbutamol inhaler"],
            "communication_style": "plain",
        },
    )
    assert view.data["age_band"] == "30_44"
    assert view.data["chronic_conditions"] == ["asthma"]

    reread = await get_profile(db_session, user_id)
    assert reread.data["current_medications"] == ["salbutamol inhaler"]


async def test_unknown_fields_are_ignored_not_stored(db_session):
    """An unknown key is a bug or an attack; neither should become a record."""
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await update_profile(
        db_session, user_id, {"age_band": "45_59", "secret_note": "anything"}
    )
    assert "secret_note" not in view.data


async def test_list_fields_are_capped(db_session):
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await update_profile(
        db_session, user_id, {"allergies": [f"allergen {i}" for i in range(50)]}
    )
    assert len(view.data["allergies"]) <= 20


async def test_a_partial_update_leaves_other_fields_alone(db_session):
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    await update_profile(db_session, user_id, {"age_band": "60_74"})
    view = await update_profile(db_session, user_id, {"sex": "male"})
    assert view.data["age_band"] == "60_74"
    assert view.data["sex"] == "male"


# --------------------------------------------------------------------------- #
# Erasure
# --------------------------------------------------------------------------- #
async def test_forgetting_clears_the_profile_and_the_topic_memory(db_session):
    """One call, both stores — making a reader find two switches would be a
    dark pattern."""
    from app.chat.long_term import record_topics
    from app.models.chat import UserMemory

    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    await update_profile(db_session, user_id, {"allergies": ["penicillin"]})
    await record_topics(db_session, user_id, {"MC001": "Type 2 Diabetes"})

    deleted = await forget_everything(db_session, user_id)
    assert deleted["profile"] == 1
    assert deleted["memories"] >= 1

    assert (await get_profile(db_session, user_id)).is_empty
    remaining = (
        (
            await db_session.execute(
                select(UserMemory).where(UserMemory.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_the_consent_ledger_survives_erasure(db_session):
    """The ledger is append-only; the record that consent existed IS the audit
    trail and must outlive the data."""
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    await update_profile(db_session, user_id, {"age_band": "18_29"})
    await forget_everything(db_session, user_id)

    rows = (
        (
            await db_session.execute(
                select(ConsentLedger).where(ConsentLedger.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# How it reaches the model
# --------------------------------------------------------------------------- #
def test_nothing_is_rendered_without_consent():
    view = ProfileView(data={"age_band": "30_44"}, has_consent=False)
    assert render_for_prompt(view) == ""


async def test_the_render_frames_it_as_self_reported(db_session):
    """The model must not present self-reported context as an established
    diagnosis — the same framing the compacted-context block uses."""
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await update_profile(
        db_session, user_id, {"chronic_conditions": ["asthma"]}
    )
    rendered = render_for_prompt(view)
    assert "self-reported" in rendered.lower()
    assert "not a medical record" in rendered.lower()
    assert "asthma" in rendered


async def test_the_communication_style_reaches_the_prompt(db_session):
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await update_profile(
        db_session,
        user_id,
        {"communication_style": "plain", "chronic_conditions": ["asthma"]},
    )
    assert "skip the jargon" in render_for_prompt(view)


async def test_an_empty_profile_renders_nothing(db_session):
    user_id = uuid.uuid4()
    await grant_personalization(db_session, user_id)
    view = await get_profile(db_session, user_id)
    assert render_for_prompt(view) == ""

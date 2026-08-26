"""Deferred erasure: forget immediately, destroy later, and destroy it all.

Three properties, and each is the answer to a question somebody may one day
have to answer under oath:

1. **Does the assistant stop using the data when asked?** Immediately — before
   anything is destroyed. Otherwise "we have deleted your data" is false for
   the whole grace period.
2. **Does the deletion actually cover everything?** `forget_everything` covered
   three of eleven Davi-owned per-user tables. Episodes, insights, pedigree,
   sessions, messages, summaries and receipts all survived a "forget me".
3. **Can the window be used?** A grace period that cannot be cancelled within
   is just a delay.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.chat import erasure, memory_assembly
from app.chat.orchestrator import handle_chat
from app.chat.profile import grant_personalization, update_profile
from app.config import get_settings
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.chat import (
    ActiveSymptomState,
    ConversationMessage,
    ConversationSession,
    RagTurnReceipt,
    SymptomLog,
    UserMemory,
)
from app.models.common import utcnow
from app.models.core import PedigreeCondition, PedigreeMember
from app.models.erasure import ErasureRequest
from app.models.feedback import TurnFeedback
from app.models.profile import UserProfile
from app.models.rules import InsightArtifact

USER = uuid.UUID("00000000-0000-0000-0000-0000000e7a5e")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000ffff1")

# Every Davi-owned per-user table an erasure must clear. Messages are counted
# separately because they carry no user_id of their own — they hang off the
# session, and the cascade is what reaches them.
ERASABLE_BY_USER_ID = (
    UserProfile, PedigreeCondition, PedigreeMember, ActiveSymptomState,
    SymptomLog, UserMemory, TurnFeedback, RagTurnReceipt, InsightArtifact,
    ConversationSession,
)


async def _seed_everything(db, user_id=USER):
    """One row in every table an erasure is supposed to reach."""
    await grant_personalization(db, user_id)
    await update_profile(db, user_id, {"allergies": ["penicillin"]})

    member = PedigreeMember(user_id=user_id, slot="mother", vital_status="alive")
    db.add(member)
    await db.flush()
    db.add(
        PedigreeCondition(
            user_id=user_id, slot="mother", condition_code="T2DM",
            condition_display="type 2 diabetes", onset_band="55_59",
            certainty="confirmed", provenance="self_report", soft_deleted=False,
        )
    )
    db.add(ActiveSymptomState(
        user_id=user_id, symptom="headache", risk_level="none",
        last_seen_at=utcnow(),
    ))
    db.add(SymptomLog(user_id=user_id, symptom="headache", risk_level="none"))
    db.add(UserMemory(
        user_id=user_id, kind="condition_topic", mem_key="T2DM",
        value="type 2 diabetes", mention_count=1, last_seen_at=utcnow(),
    ))
    db.add(RagTurnReceipt(
        user_id=user_id, query_hash="a" * 64, model_name="fake",
        prompt_version="v1", grounding_mode="off", grounding_status="n/a",
    ))
    first = InsightArtifact(
        user_id=user_id, condition_code="T2DM", tier="elevated", title="t",
        body="b", template_key="k", template_version=1, pipeline_version=1,
        content_hash="c" * 64, status="superseded",
    )
    second = InsightArtifact(
        user_id=user_id, condition_code="T2DM", tier="elevated", title="t2",
        body="b2", template_key="k", template_version=1, pipeline_version=1,
        content_hash="d" * 64, status="active",
    )
    db.add_all([first, second])
    await db.flush()
    # The self-referencing FK that makes delete order matter.
    first.superseded_by = second.id

    session = ConversationSession(user_id=user_id)
    db.add(session)
    await db.flush()
    message = ConversationMessage(
        session_id=session.id, role="user", message="hello"
    )
    db.add(message)
    await db.flush()
    db.add(TurnFeedback(
        user_id=user_id, message_id=message.id, session_id=session.id,
        rating="down", comment="private complaint",
    ))
    await db.commit()


async def _counts(db, user_id=USER) -> dict[str, int]:
    out = {}
    # Messages have no user_id of their own — they hang off the session.
    out["conversation_messages"] = (
        await db.execute(
            select(func.count(ConversationMessage.id))
            .join(ConversationSession)
            .where(ConversationSession.user_id == user_id)
        )
    ).scalar() or 0
    for model in ERASABLE_BY_USER_ID:
        out[model.__tablename__] = (
            await db.execute(
                select(func.count(model.id)).where(model.user_id == user_id)
            )
        ).scalar() or 0
    return out


# --------------------------------------------------------------------------- #
# 1. The assistant stops USING the data immediately
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
async def test_a_pending_erasure_stops_the_memory_reaching_the_prompt(
    db_session, monkeypatch, engine_name
):
    """This is what makes a deferred erasure honest rather than a delay."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    await _seed_everything(db_session)

    captured: list[str] = []

    class Spy(FakeProvider):
        async def generate(self, *, system, user):
            captured.append(join_system(system))
            return "General information [GK]."

        async def generate_turn(self, *, system, messages, tools=()):
            from app.llm.tools import LLMTurn

            captured.append(join_system(system))
            return LLMTurn(text="General information [GK].")

    # Before: the profile is in the prompt.
    await handle_chat(db_session, USER, "why am I so tired?", Spy(), uuid.uuid4())
    assert "penicillin" in captured[0]

    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.commit()

    captured.clear()
    await handle_chat(db_session, USER, "why am I so tired?", Spy(), uuid.uuid4())
    assert captured, "the provider was not called"
    assert "penicillin" not in captured[0], (
        "the assistant is still using data the reader asked it to forget"
    )


@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
async def test_family_history_stops_reaching_the_prompt(
    db_session, monkeypatch, engine_name
):
    """The gap the allergy test above could not see.

    `is_pending` gated `memory_assembly` only. `build_patient_context` is a
    SEPARATE read path over `pedigree_conditions` and `insight_artifacts` — two
    of the eleven tables the erasure destroys — and it runs BEFORE that gate on
    both engines. So the turn after a "forget me" still carried the reader's
    family history into the prompt.

    The fixture above already seeded that pedigree; nothing asserted on it,
    which is exactly how this survived. The reader is told "Davi has stopped
    using your information already" in the API response itself, so this is a
    promise made to them, not only to a reader of the PR.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    await _seed_everything(db_session)

    captured: list[str] = []

    class Spy(FakeProvider):
        async def generate(self, *, system, user):
            captured.append(join_system(system))
            return "General information [GK]."

        async def generate_turn(self, *, system, messages, tools=()):
            from app.llm.tools import LLMTurn

            captured.append(join_system(system))
            return LLMTurn(text="General information [GK].")

    await handle_chat(db_session, USER, "why am I so tired?", Spy(), uuid.uuid4())
    assert captured, "the provider was not called"
    assert "type 2 diabetes" in captured[0], (
        "fixture problem: the family history never reached the prompt at all"
    )

    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.flush()

    captured.clear()
    await handle_chat(db_session, USER, "why am I so tired?", Spy(), uuid.uuid4())
    assert captured, "the provider was not called"
    assert "type 2 diabetes" not in captured[0], (
        "family history still reaches the prompt after a forget-me request"
    )


async def test_the_sweep_does_not_rebuild_a_document_for_a_pending_erasure(db_session):
    """The document is one of the eleven erasable tables.

    Rebuilding it nightly through the grace window re-derives a fresh copy of
    exactly what the reader asked to have deleted.
    """
    from app.memory import document as memory_document

    await _seed_everything(db_session, USER)
    assert await memory_document.refresh(db_session, USER) is not None

    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.flush()

    assert await memory_document.refresh(db_session, USER) is None


async def test_a_pending_erasure_stops_new_memory_being_written(db_session):
    """Nothing new is learned about someone who asked to be forgotten."""
    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.flush()

    await memory_assembly.record(
        db_session, USER, codes=["T2DM"], flags=["chest pain"], risk="high"
    )

    assert (await _counts(db_session))["user_memories"] == 0
    assert (await _counts(db_session))["active_symptom_states"] == 0


async def test_the_pending_check_fails_CLOSED(db_session, monkeypatch):
    """If the check itself breaks, withhold the memory.

    Wrongly remembering someone who asked to be forgotten is the worse of the
    two available mistakes.
    """
    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(erasure, "pending_request", _boom)
    assert await erasure.is_pending(db_session, USER) is True


# --------------------------------------------------------------------------- #
# 2. The deletion covers everything
# --------------------------------------------------------------------------- #
async def test_purge_clears_every_per_user_table(db_session):
    """`forget_everything` reached 3 of 11. This must reach all of them."""
    await _seed_everything(db_session)
    before = await _counts(db_session)
    assert all(v > 0 for v in before.values()), f"seed incomplete: {before}"

    await erasure.purge_user(db_session, USER)
    await db_session.commit()

    after = await _counts(db_session)
    survivors = {k: v for k, v in after.items() if v}
    assert not survivors, f"survived a full erasure: {survivors}"


async def test_purge_does_not_touch_another_user(db_session):
    await _seed_everything(db_session, USER)
    await _seed_everything(db_session, OTHER)

    await erasure.purge_user(db_session, USER)
    await db_session.commit()

    assert all(v > 0 for v in (await _counts(db_session, OTHER)).values())


async def test_consent_ledger_survives_erasure(db_session):
    """It is the record that the erasure was authorised.

    Destroying it would destroy the evidence that consent was given and
    withdrawn — the opposite of an audit.
    """
    from app.models.core import ConsentLedger

    await _seed_everything(db_session)
    await erasure.purge_user(db_session, USER)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(ConsentLedger).where(ConsentLedger.user_id == USER)
        )
    ).scalars().all()
    assert rows, "the consent record was destroyed with the data"


# --------------------------------------------------------------------------- #
# 3. The window works
# --------------------------------------------------------------------------- #
async def test_nothing_is_destroyed_before_the_grace_period_expires(db_session):
    await _seed_everything(db_session)
    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.commit()

    result = await erasure.execute_due(db_session)
    assert result["erasures_executed"] == 0

    after = await _counts(db_session)
    assert all(v > 0 for v in after.values()), "data destroyed during the grace period"


async def test_the_erasure_runs_once_the_window_expires(db_session):
    await _seed_everything(db_session)
    request = await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.commit()

    result = await erasure.execute_due(
        db_session, now=request.scheduled_for + timedelta(seconds=1)
    )
    assert result["erasures_executed"] == 1

    after = await _counts(db_session)
    assert not {k: v for k, v in after.items() if v}


async def test_a_cancelled_request_is_never_executed(db_session):
    await _seed_everything(db_session)
    request = await erasure.request_erasure(db_session, USER, grace_days=30)
    await erasure.cancel_erasure(db_session, USER)
    await db_session.commit()

    result = await erasure.execute_due(
        db_session, now=request.scheduled_for + timedelta(days=365)
    )
    assert result["erasures_executed"] == 0
    assert all(v > 0 for v in (await _counts(db_session)).values())


async def test_the_deadline_is_fixed_at_request_time(db_session):
    """Changing the configured grace must not move a promise already made."""
    request = await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.flush()
    original = request.scheduled_for

    again = await erasure.request_erasure(db_session, USER, grace_days=365)
    assert again.scheduled_for == original


async def test_the_outcome_is_recorded(db_session):
    """"We deleted your data" is a claim somebody may have to substantiate."""
    await _seed_everything(db_session)
    request = await erasure.request_erasure(db_session, USER, grace_days=1)
    await db_session.commit()

    await erasure.execute_due(
        db_session, now=request.scheduled_for + timedelta(seconds=1)
    )

    row = (
        await db_session.execute(
            select(ErasureRequest).where(ErasureRequest.user_id == USER)
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.deleted_counts
    assert row.deleted_counts["user_profiles"] == 1

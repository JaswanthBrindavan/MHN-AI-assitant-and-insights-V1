"""Both engines must use ALL of the per-user memory, not half each.

Before `app/chat/memory_assembly.py`, the five long-lived memory operations
were split across the two engine branches:

| Memory                          | legacy | agentic |
|---------------------------------|--------|---------|
| `user_profiles` (consent-gated) | never  | read    |
| open symptom episodes           | never  | read    |
| recording a symptom episode     | never  | written |
| discussed-topic recall          | read   | never   |
| recording discussed topics      | written| never   |

`CHAT_ENGINE` defaults to `legacy`, so in production the consent-gated profile
a reader filled in was never read into the prompt at all, and no symptom
episode was ever recorded. The whole suite passed both before and after the
fix — nothing covered it. These are the tests that would have.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.chat.profile import grant_personalization, update_profile
from app.config import get_settings
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.chat import ActiveSymptomState, UserMemory
from app.models.common import utcnow

USER = uuid.UUID("00000000-0000-0000-0000-00000000e0e0")
BOTH_ENGINES = pytest.mark.parametrize("engine_name", ["legacy", "agentic"])


class SystemSpy(FakeProvider):
    """Captures the system prompt actually sent."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.systems: list[str] = []

    async def generate(self, *, system, user: str) -> str:
        self.systems.append(join_system(system))
        return "General information about tiredness [GK]. See a clinician."

    async def generate_turn(self, *, system, messages, tools=()):
        from app.llm.tools import LLMTurn

        self.systems.append(join_system(system))
        return LLMTurn(text="General information about tiredness [GK].")


async def _seed_profile(db, user_id=USER):
    await grant_personalization(db, user_id)
    await update_profile(
        db,
        user_id,
        {
            "chronic_conditions": ["hypothyroidism"],
            "current_medications": ["levothyroxine 75mcg"],
        },
    )
    await db.flush()


# --------------------------------------------------------------------------- #
# Reads — every engine sees every store
# --------------------------------------------------------------------------- #
@BOTH_ENGINES
async def test_the_consent_gated_profile_reaches_the_prompt(
    db_session, monkeypatch, engine_name
):
    """On the DEFAULT engine this never happened — Task 14 shipped a store
    that production never read."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    await _seed_profile(db_session)

    spy = SystemSpy()
    await handle_chat(db_session, USER, "why am I so tired?", spy, uuid.uuid4())

    assert spy.systems, "the provider was never called"
    assert "levothyroxine" in spy.systems[0], (
        f"the {engine_name} engine did not put the reader's profile in the prompt"
    )


@BOTH_ENGINES
async def test_open_symptom_episodes_reach_the_prompt(
    db_session, monkeypatch, engine_name
):
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    db_session.add(
        ActiveSymptomState(
            user_id=USER,
            symptom="chest tightness",
            risk_level="none",
            last_seen_at=utcnow(),
        )
    )
    await db_session.flush()

    spy = SystemSpy()
    await handle_chat(db_session, USER, "why am I so tired?", spy, uuid.uuid4())

    assert "chest tightness" in spy.systems[0], (
        f"the {engine_name} engine did not surface an open episode"
    )


@BOTH_ENGINES
async def test_past_topics_reach_the_prompt(db_session, monkeypatch, engine_name):
    """The agentic engine never read long-term memory."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)
    db_session.add(
        UserMemory(
            user_id=USER,
            kind="condition_topic",
            mem_key="T2DM",
            value="type 2 diabetes",
            mention_count=3,
            last_seen_at=utcnow(),
        )
    )
    await db_session.flush()

    spy = SystemSpy()
    await handle_chat(db_session, USER, "why am I so tired?", spy, uuid.uuid4())

    assert "type 2 diabetes" in spy.systems[0], (
        f"the {engine_name} engine did not recall past topics"
    )


# --------------------------------------------------------------------------- #
# Writes — every engine records what it should
# --------------------------------------------------------------------------- #
@BOTH_ENGINES
async def test_a_red_flag_opens_a_symptom_episode(
    db_session, monkeypatch, engine_name
):
    """The legacy engine never recorded one, so its own recall was always empty."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    await handle_chat(
        db_session, USER, "I have blood in my stool", FakeProvider(),
        uuid.uuid4(),
    )

    rows = (
        await db_session.execute(
            select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
        )
    ).scalars().all()
    assert rows, f"the {engine_name} engine recorded no symptom episode"


@BOTH_ENGINES
async def test_an_episode_is_recorded_at_the_triage_floors_severity(
    db_session, monkeypatch, engine_name
):
    """Severity must come from the deterministic floor, never from the model.

    An episode that recorded whatever the reply implied would let a model talk
    a red flag down into a routine note.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    result = await handle_chat(
        db_session, USER, "I have blood in my stool", FakeProvider(),
        uuid.uuid4(),
    )

    rows = (
        await db_session.execute(
            select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
        )
    ).scalars().all()
    assert rows
    assert all(r.risk_level == result.risk_level for r in rows)


@BOTH_ENGINES
async def test_discussed_topics_are_recorded(db_session, monkeypatch, engine_name):
    """The agentic engine never wrote these, so long-term memory never grew."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    await handle_chat(
        db_session, USER, "what should I know about type 2 diabetes?",
        FakeProvider(), uuid.uuid4(),
    )

    rows = (
        await db_session.execute(
            select(UserMemory).where(
                UserMemory.user_id == USER, UserMemory.kind == "condition_topic"
            )
        )
    ).scalars().all()
    assert rows, f"the {engine_name} engine recorded no discussed topic"


# --------------------------------------------------------------------------- #
# Fail-open, independently
# --------------------------------------------------------------------------- #
async def test_one_failing_store_does_not_cost_the_others(db_session, monkeypatch):
    """A broken episode query must not also lose the reader their profile."""
    from app.chat import memory_assembly

    await _seed_profile(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("episodes table is unavailable")

    monkeypatch.setattr(memory_assembly, "open_episodes", _boom)

    memory = await memory_assembly.assemble(db_session, USER)
    assert "levothyroxine" in memory.profile_text
    assert memory.episode_text == ""


async def test_assembly_never_raises(db_session, monkeypatch):
    """Memory is enrichment. Nobody loses an answer because recall failed."""
    from app.chat import memory_assembly

    async def _boom(*a, **k):
        raise RuntimeError("down")

    for name in ("get_profile", "open_episodes", "recall"):
        monkeypatch.setattr(memory_assembly, name, _boom)

    memory = await memory_assembly.assemble(db_session, USER)
    assert memory.blocks() == []
    assert memory.append_to("existing [P]") == "existing [P]"


async def test_recording_never_raises(db_session, monkeypatch):
    from app.chat import memory_assembly

    async def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(memory_assembly, "record_topics", _boom)
    monkeypatch.setattr(memory_assembly, "open_or_touch", _boom)

    # Must not raise.
    await memory_assembly.record(
        db_session, USER, codes=["T2DM"], flags=["chest pain"], risk="high",
    )


async def test_a_recorded_topic_stores_a_readable_name_not_a_code(db_session):
    """`recall()` renders the stored value verbatim.

    Storing the raw code would have the assistant tell a reader "you
    previously asked about: MC001". Resolution lives in memory_assembly so no
    call site can get it wrong -- the agentic caller originally did.
    """
    from app.chat import memory_assembly
    from app.models.knowledge import ConditionRegistry

    db_session.add(
        ConditionRegistry(
            condition_code="MC001",
            display_name="Type 2 Diabetes Mellitus",
            aliases=["sugar disease"],
            engine_codes=["T2DM"],
            active=True,
        )
    )
    await db_session.flush()

    await memory_assembly.record(db_session, USER, codes=["MC001"])

    row = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.mem_key == "MC001")
        )
    ).scalars().first()
    assert row is not None
    assert row.value == "Type 2 Diabetes Mellitus", (
        f"stored {row.value!r} -- recall would read this back to the user"
    )


async def test_an_unknown_code_falls_back_to_the_code_itself(db_session):
    """Better a code than a crash: display names are not worth a failure."""
    from app.chat import memory_assembly

    await memory_assembly.record(db_session, USER, codes=["UNKNOWN_CODE"])
    row = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.mem_key == "UNKNOWN_CODE")
        )
    ).scalars().first()
    assert row is not None and row.value == "UNKNOWN_CODE"


@BOTH_ENGINES
async def test_an_emergency_is_recorded_before_the_path_exits(
    db_session, monkeypatch, engine_name
):
    """The most severe red flags used to be the ONLY ones that opened no episode.

    The emergency branch returns from the shared prologue before either engine
    reaches the normal recording step, so the top of the severity range the
    triage floor decides was never persisted. Recording now happens inside that
    branch, before it returns.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    result = await handle_chat(
        db_session, USER, "I can't breathe", FakeProvider(), uuid.uuid4()
    )
    assert result.risk_level == "emergency"

    rows = (
        await db_session.execute(
            select(ActiveSymptomState).where(ActiveSymptomState.user_id == USER)
        )
    ).scalars().all()
    assert rows, f"the {engine_name} engine did not record the emergency"
    assert all(r.risk_level == "emergency" for r in rows), (
        "an emergency must be recorded AT emergency severity"
    )


@BOTH_ENGINES
async def test_an_emergency_still_answers_deterministically(
    db_session, monkeypatch, engine_name
):
    """Recording must not delay, dilute or displace the directive.

    Emergency handling does NOT continue through the normal symptom-assessment
    flow: no retrieval, no model, no topics recorded — just the deterministic
    directive and the event on the record.
    """
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    result = await handle_chat(
        db_session, USER, "I can't breathe", FakeProvider(), uuid.uuid4()
    )

    assert result.risk_level == "emergency"
    assert result.recommended_action == "call_emergency_services"
    assert "emergency" in result.response_message.lower()
    assert result.provenance.get("path") != "symptom_rag"

    # No topics: retrieval never ran, and must not have.
    topics = (
        await db_session.execute(
            select(UserMemory).where(UserMemory.kind == "condition_topic")
        )
    ).scalars().all()
    assert topics == [], "the emergency path ran the normal assessment flow"


async def test_a_recording_failure_cannot_swallow_the_emergency_directive(
    db_session, monkeypatch
):
    """Fail-open, on the one path where it matters most.

    If remembering the event ever cost someone the directive telling them to
    call for help, that would be the worst bug in this codebase.
    """
    from app.chat import memory_assembly

    async def _boom(*a, **k):
        raise RuntimeError("episode table is down")

    monkeypatch.setattr(memory_assembly, "open_or_touch", _boom)

    result = await handle_chat(
        db_session, USER, "I can't breathe", FakeProvider(), uuid.uuid4()
    )
    assert result.risk_level == "emergency"
    assert "emergency" in result.response_message.lower()

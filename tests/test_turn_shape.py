"""A turn should cost what its question is worth.

Measured before this: "what is diabetes" — three words — built a 4,291-token
prompt of which 3,601 tokens could not contribute to the answer, and made TWO
sequential model calls, on the agentic engine. The legacy engine had answered
the same question from the validated profile with no model call since Phase 4;
the agentic engine could not reach that path, because `_dispatch` returns into
`_dispatch_agentic` ~230 lines above it. The agentic engine had re-implemented
the expensive half of the pipeline and skipped the free half.

These tests pin the SHAPE of a turn: which questions reach a model at all, and
what lands in retrieval scope.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.orchestrator import handle_chat
from app.llm.base import LLMProvider
from app.llm.tools import LLMTurn
from app.models.chat import McpChunk
from app.models.core import PedigreeCondition
from app.models.knowledge import ConditionRegistry
from app.triage.red_flags import NONE

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    """These pin the AGENTIC engine specifically — it is the one that could not
    reach the deterministic corpus path, and the one staging runs.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_engine", "agentic")


class CountingProvider(LLMProvider):
    """Answers trivially, and counts every model call.

    The point of these tests is the COUNT: a shape that should never reach a
    model must leave `calls` empty.
    """

    model_name = "counting"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, *, system, messages, **kw) -> str:
        self.calls.append("generate")
        return "A short answer."

    async def generate_turn(self, *, system, messages, tools=()) -> LLMTurn:
        self.calls.append("turn")
        return LLMTurn(text="A short answer.", tool_calls=())


async def _seed(db, *, with_pedigree: bool = True) -> uuid.UUID:
    user = uuid.uuid4()
    db.add(ConditionRegistry(
        condition_code="MC001", display_name="Diabetes mellitus",
        aliases=["diabetes", "sugar"], engine_codes=["T2DM"], active=True))
    db.add(ConditionRegistry(
        condition_code="MC051", display_name="Primary Hypertension",
        aliases=["hypertension", "high bp"], engine_codes=["HTN"], active=True))
    for section, body in (
        ("definition", "Diabetes mellitus is a chronic metabolic condition in "
                       "which blood glucose stays elevated over time."),
        ("symptoms", "Increased thirst, frequent urination, unexplained "
                     "fatigue and blurred vision are common early features."),
    ):
        db.add(McpChunk(
            condition_code="MC001", chunk_type=section,
            content=f"Diabetes mellitus — {section}:\n{body}"))
    db.add(McpChunk(
        condition_code="MC051", chunk_type="definition",
        content="Primary Hypertension — definition:\nPersistently raised "
                "arterial blood pressure over successive readings."))
    if with_pedigree:
        # The reader's FATHER has hypertension. The reader does not.
        db.add(PedigreeCondition(
            user_id=user, slot="father", condition_code="HTN",
            condition_display="Hypertension", onset_band="45_54",
            certainty="confirmed", provenance="user"))
    await db.flush()
    return user


# --------------------------------------------------------------------------
# Retrieval scope
# --------------------------------------------------------------------------

async def test_a_named_condition_does_not_drag_the_pedigree_into_scope(
    db_session,
):
    """The reader named their topic. Their family history is not the topic.

    Unioning it in gave "what is type 2 diabetes" a scope of
    {T2DM, MC001, HTN, MC051} — and `spread_across_conditions` then GUARANTEES
    the off-topic code a slot out of k=4, because it exists to stop one
    condition taking every slot. So a hypertension profile was retrieved, put
    in the prompt, and cited, on a question that said "diabetes".
    """
    user = await _seed(db_session)
    result = await handle_chat(
        db_session, user, "what is diabetes", CountingProvider()
    )
    scope = result.provenance.get("conditions", [])
    assert "MC001" in scope
    assert "MC051" not in scope, f"hypertension still in scope: {scope}"
    assert "HTN" not in scope, f"hypertension still in scope: {scope}"


async def test_a_question_naming_nothing_still_uses_the_pedigree(db_session):
    """The union is right when the reader named no condition — don't lose it."""
    user = await _seed(db_session)
    result = await handle_chat(
        db_session, user, "tell me more", CountingProvider()
    )
    scope = result.provenance.get("conditions", [])
    assert scope, "a question naming nothing must still reach the pedigree"


# --------------------------------------------------------------------------
# How many model calls a shape is worth
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "what is diabetes",
    "what are the symptoms of diabetes",
])
async def test_a_corpus_question_reaches_no_model_at_all(db_session, question):
    """The validated profile answers this verbatim. A model adds latency only."""
    user = await _seed(db_session)
    provider = CountingProvider()
    result = await handle_chat(db_session, user, question, provider)

    assert provider.calls == [], (
        f"{question!r} made {len(provider.calls)} model call(s); the profile "
        "already answers it"
    )
    assert result.provenance.get("mode") == "extractive"
    assert result.risk_level == NONE


async def test_a_personal_question_still_reaches_the_model(db_session):
    """The cut must not swallow questions the corpus cannot answer."""
    user = await _seed(db_session)
    provider = CountingProvider()
    await handle_chat(
        db_session, user, "what was my last blood sugar reading", provider
    )
    assert provider.calls, "a records question must still reach the model"


async def test_a_bare_follow_up_still_reaches_the_model(db_session):
    user = await _seed(db_session)
    provider = CountingProvider()
    await handle_chat(db_session, user, "tell me more", provider)
    assert provider.calls, "a follow-up must still reach the model"


async def test_the_extractive_answer_carries_its_citations(db_session):
    """Zero model calls must not mean zero provenance."""
    user = await _seed(db_session)
    result = await handle_chat(
        db_session, user, "what is diabetes", CountingProvider()
    )
    assert result.citations, "an extractive answer still cites its source"
    assert all(c["condition_code"] == "MC001" for c in result.citations)


async def test_the_corpus_shortcut_is_gated_on_the_section_being_present(
    db_session,
):
    """`_prefer_section` fails open, so chunks alone do not mean the corpus
    holds the asked-for section. Without the `is_focused` conjunct a
    definitional-SHAPED question whose answer is not a profile section would be
    served a mismatched section with no model left to notice.
    """
    user = uuid.uuid4()
    db_session.add(ConditionRegistry(
        condition_code="MC001", display_name="Diabetes mellitus",
        aliases=["diabetes"], engine_codes=["T2DM"], active=True))
    # Only a prevalence chunk: the profile cannot answer "what is".
    db_session.add(McpChunk(
        condition_code="MC001", chunk_type="prevalence",
        content="Diabetes mellitus — prevalence:\nAround one in ten adults "
                "worldwide is affected according to recent surveys."))
    await db_session.flush()

    provider = CountingProvider()
    await handle_chat(db_session, user, "what is diabetes", provider)
    assert provider.calls, (
        "the corpus lacks the definition section, so the model must still run"
    )

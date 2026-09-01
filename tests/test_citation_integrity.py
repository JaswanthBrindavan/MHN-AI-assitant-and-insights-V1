"""Cite only what the answer actually used.

Reported from the deployed app: "how much water did I drink today" came back
with four Master Condition Profile citations — MC369, MC131, MC044, MC568 —
none of which the answer had touched. The reply was composed entirely from the
reader's own lifestyle rows.

The defect was structural, not a missed `None`. Retrieval runs ABOVE the engine
branch, so `chunks` — what retrieval RETURNED — was in scope at every return
statement, and the agentic engine cited it regardless of what produced the
text. With an empty condition scope, retrieval falls into the unscoped global
fallback and `%water%` matches whatever the corpus happens to say about water.

The fix threads what the answer USED out of each path WITH the answer
(`ChatResult.used`), and builds citations in exactly one place, `handle_chat`.
A path that declares nothing cites nothing, so the guarantee survives a sixth
call site being added — which is what these tests actually pin.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.chat.orchestrator import handle_chat
from app.coredata.service import week_start
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall, ToolResultMessage
from app.models.chat import McpChunk
from app.models.common import utcnow
from app.models.coredata import LifestyleDailyTotal, SahhaDailyTotal
from app.models.knowledge import ConditionRegistry
from app.rag.extractive import rendered_chunks
from app.rag.retrieval import retrieve_chunks

USER = uuid.UUID("0c17a710-0000-0000-0000-00000000c17e")
TODAY = utcnow().date()
THIS_WEEK = week_start(TODAY)


# --------------------------------------------------------------------------- #
# The invariant every path must satisfy
# --------------------------------------------------------------------------- #
def corpus_citations(result) -> list[dict]:
    return [
        c for c in (result.citations or [])
        if c.get("source") == "mcp_master_profile"
    ]


def assert_provenance_agrees(result) -> None:
    """Provenance and citations are two views of ONE fact and cannot disagree.

    Both come out of the same `Used` in `_cite`, so a disagreement means
    something started building citations from a second slice again — the exact
    shape of the original bug.
    """
    assert "used_chunks" in result.provenance, (
        "every reply must state what it used, even if that is nothing"
    )
    declared = list(result.provenance["used_chunks"])
    cited = [c["chunk_id"] for c in corpus_citations(result)]
    assert cited == declared, f"citations {cited} != provenance {declared}"
    if not declared:
        assert not corpus_citations(result), (
            "citations claim corpus content the provenance does not"
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _seed_corpus(db) -> None:
    """A corpus that the UNSCOPED fallback will happily return for a question
    about water — which is how the reported citations were chosen."""
    db.add(ConditionRegistry(
        condition_code="MC001", display_name="Diabetes mellitus",
        aliases=["diabetes", "sugar"], engine_codes=["T2DM"], active=True))
    for section, body in (
        ("definition", "Diabetes mellitus is a chronic metabolic condition in "
                       "which blood glucose stays elevated over time."),
        ("symptoms", "Increased thirst, drinking more water than usual, "
                     "frequent urination and unexplained fatigue are common "
                     "early features."),
        # A suggestions chunk is a flattened table row; `format_suggestions`
        # renders nothing from any other shape.
        ("suggestions",
         "LHP: Food; Suggestion: Keep to regular meals and choose whole "
         "grains over refined ones\n"
         "LHP: Water; Suggestion: Drink water rather than sweetened "
         "drinks through the day"),
    ):
        db.add(McpChunk(
            condition_code="MC001", chunk_type=section,
            content=f"Diabetes mellitus — {section}:\n{body}"))
    for i in range(1, 5):
        db.add(McpChunk(
            condition_code=f"MC90{i}", chunk_type="suggestions",
            content=f"Condition {i} — suggestions:\nDrink water through the "
                    "day and keep a record of what you drink today."))


def _seed_water(db) -> None:
    db.add(LifestyleDailyTotal(
        user_id=USER, metric="water", bucket_start=THIS_WEEK,
        total=2000.0, entries=1, days_counted=1))


def _seed_correlation(db, *, coffee_days: int = 9, days: int = 24) -> None:
    for i in range(1, days + 1):
        db.add(SahhaDailyTotal(
            user_id=USER, metric="sleep_duration",
            bucket_start=TODAY - timedelta(days=i),
            total=360.0 if i <= coffee_days else 402.0,
            entries=1, days_counted=1))
    for i in range(1, coffee_days + 1):
        db.add(LifestyleDailyTotal(
            user_id=USER, metric="coffee", bucket_start=TODAY - timedelta(days=i),
            total=2.0, entries=2, days_counted=1))


@pytest.fixture
def set_engine(monkeypatch):
    from app.config import get_settings

    def _set(name: str) -> None:
        monkeypatch.setattr(get_settings(), "chat_engine", name)

    return _set


class QuotesTheTool(FakeProvider):
    """Calls one tool and answers with the tool's own validated wording.

    This is the shape that produced the report: the answer is entirely the
    tool's, and the corpus contributed nothing.
    """

    def __init__(self, name: str, args: dict) -> None:
        super().__init__()
        self.name = name
        self.args = args

    async def generate_turn(self, *, system, messages, tools):
        for message in messages:
            if isinstance(message, ToolResultMessage):
                payload = json.loads(message.results[0].content)
                return LLMTurn(
                    text=payload.get("deterministic_reply", FakeProvider.DEFAULT),
                    stop_reason="end_turn",
                )
        return LLMTurn(
            tool_calls=(ToolCall("t1", self.name, self.args),),
            stop_reason="tool_use",
        )


# --------------------------------------------------------------------------- #
# (c) A deterministic answer from the reader's own rows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_tracker_answer_cites_nothing(db_session, set_engine, name):
    """The reported bug, both engines.

    Retrieval still runs and still returns profiles — that is not the bug. The
    bug was treating them as provenance for an answer that never read them.
    """
    _seed_corpus(db_session)
    _seed_water(db_session)
    await db_session.flush()

    question = "how much water this week"
    # Non-vacuity: the unscoped fallback DOES return profiles for this message,
    # so an engine that cites the retrieved set has something to cite wrongly.
    assert await retrieve_chunks(db_session, set(), question)

    set_engine(name)
    provider = (
        FakeProvider() if name == "legacy"
        else QuotesTheTool("get_tracker_total",
                           {"metric": "water", "period": "this_week"})
    )
    result = await handle_chat(db_session, USER, question, provider, uuid.uuid4())

    assert "2000" in result.response_message
    assert result.citations is None, (
        f"a tracker total cited the corpus: {result.citations}"
    )
    assert result.provenance["used_chunks"] == []
    assert_provenance_agrees(result)


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_summary_answer_cites_nothing(db_session, set_engine, name):
    """The health summary is composed from the reader's own rows only."""
    _seed_corpus(db_session)
    _seed_water(db_session)
    await db_session.flush()

    set_engine(name)
    provider = (
        FakeProvider() if name == "legacy"
        else QuotesTheTool("get_health_summary", {"period": "week"})
    )
    result = await handle_chat(
        db_session, USER, "show my weekly health summary", provider, uuid.uuid4()
    )

    assert result.citations is None, f"summary cited {result.citations}"
    assert_provenance_agrees(result)


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_correlation_readout_cites_nothing(db_session, set_engine, name):
    """Worse than wrong here: a profile beside a co-occurrence count reads as
    clinical backing for the causal claim the handler exists to refuse."""
    _seed_corpus(db_session)
    _seed_correlation(db_session)
    await db_session.flush()

    set_engine(name)
    result = await handle_chat(
        db_session, USER, "does coffee affect my sleep", FakeProvider(),
        uuid.uuid4(),
    )

    assert result.provenance.get("path") == "correlation_query"
    assert result.citations is None
    assert_provenance_agrees(result)


# --------------------------------------------------------------------------- #
# (a) An extractive corpus answer cites EXACTLY what it rendered
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_an_extractive_answer_cites_exactly_its_rendered_chunks(
    db_session, set_engine, name
):
    """`build_extractive_answer` renders a SUBSET of what was retrieved.

    The legacy branch has cited `rendered_chunks` since Phase 4; the agentic
    twin cited the whole retrieved set from the same variable.
    """
    _seed_corpus(db_session)
    await db_session.flush()

    set_engine(name)
    result = await handle_chat(
        db_session, USER, "what are the symptoms of diabetes", FakeProvider(),
        uuid.uuid4(),
    )
    assert result.provenance.get("mode") == "extractive"

    chunks = await retrieve_chunks(db_session, {"MC001"},
                                   "what are the symptoms of diabetes")
    expected = [c.id for c in rendered_chunks(chunks, focused=True)]
    assert expected, "the fixture must actually render something"
    assert [c["chunk_id"] for c in corpus_citations(result)] == expected
    assert_provenance_agrees(result)


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_the_same_question_cites_the_same_sources_every_run(
    db_session, set_engine, name
):
    """This repo has already shipped citations that changed run to run."""
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine(name)

    runs = [
        (await handle_chat(db_session, USER, "what is diabetes",
                           FakeProvider(), uuid.uuid4())).citations
        for _ in range(3)
    ]
    assert runs[0], "an extractive answer still cites its source"
    assert runs[0] == runs[1] == runs[2]


async def test_both_engines_cite_identically(db_session, set_engine):
    """Same question, same corpus, same citation list — or the audit trail
    depends on which engine happened to run."""
    _seed_corpus(db_session)
    await db_session.flush()

    set_engine("legacy")
    legacy = await handle_chat(
        db_session, USER, "what is diabetes", FakeProvider(), uuid.uuid4())
    set_engine("agentic")
    agentic = await handle_chat(
        db_session, USER, "what is diabetes", FakeProvider(), uuid.uuid4())

    assert legacy.citations == agentic.citations
    assert legacy.provenance["used_chunks"] == agentic.provenance["used_chunks"]
    assert_provenance_agrees(legacy)
    assert_provenance_agrees(agentic)


# --------------------------------------------------------------------------- #
# (b) A generated answer cites the blocks it CITED, and only those
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_provider(monkeypatch):
    """LLM_PROVIDER=fake serves everything extractively; these need the model
    path, which is what the deployed app runs.

    Through the ENV, not the cached settings object: `set_grounding_mode`
    clears that cache, which would drop an attribute patch.
    """
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_PERSONAL = "why am i so tired lately with my diabetes"


async def test_a_generated_answer_cites_only_the_block_it_marked(
    db_session, set_engine, live_provider, set_grounding_mode
):
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine("legacy")

    result = await handle_chat(
        db_session, USER, _PERSONAL,
        FakeProvider(responses=["Tiredness has many ordinary causes [1]."]),
        uuid.uuid4(),
    )

    assert result.provenance.get("degraded") is None
    cited = corpus_citations(result)
    assert len(cited) == 1, f"one marker, one citation: {result.citations}"
    assert cited[0]["marker"] == "1"
    assert_provenance_agrees(result)


async def test_a_generated_answer_that_marked_nothing_cites_nothing(
    db_session, set_engine, live_provider, set_grounding_mode
):
    """Retrieval happened; the answer did not use it. That is not a citation."""
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine("legacy")

    result = await handle_chat(
        db_session, USER, _PERSONAL,
        FakeProvider(responses=["Tiredness has many ordinary causes."]),
        uuid.uuid4(),
    )

    assert result.provenance.get("degraded") is None
    assert result.citations is None
    assert_provenance_agrees(result)


# --------------------------------------------------------------------------- #
# (d) A reply that was REPLACED carries none of the discarded answer's sources
# --------------------------------------------------------------------------- #
_STRAY = "Your reading was 999 mg/dl [1]."


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_safe_reply_cites_nothing(
    db_session, set_engine, live_provider, set_grounding_mode, name
):
    """The legacy RAG site attached citations built from the ORIGINAL answer
    even after the numeric-fidelity guard threw that answer away."""
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine(name)

    result = await handle_chat(
        db_session, USER, _PERSONAL,
        FakeProvider(responses=[_STRAY, _STRAY]), uuid.uuid4(),
    )

    assert result.provenance.get("degraded") == "fidelity"
    assert "999" not in result.response_message
    assert result.citations is None, (
        f"a discarded answer's citations shipped with the safe reply: "
        f"{result.citations}"
    )
    assert_provenance_agrees(result)


async def test_the_same_answer_without_the_stray_value_does_cite(
    db_session, set_engine, live_provider, set_grounding_mode
):
    """Non-vacuity for the test above: the marker really would have cited."""
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine("legacy")

    result = await handle_chat(
        db_session, USER, _PERSONAL,
        FakeProvider(responses=["Tiredness has many ordinary causes [1]."]),
        uuid.uuid4(),
    )
    assert corpus_citations(result)


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_a_marker_free_corpus_answer_still_records_the_lookup(
    db_session, set_engine, live_provider, set_grounding_mode, name
):
    """`_GROUNDING_RULES` only demands a marker on a sentence stating a
    clinical value, so an ordinary educational answer is legitimately
    marker-free — and citations are marker-derived. Without `chunks` in the
    agentic provenance that left NO client-visible trace that the corpus was
    consulted, which is the reader-reported bug the old attribution list
    existed to fix."""
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine(name)

    result = await handle_chat(
        db_session, USER, "what are the early symptoms of diabetes",
        FakeProvider(responses=[
            "Unexplained fatigue and increased thirst are common early "
            "features of diabetes. Please discuss this with your doctor."
        ]),
        uuid.uuid4(),
    )

    assert result.provenance.get("chunks"), (
        "retrieval ran and nothing in the payload says so"
    )
    assert_provenance_agrees(result)


@pytest.mark.parametrize("name", ["legacy", "agentic"])
async def test_general_knowledge_is_not_claimed_when_something_was_retrieved(
    db_session, set_engine, live_provider, set_grounding_mode, name
):
    """`[GK]` renders as "General knowledge (nothing retrieved)". Emitting it
    beside `provenance.chunks = [3 ids]` is one payload contradicting itself,
    and `log` mode — the default — ships the answer."""
    set_grounding_mode("log")
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine(name)

    result = await handle_chat(
        db_session, USER, _PERSONAL,
        FakeProvider(responses=["Staying active helps most people [GK]."]),
        uuid.uuid4(),
    )

    gk = [c for c in (result.citations or [])
          if c.get("source") == "general_knowledge"]
    assert not (gk and result.provenance.get("chunks")), (
        f"claimed nothing was retrieved beside {result.provenance.get('chunks')}"
    )


# --------------------------------------------------------------------------- #
# Legitimate citations are NOT dropped
# --------------------------------------------------------------------------- #
async def test_a_suggestions_answer_still_cites_the_rows_it_rendered(
    db_session, set_engine
):
    """The one deterministic handler that DOES quote the corpus."""
    _seed_corpus(db_session)
    db_session.add(ConditionRegistry(
        condition_code="MC002", display_name="Hypertension",
        aliases=["hypertension"], engine_codes=["HTN"], active=True))
    await db_session.flush()
    set_engine("legacy")

    result = await handle_chat(
        db_session, USER, "any tips for diabetes", FakeProvider(), uuid.uuid4()
    )

    assert result.provenance.get("path") == "mcp_suggestions"
    assert corpus_citations(result), "a corpus-quoting handler must cite"
    # A SUBSET of what the query returned, never equal to it by assumption:
    # asserting equality is what pinned the over-citation in place. See the
    # test below for the case where the renderer actually drops rows.
    assert set(result.provenance["used_chunks"]) <= set(
        result.provenance["chunks"]
    )
    assert_provenance_agrees(result)


async def test_suggestions_cite_only_the_rows_the_renderer_emitted(
    db_session, set_engine
):
    """`format_suggestions` renders at most 4 headers x 4 bullets, drops lines
    it cannot parse, and dedupes. Building the citation list from a parallel
    slice of the QUERY RESULT cited four chunks for a reply carrying one
    chunk's bullets -- the same drift the `Used` threading closed one level up,
    re-opened by a handler computing its own list from the wrong thing."""
    db_session.add(ConditionRegistry(
        condition_code="MC001", display_name="Diabetes mellitus",
        aliases=["diabetes", "sugar"], engine_codes=["T2DM"], active=True))
    # Chunk 1 supplies all four headers on its own; chunk 2 adds a fifth
    # section that `order[:_MAX_SECTIONS]` drops, and chunk 3 is a shape
    # `_parse_suggestion_line` cannot read at all.
    db_session.add(McpChunk(
        condition_code="MC001", chunk_type="suggestions",
        content="Diabetes mellitus — suggestions:\n" + "\n".join(
            f"LHP: {lhp}; Suggestion: Something sensible about {lhp} to do "
            "every day"
            for lhp in ("Food", "Water", "Sleep", "Movement")
        )))
    db_session.add(McpChunk(
        condition_code="MC001", chunk_type="suggestions_2",
        content="Diabetes mellitus — suggestions:\n"
                "LHP: Footcare; Suggestion: Check your feet daily for any "
                "cuts or sores"))
    db_session.add(McpChunk(
        condition_code="MC001", chunk_type="suggestions_3",
        content="Diabetes mellitus — suggestions:\nnot a table row at all"))
    await db_session.flush()
    set_engine("legacy")

    result = await handle_chat(
        db_session, USER, "any tips for diabetes", FakeProvider(), uuid.uuid4()
    )

    assert result.provenance.get("path") == "mcp_suggestions"
    used = result.provenance["used_chunks"]
    assert len(used) == 1, "only chunk 1 contributed a rendered bullet"
    assert set(used) < set(result.provenance["chunks"])
    assert "Footcare" not in result.response_message
    assert_provenance_agrees(result)


async def test_a_corpus_tool_cites_its_own_retrieval_not_the_turns(
    db_session, set_engine
):
    """`get_condition_guidance` runs a SEPARATE scoped retrieval. Citing the
    turn-level retrieval beside it is a wrong citation on the one agentic path
    where a citation is genuinely owed."""
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine("agentic")

    result = await handle_chat(
        db_session, USER, "how much water this week",
        QuotesTheTool("get_condition_guidance",
                      {"condition": "diabetes", "section": "suggestions"}),
        uuid.uuid4(),
    )

    cited = corpus_citations(result)
    assert cited, "the reply quotes profile text verbatim"
    assert {c["condition_code"] for c in cited} == {"MC001"}
    assert_provenance_agrees(result)


# --------------------------------------------------------------------------- #
# Canned replies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["legacy", "agentic"])
@pytest.mark.parametrize("message", [
    "hi there",
    "i can't breathe",
    "what's the weather in Chennai",
])
async def test_a_canned_reply_cites_nothing(db_session, set_engine, name, message):
    _seed_corpus(db_session)
    await db_session.flush()
    set_engine(name)

    result = await handle_chat(
        db_session, USER, message, FakeProvider(), uuid.uuid4()
    )
    assert result.citations is None
    assert_provenance_agrees(result)

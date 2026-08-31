"""A named condition with no profile must return NOTHING, never a substitute.

`scripts/ingest_mcp_corpus.py:36` maps `MC051` -> `HTN`, and MC051 (Primary
Hypertension) is ABSENT from `knowledge/mcp/` — the corpus jumps MC050 -> MC052.
So every hypertension question resolves to a scope that matches zero rows.

Letting that fall through to the global lexical fallback was tried and REVERTED.
Every chunk header is literally "<Name> - symptoms:", so the question own
section noun gives overlap > 0, the score floor is vacuous, and the section
filter then drops the chunks that DO mention the topic for being the wrong
section. Measured against the real 481-file corpus, "what is hypertension"
answered "What it is - Hyperuricemia (asymptomatic)" and "symptoms of
hypertension" answered with Hypothyroidism symptoms — rendered verbatim by the
extractive path with no model in the loop.

No answer is safer than another condition answer. The real gap is fixed by
ingesting MC051.
"""

from __future__ import annotations

import pytest

from app.models.chat import McpChunk
from app.rag.retrieval import retrieve_chunks

pytestmark = pytest.mark.asyncio


async def _seed(db_session) -> None:
    db_session.add(
        McpChunk(
            condition_code="MC052",
            chunk_type="definition",
            content=(
                "Coronary Heart Disease — definition:\n"
                "Coronary heart disease develops when the arteries supplying "
                "the heart narrow. Hypertension is a major contributing "
                "factor and is commonly recorded alongside it."
            ),
        )
    )
    await db_session.flush()


async def test_a_named_condition_with_no_profile_never_borrows_another(
    db_session,
):
    """The regression guard. An MC052 chunk is seeded and MENTIONS hypertension.

    An earlier version of this test asserted only `any("hypertension" in
    content)` and passed for exactly the wrong reason: the seeded chunk is a
    Coronary Heart Disease profile, which the reader would have been shown as
    the answer to a blood-pressure question.
    """
    await _seed(db_session)

    for message in (
        "what is hypertension",
        "what are the symptoms of hypertension",
    ):
        chunks = await retrieve_chunks(db_session, {"MC051"}, message)
        assert chunks == [], (
            f"{message!r} must return no corpus content rather than another "
            f"condition profile, got {[c.condition_code for c in chunks]}"
        )


async def test_a_scope_that_does_match_is_unaffected(db_session):
    await _seed(db_session)
    chunks = await retrieve_chunks(
        db_session, {"MC052"}, "what is coronary heart disease"
    )
    assert [c.condition_code for c in chunks] == ["MC052"]


async def test_empty_corpus_returns_empty(db_session):
    assert await retrieve_chunks(db_session, {"MC051"}, "what is hypertension") == []


async def test_an_unscoped_question_still_reaches_the_global_fallback(db_session):
    """Reverting the fall-through must not disable the fallback entirely.

    An EMPTY scope (a symptom description naming no condition) is a different
    branch and still searches the whole corpus.
    """
    await _seed(db_session)
    chunks = await retrieve_chunks(db_session, set(), "narrowing arteries")
    assert chunks and chunks[0].condition_code == "MC052"


async def test_section_filter_applies_on_the_scoped_path(db_session):
    for section, body in (
        ("definition", "Coronary heart disease is a narrowing of the arteries."),
        ("symptoms", "Chest tightness and breathlessness on exertion are common."),
        ("prevalence", "It is common worldwide."),
    ):
        db_session.add(
            McpChunk(
                condition_code="MC052",
                chunk_type=section,
                content=f"Coronary Heart Disease — {section}:\n{body}",
            )
        )
    await db_session.flush()

    chunks = await retrieve_chunks(
        db_session, {"MC052"}, "what are the symptoms of coronary heart disease"
    )
    assert [c.chunk_type for c in chunks] == ["symptoms"]


async def test_section_filter_fails_open_when_the_section_is_missing(db_session):
    await _seed(db_session)  # definition only
    chunks = await retrieve_chunks(
        db_session, {"MC052"}, "what are the symptoms of coronary heart disease"
    )
    assert chunks, "a missing section must degrade to the unfiltered ranking"
    assert chunks[0].chunk_type == "definition"

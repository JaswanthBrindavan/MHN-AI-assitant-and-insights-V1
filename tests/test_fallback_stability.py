"""Two phrasings of one question must not return unrelated citations.

Reported from staging, two adjacent turns:

    "my legs swell up at night"      ->  MC190  MC190  MC190  MC190
    "my legs are swelling at night"  ->  MC006  MC009  MC006  MC041

Neither set was wrong exactly — both were whatever the candidate window
happened to contain. Neither message names a condition, so both go to the
unscoped global fallback, which capped its pool at 200 rows ORDERED BY
condition_code: over a ~15,000-chunk corpus that is the alphabetically first
200 matches, not the 200 best. `%swell%` also matches "swelling" while
`%swelling%` does not match "swell", so the two pools genuinely differ — and
ordering by code then sliced each from a different part of the corpus.

Ranking the window by how many query tokens each chunk matches makes it the
most relevant rows, so two phrasings of one question overlap instead of
diverging.
"""

from __future__ import annotations

import pytest

from app.models.chat import McpChunk
from app.rag.retrieval import retrieve_chunks

pytestmark = pytest.mark.asyncio


async def _seed(db) -> None:
    """One chunk that is genuinely about leg swelling, buried under many that
    merely mention 'night' — the shape that defeated the old window."""
    db.add(McpChunk(
        condition_code="MC900", chunk_type="symptoms",
        content="Chronic Venous Insufficiency — symptoms:\n"
                "Swelling of the legs and ankles that worsens through the day "
                "and at night after prolonged standing, with aching and "
                "heaviness in the affected leg.",
    ))
    # Decoys, alphabetically FIRST, each matching only the weaker token.
    for i in range(1, 12):
        db.add(McpChunk(
            condition_code=f"MC{i:03d}", chunk_type="symptoms",
            content=f"Condition {i} — symptoms:\n"
                    "Symptoms are often worse at night and disturb sleep, "
                    "though the pattern varies between people.",
        ))
    await db.flush()


async def test_the_window_keeps_the_best_match_not_the_first_codes(db_session):
    await _seed(db_session)
    chunks = await retrieve_chunks(
        db_session, set(), "my legs are swelling at night"
    )
    assert chunks, "the fallback should retrieve something"
    codes = [c.condition_code for c in chunks]
    assert "MC900" in codes, (
        f"the one chunk actually about leg swelling was not retrieved: {codes}"
    )


@pytest.mark.parametrize(("a", "b"), [
    ("my legs swell up at night", "my legs are swelling at night"),
    ("i have swelling in my legs", "swelling in the legs at night"),
])
async def test_two_phrasings_of_one_question_agree(db_session, a, b):
    """Not byte-identical — the token sets genuinely differ — but they must
    agree on the condition, which is what the reader sees as a citation."""
    await _seed(db_session)
    ca = {c.condition_code for c in await retrieve_chunks(db_session, set(), a)}
    cb = {c.condition_code for c in await retrieve_chunks(db_session, set(), b)}
    assert ca & cb, f"no overlap at all between {a!r} -> {ca} and {b!r} -> {cb}"


async def test_the_same_question_twice_is_identical(db_session):
    """A LIMIT without a total order lets the pool rotate between calls."""
    await _seed(db_session)
    q = "my legs are swelling at night"
    first = [c.id for c in await retrieve_chunks(db_session, set(), q)]
    from app.rag.retrieval import reset_retrieval_cache
    reset_retrieval_cache()
    second = [c.id for c in await retrieve_chunks(db_session, set(), q)]
    assert first == second


async def test_known_gap_morphology_is_not_handled(db_session):
    """Documented, not fixed: the prefilter is substring matching, not stemming.

    "swollen" does not substring-match "swelling", so a reader who says "my
    legs are swollen" retrieves nothing from a corpus that only ever writes
    "swelling". `%swell%` happens to cover both, which is luck, not design.

    This test asserts the CURRENT behaviour so the gap is visible and a future
    stemmer has something to flip. It is a retrieval-recall limitation, not the
    window-ordering bug this module exists for.
    """
    await _seed(db_session)
    chunks = await retrieve_chunks(db_session, set(), "my legs are swollen")
    assert chunks == [], (
        "morphology now works — good; delete this test and say so in the "
        "commit rather than leaving a stale 'known gap' on record"
    )

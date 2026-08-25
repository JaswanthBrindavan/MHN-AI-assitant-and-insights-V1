"""Prompt caching: the breakpoint, the prefix stability, and the budget.

A cache breakpoint that fails to cache is INVISIBLE — the reply is identical
and only the usage numbers differ. Everything checkable without a live API
key is checked here; the parts that need one are in scripts/cache_probe.py,
which refuses to report a hit rate it did not measure.
"""

from __future__ import annotations

import pytest

from app.chat.agent import append_directive
from app.llm.anthropic import _CACHE_CONTROL, _to_system_blocks
from app.llm.tools import join_system
from app.rag.prompt import (
    DEFAULT_VOLATILE_BUDGET_TOKENS,
    build_agentic_system_prompt,
    estimate_tokens,
)
from app.rag.retrieval import RetrievedChunk


def _chunk(content: str, code: str = "MC001") -> RetrievedChunk:
    return RetrievedChunk(
        id="c1", condition_code=code, chunk_type="overview", content=content, score=1.0
    )


# --------------------------------------------------------------------------- #
# The breakpoint
# --------------------------------------------------------------------------- #
def test_a_plain_string_gets_no_breakpoint():
    """Callers that never split must be unaffected — no behaviour change."""
    assert _to_system_blocks("just a prompt") == "just a prompt"


def test_the_breakpoint_lands_on_the_stable_prefix_only():
    blocks = _to_system_blocks(["STABLE RULES", "volatile chunks"])
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == "STABLE RULES"
    assert blocks[0]["cache_control"] == _CACHE_CONTROL
    # The volatile half must carry NO breakpoint. One there would re-write the
    # cache every turn: full price on writes, never a read.
    assert "cache_control" not in blocks[1]


def test_an_empty_volatile_half_does_not_produce_an_empty_block():
    """An empty text block is not valid content for the API."""
    blocks = _to_system_blocks(["STABLE", ""])
    assert blocks == "STABLE"


def test_splitting_never_changes_what_the_model_is_told():
    """The split is a billing optimisation. It must not alter the prompt."""
    stable, volatile = build_agentic_system_prompt("patient ctx", None, None, None)
    joined = join_system([stable, volatile])
    blocks = _to_system_blocks([stable, volatile])
    assert isinstance(blocks, list)
    assert "\n\n".join(b["text"] for b in blocks) == joined


# --------------------------------------------------------------------------- #
# Prefix stability — the property the whole feature rests on
# --------------------------------------------------------------------------- #
def test_the_prefix_is_byte_identical_across_wildly_different_turns():
    """If the prefix varies at all, the cache is never read. Not once."""
    first, _ = build_agentic_system_prompt(
        "Patient: 44F, hypertension",
        '{"conditions": ["HTN"]}',
        recent_turns=[{"role": "user", "message": "what is my blood pressure?"}],
        chunks=[_chunk("Blood pressure targets vary by individual.")],
    )
    second, _ = build_agentic_system_prompt("", None, None, None)
    assert first == second


def test_allow_questions_is_the_one_thing_that_may_change_the_prefix():
    """A documented, deliberate exception — it changes the RULES, not data.

    It flips at most once per session (after the clarifying-question budget is
    spent), so it costs one extra cache write, not one per turn.
    """
    with_questions, _ = build_agentic_system_prompt("", None, None, None, True)
    without, _ = build_agentic_system_prompt("", None, None, None, False)
    assert with_questions != without


def test_patient_data_never_reaches_the_cached_prefix():
    """A cached prefix is shared across turns. PHI in it would be a leak risk.

    This is the safety half of the split, not the cost half.
    """
    stable, volatile = build_agentic_system_prompt(
        "Patient: Ramesh, 61, creatinine 1.8",
        '{"conditions": ["CKD"]}',
        recent_turns=[{"role": "user", "message": "my creatinine is 1.8"}],
        chunks=[_chunk("Kidney function is assessed with several markers.")],
    )
    for leaked in ("Ramesh", "1.8", "CKD", "creatinine"):
        assert leaked not in stable, f"{leaked!r} leaked into the cached prefix"
    assert "Ramesh" in volatile


# --------------------------------------------------------------------------- #
# Per-turn directives must not disturb the prefix
# --------------------------------------------------------------------------- #
def test_a_directive_appends_to_the_volatile_tail_not_the_prefix():
    result = append_directive(["STABLE", "volatile"], "\n\nANSWER NOW")
    assert result == ["STABLE", "volatile\n\nANSWER NOW"]


def test_a_directive_on_a_plain_string_still_concatenates():
    assert append_directive("prompt", "\n\nEXTRA") == "prompt\n\nEXTRA"


def test_a_directive_with_an_empty_tail_does_not_leave_stray_newlines():
    assert append_directive(["STABLE", ""], "\n\nEXTRA") == ["STABLE", "EXTRA"]


# --------------------------------------------------------------------------- #
# The context budget
# --------------------------------------------------------------------------- #
def test_an_oversized_chunk_is_dropped_rather_than_truncated():
    """Truncating would hand the model a fact with its qualifier missing.

    "Values above 7 are concerning IN UNTREATED ADULTS" cut mid-sentence is
    worse than no chunk at all.
    """
    huge = _chunk("x" * 100_000)
    _, volatile = build_agentic_system_prompt("", None, None, chunks=[huge])
    assert "x" * 1000 not in volatile


def test_the_budget_drops_chunks_before_conversation_turns():
    """A dropped chunk costs a source. A dropped turn costs the thread."""
    chunks = [_chunk("y" * 4000) for _ in range(10)]
    turns = [{"role": "user", "message": "is that serious?"}]
    _, volatile = build_agentic_system_prompt(
        "", None, recent_turns=turns, chunks=chunks, budget_tokens=500
    )
    assert "is that serious?" in volatile, "the conversation thread was dropped first"


def test_the_most_recent_turn_survives_any_budget():
    """A follow-up fragment is meaningless without the turn it follows."""
    turns = [
        {"role": "user", "message": "z" * 20_000},
        {"role": "assistant", "message": "z" * 20_000},
        {"role": "user", "message": "THE LATEST ONE"},
    ]
    _, volatile = build_agentic_system_prompt(
        "", None, recent_turns=turns, chunks=None, budget_tokens=100
    )
    assert "THE LATEST ONE" in volatile


def test_patient_context_is_never_dropped_by_the_budget():
    """It is small, and it is the reader's own situation."""
    _, volatile = build_agentic_system_prompt(
        "Patient: on metformin",
        None,
        recent_turns=None,
        chunks=[_chunk("w" * 50_000)],
        budget_tokens=200,
    )
    assert "metformin" in volatile


def test_a_normal_turn_is_untouched_by_the_budget():
    """The budget must be a ceiling, not a routine editor."""
    chunks = [_chunk("Ordinary guidance about blood sugar.") for _ in range(3)]
    _, volatile = build_agentic_system_prompt(
        "Patient ctx", None, recent_turns=[{"role": "user", "message": "hello"}],
        chunks=chunks,
    )
    assert volatile.count("Ordinary guidance") == 3


@pytest.mark.parametrize("text", ["", "a", "hello world", "x" * 5000])
def test_the_token_estimate_is_always_positive(text):
    """A zero estimate would make the budget loop believe anything fits."""
    assert estimate_tokens(text) >= 1


def test_the_default_budget_leaves_room_for_the_answer():
    """A budget at or above the context window would defeat its own purpose."""
    assert 1000 <= DEFAULT_VOLATILE_BUDGET_TOKENS <= 50_000

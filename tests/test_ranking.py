"""Pure ranking primitives: BM25 scoring and (weighted) RRF fusion."""

from __future__ import annotations

import pytest

from app.rag.ranking import bm25_scores, rrf_fuse


def test_bm25_scores_relevant_doc_highest():
    docs = [
        "tinnitus is a ringing or buzzing sound in the ears",
        "diabetes mellitus raises blood glucose levels",
        "hypertension is persistently raised blood pressure",
    ]
    scores = bm25_scores("ringing sound in my ears", docs)
    assert scores[0] == max(scores)
    assert scores[0] > 0


def test_bm25_scores_empty_inputs():
    assert bm25_scores("", ["doc"]) == [0.0]
    assert bm25_scores("query", []) == []


def test_rrf_fuse_uniform_k_sums_reciprocal_ranks():
    fused = rrf_fuse([["a", "b"], ["b", "a"]], k=60)
    # Both appear at ranks 1 and 2 across the two rankings — identical scores.
    assert fused["a"] == pytest.approx(fused["b"])
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)


def test_rrf_fuse_semantic_head_beats_lexical_coincidence():
    """The bug the per-ranking ks fix exists for: a chunk that is mid-tail
    semantically but rank 1 lexically must NOT outscore the semantic top hit
    ("ringing sound in my ears" once lost tinnitus to a chunk that merely
    contained the words "sound" and "night")."""
    semantic = ["true_hit"] + [f"filler_{i}" for i in range(13)] + ["coincidence"]
    lexical = ["coincidence"]
    fused = rrf_fuse([semantic, lexical], ks=[10, 60])
    assert fused["true_hit"] > fused["coincidence"]
    # With a uniform k=60 the coincidence double-counts past the true hit —
    # the regression this test pins down.
    uniform = rrf_fuse([semantic, lexical], k=60)
    assert uniform["coincidence"] > uniform["true_hit"]


def test_rrf_fuse_ks_length_mismatch_raises():
    with pytest.raises(ValueError):
        rrf_fuse([["a"], ["b"]], ks=[10])

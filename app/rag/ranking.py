"""Hybrid retrieval ranking: BM25 + vector ranks fused with RRF (pure stdlib).

Stage 1 (recall): two independent candidate rankings —
  * lexical: BM25 (Okapi, k1/b defaults) over the scoped chunk pool
  * semantic: pgvector cosine ANN with an instruction-formatted query
Stage 2 (rerank): Reciprocal Rank Fusion merges the two rankings; a
section-intent boost then nudges chunks whose section matches the question
("how is X diagnosed" → diagnosis/tests). Deterministic; no extra models.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_BM25_K1 = 1.5
_BM25_B = 0.75
# Standard RRF constant — dampens the head of each ranking.
RRF_K = 60


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    """Okapi BM25 of the query against each document (index = candidate pool)."""
    query_terms = tokenize(query)
    if not query_terms or not documents:
        return [0.0] * len(documents)

    doc_tokens = [tokenize(d) for d in documents]
    doc_lens = [len(t) for t in doc_tokens]
    avg_len = sum(doc_lens) / len(doc_lens) if doc_lens else 1.0
    doc_freqs = [Counter(t) for t in doc_tokens]

    n_docs = len(documents)
    df: Counter[str] = Counter()
    for freqs in doc_freqs:
        for term in freqs:
            df[term] += 1

    scores: list[float] = []
    for i in range(n_docs):
        score = 0.0
        for term in set(query_terms):
            tf = doc_freqs[i].get(term, 0)
            if tf == 0:
                continue
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * doc_lens[i] / avg_len
            )
            score += idf * tf * (_BM25_K1 + 1) / denom
        scores.append(score)
    return scores


def rrf_fuse(
    rankings: list[list[str]],
    k: int = RRF_K,
    ks: list[int] | None = None,
) -> dict[str, float]:
    """Reciprocal Rank Fusion: id → Σ 1/(k_i + rank) across rankings.

    ``ks`` optionally assigns each ranking its own constant; a smaller k
    sharpens that ranking's head so its top results carry more weight.
    """
    if ks is not None and len(ks) != len(rankings):
        raise ValueError("ks must have one entry per ranking")
    fused: dict[str, float] = {}
    for idx, ranking in enumerate(rankings):
        k_i = ks[idx] if ks is not None else k
        for rank, item_id in enumerate(ranking, start=1):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k_i + rank)
    return fused


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def mmr_rerank(
    ranked_ids: list[str],
    vectors: dict[str, list[float]],
    k: int,
    lam: float = 0.75,
) -> list[str]:
    """Maximal-marginal-relevance rerank over an already relevance-ordered list.

    Balances staying near the fused relevance order (weight ``lam``) against
    penalising candidates too similar to chunks already selected — so the k
    slots carry k different pieces of information instead of four restatements
    of the same section. Deterministic; ids without a vector keep their fused
    position unpenalised. Relevance is taken from the input ORDER (reciprocal
    rank), so the caller's fusion scoring stays the single source of truth.
    """
    if k <= 0 or not ranked_ids:
        return []
    relevance = {cid: 1.0 / (i + 1) for i, cid in enumerate(ranked_ids)}
    selected: list[str] = []
    remaining = list(ranked_ids)
    while remaining and len(selected) < k:
        best_id, best_score = None, None
        for cid in remaining:
            penalty = 0.0
            vec = vectors.get(cid)
            if vec is not None and selected:
                penalty = max(
                    (
                        _cosine(vec, vectors[sid])
                        for sid in selected
                        if vectors.get(sid) is not None
                    ),
                    default=0.0,
                )
            score = lam * relevance[cid] - (1.0 - lam) * penalty
            if best_score is None or score > best_score:
                best_id, best_score = cid, score
        assert best_id is not None
        selected.append(best_id)
        remaining.remove(best_id)
    return selected

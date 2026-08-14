"""Condition-scoped retrieval over mcp_chunks.

Scope = conditions named in the message ∪ the user's pedigree/insight
conditions. Scoping is data-driven from the clinically-validated condition
registry (512 Master Condition Profiles) when ingested, falling back to the
legacy static keyword map otherwise. Uses vector ANN when embeddings are
present and an embedding service is configured; otherwise falls back to
keyword (token-overlap) search. Chunks are returned ranked and capped at k.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.registry import load_condition_index
from app.models.chat import McpChunk

logger = logging.getLogger("davi.retrieval")

# Message keyword → condition code (DRAFT — pending clinician sign-off).
CONDITION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "T2DM": ("diabetes", "diabetic", "blood sugar", "glucose", "hba1c", "sugar level"),
    "HTN": ("blood pressure", "hypertension", " bp ", "hypertensive"),
    "CAD": ("chest pain", "heart disease", "coronary", "cardiac", "angina", "cholesterol"),
}

_STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "what", "how", "why", "does",
    "can", "should", "about", "have", "has", "are", "is", "of", "to", "in",
    "my", "me", "a", "an", "on", "do", "i",
}


@dataclass
class RetrievedChunk:
    id: str
    condition_code: str
    chunk_type: str
    content: str
    score: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_code": self.condition_code,
            "chunk_type": self.chunk_type,
            "score": round(self.score, 4),
        }


def extract_condition_codes(message: str) -> set[str]:
    text = f" {message.lower()} "
    codes: set[str] = set()
    for code, keywords in CONDITION_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            codes.add(code)
    return codes


def scope_codes(message: str, user_condition_codes: set[str]) -> set[str]:
    """Legacy static scoping (kept as the registry-less fallback)."""
    return extract_condition_codes(message) | set(user_condition_codes)


async def resolve_scope(
    db: AsyncSession, message: str, user_condition_codes: set[str]
) -> set[str]:
    """Data-driven scoping via the condition registry, static fallback.

    Registry path: message keywords → MC codes; the user's legacy engine codes
    (T2DM, HTN, CAD) are kept AND mapped to their MC equivalents so chunks
    ingested under either coding are retrievable.
    """
    index = await load_condition_index(db)
    if index is None:
        return scope_codes(message, user_condition_codes)
    codes = index.match_message(message)
    # Static extraction contributes AND is mapped to MC codes: "diabetes" has
    # no bare registry alias, but static T2DM → MC001 reaches the real profile.
    codes |= index.map_engine_codes(
        set(user_condition_codes) | extract_condition_codes(message)
    )
    return codes


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS and len(t) > 2]


# Query-intent → chunk-section boost: "how is X diagnosed" should favour the
# diagnosis/tests chunks even when token overlap is thin ("diagnosed" and
# "diagnosis" don't token-match).
_SECTION_INTENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diagnos", ("diagnosis", "tests_quantitative", "tests_qualitative")),
    ("test", ("tests_quantitative", "tests_qualitative", "diagnosis")),
    ("symptom", ("symptoms", "signs")),
    ("sign", ("signs", "symptoms")),
    ("cause", ("etiology", "risk_profiles")),
    ("why", ("etiology",)),
    ("complicat", ("complications",)),
    ("treat", ("suggestions",)),
    ("manage", ("suggestions", "lifestyle_influence")),
    ("prevent", ("suggestions", "lifestyle_influence")),
    ("diet", ("suggestions", "lifestyle_influence")),
    ("food", ("suggestions", "lifestyle_influence")),
    ("exercis", ("suggestions", "lifestyle_influence")),
    ("lifestyle", ("lifestyle_influence", "suggestions")),
)
_SECTION_BOOST = 0.05


def _section_boost(message_lower: str, chunk_type: str) -> float:
    base_section = chunk_type.rsplit("_", 1)[0] if chunk_type[-1:].isdigit() else chunk_type
    for stem, sections in _SECTION_INTENT:
        if stem in message_lower and (
            base_section in sections or chunk_type in sections
        ):
            return _SECTION_BOOST
    return 0.0


def _keyword_rank(rows: list[McpChunk], message: str) -> list[RetrievedChunk]:
    query_tokens = set(_tokens(message))
    message_lower = message.lower()
    scored: list[RetrievedChunk] = []
    for r in rows:
        content_tokens = _tokens(r.content)
        overlap = sum(1 for t in content_tokens if t in query_tokens)
        score = overlap / (1 + len(content_tokens)) if content_tokens else 0.0
        score += _section_boost(message_lower, r.chunk_type)
        scored.append(
            RetrievedChunk(
                id=str(r.id),
                condition_code=r.condition_code,
                chunk_type=r.chunk_type,
                content=r.content,
                score=score,
            )
        )
    # Deterministic ordering: score desc, then stable keys.
    scored.sort(key=lambda c: (-c.score, c.condition_code, c.chunk_type, c.id))
    return scored


# Cap on candidate rows pulled for the unscoped (symptom-only) fallback.
GLOBAL_FALLBACK_CANDIDATES = 200


async def _global_fallback_rows(db: AsyncSession, message: str) -> list[McpChunk]:
    """Token-prefiltered search over ALL chunks for messages naming no condition.

    Lets symptom-only questions ("frequent urination and excessive thirst")
    reach the corpus. SQL prefilter keeps the candidate set small; final
    ranking happens in ``_keyword_rank``.
    """
    tokens = [t for t in _tokens(message) if len(t) >= 5][:8]
    if not tokens:
        return []
    conditions = [McpChunk.content.ilike(f"%{t}%") for t in tokens]
    rows = (
        await db.execute(
            select(McpChunk).where(or_(*conditions)).limit(GLOBAL_FALLBACK_CANDIDATES)
        )
    ).scalars().all()
    return list(rows)


_VEC_CANDIDATES = 24
_BM25_CANDIDATES = 24
# RRF-scale section boost (RRF scores live around 1/60 ≈ 0.0167).
_RRF_SECTION_BOOST = 0.008


async def _hybrid_rank(
    db: AsyncSession,
    condition_codes: set[str],
    message: str,
    k: int,
) -> list[RetrievedChunk] | None:
    """Hybrid search: BM25 + instructed-vector ANN, fused with RRF, then a
    section-intent rerank. None → caller falls back to keyword ranking.

    PostgreSQL-only (the sqlite test variant stores embeddings as JSON) and
    requires a configured embedding service AND embedded chunks.
    """
    from app.rag.embeddings import embed_query, embeddings_configured
    from app.rag.ranking import bm25_scores, rrf_fuse

    bind = db.get_bind()
    if getattr(bind, "dialect", None) is None or bind.dialect.name != "postgresql":
        return None
    if not embeddings_configured():
        return None
    query_vector = await embed_query(message, instruct=True)
    if query_vector is None:
        return None

    # --- semantic candidates (pgvector cosine ANN) ---
    stmt = (
        select(
            McpChunk,
            McpChunk.embedding.cosine_distance(query_vector).label("distance"),
        )
        .where(McpChunk.embedding.is_not(None))
    )
    if condition_codes:
        stmt = stmt.where(McpChunk.condition_code.in_(condition_codes))
    stmt = stmt.order_by("distance").limit(_VEC_CANDIDATES)
    vec_rows = (await db.execute(stmt)).all()
    if not vec_rows:
        return None
    by_id: dict[str, McpChunk] = {str(c.id): c for c, _d in vec_rows}
    vec_ranking = [str(c.id) for c, _d in vec_rows]

    # --- lexical candidates (BM25 over the scoped pool) ---
    if condition_codes:
        pool = list(
            (
                await db.execute(
                    select(McpChunk).where(
                        McpChunk.condition_code.in_(condition_codes)
                    )
                )
            ).scalars().all()
        )
    else:
        pool = await _global_fallback_rows(db, message)
    bm25_ranking: list[str] = []
    if pool:
        scores = bm25_scores(message, [c.content for c in pool])
        ranked = sorted(
            zip(pool, scores, strict=True),
            key=lambda pair: (-pair[1], str(pair[0].id)),
        )
        bm25_ranking = [
            str(c.id) for c, score in ranked[:_BM25_CANDIDATES] if score > 0
        ]
        for c in pool:
            by_id.setdefault(str(c.id), c)

    # --- reciprocal-rank fusion + section-intent rerank ---
    fused = rrf_fuse([vec_ranking, bm25_ranking])
    message_lower = message.lower()
    scored = [
        RetrievedChunk(
            id=chunk_id,
            condition_code=by_id[chunk_id].condition_code,
            chunk_type=by_id[chunk_id].chunk_type,
            content=by_id[chunk_id].content,
            score=round(
                score
                + (_RRF_SECTION_BOOST
                   if _section_boost(message_lower, by_id[chunk_id].chunk_type)
                   else 0.0),
                8,
            ),
        )
        for chunk_id, score in fused.items()
    ]
    scored.sort(key=lambda c: (-c.score, c.condition_code, c.chunk_type, c.id))
    return scored[:k]


async def retrieve_chunks(
    db: AsyncSession,
    condition_codes: set[str],
    message: str,
    k: int = 4,
) -> list[RetrievedChunk]:
    """Return up to k chunks scoped to the given conditions, ranked.

    Vector ANN (hybrid with keyword/section signals) when an embedding service
    is configured and chunks are embedded; deterministic keyword ranking
    otherwise. Empty scope falls back to a token-prefiltered global search so
    symptom descriptions still retrieve educational content.
    """
    try:
        hybrid = await _hybrid_rank(db, condition_codes, message, k)
        if hybrid:
            return hybrid
    except Exception:  # noqa: BLE001 — hybrid path fails open to keyword
        logger.warning("hybrid retrieval failed; keyword fallback", exc_info=True)

    if condition_codes:
        rows = list(
            (
                await db.execute(
                    select(McpChunk).where(
                        McpChunk.condition_code.in_(condition_codes)
                    )
                )
            ).scalars().all()
        )
        if not rows:
            return []
        # Condition scope already establishes relevance; keep zero-score chunks.
        return _keyword_rank(rows, message)[:k]

    rows = await _global_fallback_rows(db, message)
    if not rows:
        return []
    # Unscoped relevance is purely lexical — drop zero-overlap chunks.
    ranked = _keyword_rank(rows, message)
    return [c for c in ranked[:k] if c.score > 0]

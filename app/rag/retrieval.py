"""Condition-scoped retrieval over mcp_chunks.

Scope = conditions named in the message ∪ the user's pedigree/insight
conditions. Scoping is data-driven from the clinically-validated condition
registry (512 Master Condition Profiles) when ingested, falling back to the
legacy static keyword map otherwise. Uses vector ANN when embeddings are
present and an embedding service is configured; otherwise falls back to
keyword (token-overlap) search. Chunks are returned ranked and capped at k.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.registry import load_condition_index
from app.models.chat import McpChunk

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
    codes |= index.map_engine_codes(set(user_condition_codes))
    # Static extraction still contributes (covers legacy-coded chunks by name).
    codes |= extract_condition_codes(message)
    return codes


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS and len(t) > 2]


def _keyword_rank(rows: list[McpChunk], message: str) -> list[RetrievedChunk]:
    query_tokens = set(_tokens(message))
    scored: list[RetrievedChunk] = []
    for r in rows:
        content_tokens = _tokens(r.content)
        overlap = sum(1 for t in content_tokens if t in query_tokens)
        score = overlap / (1 + len(content_tokens)) if content_tokens else 0.0
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


async def retrieve_chunks(
    db: AsyncSession,
    condition_codes: set[str],
    message: str,
    k: int = 4,
) -> list[RetrievedChunk]:
    """Return up to k chunks scoped to the given conditions, ranked.

    Empty scope falls back to a token-prefiltered global search so symptom
    descriptions still retrieve educational content.
    """
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

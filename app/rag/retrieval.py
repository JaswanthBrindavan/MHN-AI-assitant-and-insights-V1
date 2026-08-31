"""Condition-scoped retrieval over mcp_chunks.

Scope = conditions named in the message ∪ the user's pedigree/insight
conditions. Scoping is data-driven from the clinically-validated condition
registry (512 Master Condition Profiles) when ingested, falling back to the
legacy static keyword map otherwise. Uses vector ANN when embeddings are
present and an embedding service is configured; otherwise falls back to
keyword (token-overlap) search. Chunks are returned ranked and capped at k.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
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


# Query-intent -> chunk sections. This table drives BOTH a small ranking
# boost and, via `target_sections`, an actual FILTER.
#
# Entries are REGEX FRAGMENTS, compiled below with a leading word anchor.
# A bare stem is a prefix match ("diagnos" covers diagnosed/diagnosis);
# ending a fragment with a word anchor requires a whole word. That
# distinction became load-bearing the moment this table stopped being a
# +0.05 nudge and became a hard filter: as a nudge, `sign` matching
# "significant" or `test` matching "testosterone" cost nothing, but as a
# filter it DISCARDS every chunk that actually answered the question.
#
# Deliberately ABSENT: "normal", "range" and "stage". They were tried here
# and removed — as a filter they hijack ordinary questions ("is this
# normal?" is a symptom question, not a lab-reference one) and drop the
# symptom chunks entirely.
_SECTION_INTENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diagnos", ("diagnosis", "tests_quantitative", "tests_qualitative")),
    (r"tests?\b", ("tests_quantitative", "tests_qualitative", "diagnosis")),
    ("symptom", ("symptoms", "signs")),
    (r"signs?\b", ("signs", "symptoms")),
    ("cause", ("etiology", "risk_profiles")),
    (r"risk factors?\b", ("risk_profiles", "etiology")),
    ("why", ("etiology",)),
    ("complicat", ("complications",)),
    ("treat", ("suggestions",)),
    ("manage", ("suggestions", "lifestyle_influence")),
    ("prevent", ("suggestions", "lifestyle_influence")),
    ("diet", ("suggestions", "lifestyle_influence")),
    ("food", ("suggestions", "lifestyle_influence")),
    ("exercis", ("suggestions", "lifestyle_influence")),
    ("lifestyle", ("lifestyle_influence", "suggestions")),
    # Sections that previously had NO stem at all, so every question about
    # them scored 0.0 on every chunk and the reader got whatever the ranker
    # happened to like.
    ("prevalen", ("prevalence",)),
    ("how common", ("prevalence",)),
    ("how many people", ("prevalence",)),
    (r"red flags?\b", ("signs", "symptoms", "complications")),
    (r"types? of\b", ("classification",)),
    (r"kinds? of\b", ("classification",)),
    ("associated", ("associated_conditions",)),
    ("along with", ("associated_conditions",)),
    ("alongside", ("associated_conditions",)),
    ("trigger", ("lifestyle_triggers", "lifestyle_influence")),
    ("threshold", ("lifestyle_triggers", "tests_quantitative")),
    # STRONG definitional openers. These belong in the unioning table, not the
    # fallback one, because a reader who literally types "what is" is asking
    # for the definition even alongside another stem: "what is diabetes and
    # what are its symptoms" must keep BOTH. The ambiguous opener "what are" is
    # deliberately NOT here -- it is how most questions begin, so unioning it
    # dragged `definition` into every section query and made the symptoms
    # answer byte-identical to the definition answer.
    ("what is", ("definition",)),
    ("what it is", ("definition",)),
    ("what's", ("definition",)),
    ("defin", ("definition",)),
    ("meaning of", ("definition",)),
)

# Generic question OPENERS. Kept apart from the table above and consulted only
# when no specific stem matched, because they are phrasing, not intent: "what
# are the symptoms of X" opens with "what are" but is not asking for the
# definition. Unioning these with the specific stems put `definition` back into
# every section query and made the symptoms answer byte-identical to the
# definition answer again — the exact defect being fixed. Measured, not assumed.
_GENERIC_SECTION_INTENT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("what are", ("definition",)),
    ("tell me about", ("definition",)),
)

_SECTION_BOOST = 0.05

# Compiled once. Fragments are authored in THIS file and never taken from
# user input, so they are treated as regex (not re.escape'd) — escaping
# would turn the `?` quantifiers into literal question marks.
_SECTION_INTENT_RES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(r"\b" + frag), sections) for frag, sections in _SECTION_INTENT
)
_GENERIC_SECTION_RES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(r"\b" + frag), sections)
    for frag, sections in _GENERIC_SECTION_INTENT
)


def _base_section(chunk_type: str) -> str:
    """Strip a continuation suffix: ``symptoms_2`` → ``symptoms``.

    Lives here rather than in ``extractive`` because retrieval needs it too, and
    the two copies had already drifted.
    """
    return chunk_type.rsplit("_", 1)[0] if chunk_type[-1:].isdigit() else chunk_type


def target_sections(message: str) -> tuple[str, ...]:
    """Sections this question asks for, or () when it names none.

    Unions EVERY matching SPECIFIC stem rather than taking the first, so a
    compound question ("symptoms and complications of X") keeps both halves.
    The generic openers are a FALLBACK, not part of that union — see
    ``_GENERIC_SECTION_INTENT``.

    Pure and total: no I/O, and it cannot raise on any input string.
    """
    message_lower = message.lower()
    out: list[str] = []
    for pattern, sections in _SECTION_INTENT_RES:
        if pattern.search(message_lower):
            for section in sections:
                if section not in out:
                    out.append(section)
    if out:
        return tuple(out)
    for pattern, sections in _GENERIC_SECTION_RES:
        if pattern.search(message_lower):
            return sections
    return ()


def _prefer_section(
    ranked: list[RetrievedChunk], sections: tuple[str, ...]
) -> list[RetrievedChunk]:
    """Keep only chunks in ``sections``, or everything when that would empty it.

    Filtering rather than boosting is the fix for section-targeted questions: a
    flat +0.05 nudge could not lift MC001's 839-char ``symptoms`` chunk (0.0096)
    past its 111-char ``prevalence`` chunk (0.1429, scored on header hits
    alone), so "what are the symptoms of X" returned a reply byte-identical to
    "what is X".

    Fail-open by construction: a profile lacking the asked-for section degrades
    to today's unfiltered ranking rather than to an empty reply.
    """
    if not sections:
        return ranked
    kept = [
        c for c in ranked
        if _base_section(c.chunk_type) in sections or c.chunk_type in sections
    ]
    return kept or ranked


def _section_boost(sections: tuple[str, ...], chunk_type: str) -> float:
    """Ordering nudge WITHIN the section-filtered set.

    Takes the already-resolved sections rather than the raw message: it is
    called once per candidate chunk (up to 200 on the global fallback), and
    memoising on the message text instead would keep reader wording alive in
    process memory across turns and users for no real gain.

    Sharing `target_sections` with the filter means the boost and the filter
    can never disagree about what a question is asking for.
    """
    if not sections:
        return 0.0
    if _base_section(chunk_type) in sections or chunk_type in sections:
        return _SECTION_BOOST
    return 0.0


def spread_across_conditions(
    ranked: list[RetrievedChunk], k: int
) -> list[RetrievedChunk]:
    """Give every matched condition a slot before any condition takes a second.

    "Diabetes" resolves to SEVERAL profiles — type 1, type 2, gestational and
    others all live in the corpus. Ranking is per-chunk and purely lexical, so
    whichever profile happens to word its overview closest to the question can
    take all k slots, and the reader gets an answer about type 1 for a question
    that was not about type 1. That is what was reported, and it reads as the
    assistant simply not knowing about the other documents.

    Round-robin over conditions, preserving rank inside each. With one matched
    condition this is a no-op, so the single-condition case is untouched.

    Pure: no I/O, so it applies to the keyword path and the hybrid path alike.
    """
    if k <= 0 or len(ranked) <= 1:
        return ranked[:k]
    by_condition: dict[str, list[RetrievedChunk]] = {}
    for chunk in ranked:  # `ranked` is already ordered, so each list is too
        by_condition.setdefault(chunk.condition_code, []).append(chunk)
    if len(by_condition) < 2:
        return ranked[:k]

    # Condition order follows each one's BEST chunk, so the strongest match
    # still leads — this spreads the slots, it does not reorder by relevance.
    queues = list(by_condition.values())
    out: list[RetrievedChunk] = []
    round_index = 0
    while len(out) < k and any(len(q) > round_index for q in queues):
        for queue in queues:
            if len(queue) > round_index:
                out.append(queue[round_index])
                if len(out) == k:
                    return out
        round_index += 1
    return out


def _keyword_rank(rows: list[McpChunk], message: str) -> list[RetrievedChunk]:
    query_tokens = set(_tokens(message))
    sections = target_sections(message)
    scored: list[RetrievedChunk] = []
    for r in rows:
        content_tokens = _tokens(r.content)
        overlap = sum(1 for t in content_tokens if t in query_tokens)
        score = overlap / (1 + len(content_tokens)) if content_tokens else 0.0
        score += _section_boost(sections, r.chunk_type)
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
    # ORDER BY makes the candidate SELECTION deterministic: LIMIT without it
    # lets Postgres synchronized seq-scans rotate the pool between calls.
    rows = (
        await db.execute(
            select(McpChunk)
            .where(or_(*conditions))
            .order_by(McpChunk.condition_code, McpChunk.chunk_type, McpChunk.id)
            .limit(GLOBAL_FALLBACK_CANDIDATES)
        )
    ).scalars().all()
    return list(rows)


_VEC_CANDIDATES = 24
_BM25_CANDIDATES = 24
# Per-ranking RRF constants. The semantic ranking gets a much smaller k so its
# head dominates: cosine rank 1 over the whole corpus is a far stronger signal
# than BM25 rank 1 over the crude token-prefiltered pool, and a uniform k=60
# let lexical coincidences that also scraped into the vector tail double-count
# past the true semantic top hit ("ringing sound in my ears" lost tinnitus to
# a chunk that merely contained "sound" and "night").
_RRF_K_SEMANTIC = 10
_RRF_K_LEXICAL = 60
# Section boost sized to flip adjacent semantic head ranks only
# (1/11 − 1/12 ≈ 0.0076).
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
    fused = rrf_fuse(
        [vec_ranking, bm25_ranking], ks=[_RRF_K_SEMANTIC, _RRF_K_LEXICAL]
    )
    sections = target_sections(message)
    scored = [
        RetrievedChunk(
            id=chunk_id,
            condition_code=by_id[chunk_id].condition_code,
            chunk_type=by_id[chunk_id].chunk_type,
            content=by_id[chunk_id].content,
            score=round(
                score
                + (_RRF_SECTION_BOOST
                   if _section_boost(sections, by_id[chunk_id].chunk_type)
                   else 0.0),
                8,
            ),
        )
        for chunk_id, score in fused.items()
    ]
    scored.sort(key=lambda c: (-c.score, c.condition_code, c.chunk_type, c.id))

    # Section filter goes HERE — before the try — so the MMR path and its
    # fail-open `except` branch see the same list. MMR's similarity penalty is
    # section diversity by design, so without this it actively works against a
    # reader who asked for one specific section.
    scored = _prefer_section(scored, sections)

    # --- MMR diversity rerank over the fused shortlist ---
    # The fused top-k often carries near-duplicate chunks (the same section
    # from overview + suggestions). Rerank the top 2k with maximal marginal
    # relevance over the chunk embeddings so the k slots carry k different
    # pieces of information. Fail-open: any surprise keeps the fused order.
    try:
        from app.rag.ranking import mmr_rerank

        shortlist = scored[: max(k * 2, k)]
        vectors = {
            c.id: list(by_id[c.id].embedding)
            for c in shortlist
            if by_id[c.id].embedding is not None
        }
        # Order the WHOLE shortlist, not just k. MMR is asked for a ranking
        # here, not a selection: `spread_across_conditions` does the selecting,
        # and it can only give a second condition a slot if there is a second
        # condition still on the list. Passing k here returned exactly k ids,
        # so when MMR had already filled them from one profile the spread had
        # nothing left to spread and silently did nothing.
        order = mmr_rerank([c.id for c in shortlist], vectors, len(shortlist))
        by_chunk_id = {c.id: c for c in shortlist}
        # MMR cannot do this itself: it never sees condition_code, and its
        # similarity penalty works AGAINST condition coverage — having picked
        # type-1 symptoms, type-2 symptoms is penalised for being about the
        # same thing, while type-1 diagnosis is not. That is MMR working as
        # designed; it is section diversity, not condition diversity.
        return spread_across_conditions(
            [by_chunk_id[cid] for cid in order], k
        )
    except Exception:  # noqa: BLE001 — reranking must never break retrieval
        logger.warning("MMR rerank failed; keeping fused order", exc_info=True)
        return spread_across_conditions(scored, k)



# --------------------------------------------------------------------------- #
# Retrieval cache
#
# "What is diabetes" is asked over and over, by different readers, and the
# answer is the same corpus content every time. Each miss costs DB round trips
# plus — when embeddings are configured — a network call to the embedding
# service, and in production every one of those is a network hop.
#
# ONLY corpus content is cached. The key is (scope, message digest, k) and the
# value is McpChunk-derived text that is identical for every reader, so nothing
# personal is stored and nothing crosses between users: two readers asking the
# same question about the same conditions are entitled to the same profile
# sections. The reader's own data is never part of this — it enters the reply
# later, through the [P] block, which is assembled per turn and never cached.
#
# The message is keyed by DIGEST, not text: a health question is the reader's
# own words, and a process-level dict holding them for five minutes is a
# needless place for them to sit.
#
# Follows the process-level TTL shape already used for the condition index
# (registry.py:238) — and, like it, is reset when the corpus changes.
# ponytail: per-process, so N replicas keep N copies; move to a shared cache
# only if the hit rate ever justifies the operational cost.
# --------------------------------------------------------------------------- #
RETRIEVAL_CACHE_TTL_SECONDS = 300.0
RETRIEVAL_CACHE_MAX_ENTRIES = 512

_retrieval_cache: OrderedDict[tuple, tuple[float, list[RetrievedChunk]]] = (
    OrderedDict()
)


def reset_retrieval_cache() -> None:
    """Drop every cached retrieval. Call whenever mcp_chunks changes."""
    _retrieval_cache.clear()


def _retrieval_key(
    condition_codes: set[str], message: str, k: int
) -> tuple[tuple[str, ...], str, int]:
    digest = hashlib.sha256(
        " ".join(message.lower().split()).encode("utf-8")
    ).hexdigest()[:16]
    return (tuple(sorted(condition_codes)), digest, k)


def _cache_get(key: tuple) -> list[RetrievedChunk] | None:
    hit = _retrieval_cache.get(key)
    if hit is None:
        return None
    stored_at, chunks = hit
    if (time.monotonic() - stored_at) > RETRIEVAL_CACHE_TTL_SECONDS:
        _retrieval_cache.pop(key, None)
        return None
    _retrieval_cache.move_to_end(key)
    # A copy: callers slice and reorder what they are given.
    return list(chunks)


def _cache_put(key: tuple, chunks: list[RetrievedChunk]) -> None:
    _retrieval_cache[key] = (time.monotonic(), list(chunks))
    _retrieval_cache.move_to_end(key)
    while len(_retrieval_cache) > RETRIEVAL_CACHE_MAX_ENTRIES:
        _retrieval_cache.popitem(last=False)


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
    cache_key = _retrieval_key(condition_codes, message, k)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        hybrid = await _hybrid_rank(db, condition_codes, message, k)
        if hybrid:
            _cache_put(cache_key, hybrid)
            return hybrid
    except Exception:  # noqa: BLE001 — hybrid path fails open to keyword
        logger.warning("hybrid retrieval failed; keyword fallback", exc_info=True)

    # `target_sections` is pure and cannot raise, but retrieve_chunks has no
    # outer catch — a throw here is a 500, not a safe reply — so the whole
    # section step stays inside its own guard.
    try:
        sections = target_sections(message)
    except Exception:  # noqa: BLE001 — section intent must never break retrieval
        logger.warning("section-intent parse failed; unfiltered", exc_info=True)
        sections = ()

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
            # The message named a condition the corpus has no profile for.
            # MC051 (Primary Hypertension) is the live example: it is mapped
            # from the HTN engine code by scripts/ingest_mcp_corpus.py:36 but
            # absent from knowledge/mcp/, which jumps MC050 → MC052.
            #
            # Falling through to the global lexical fallback here was tried and
            # REVERTED: it answers the question from a DIFFERENT condition.
            # Every chunk header is literally "<Name> — symptoms:", so the
            # question's own section noun gives overlap > 0 and the score floor
            # below lets any profile through; `_prefer_section` then drops the
            # chunks that do mention the topic for being the wrong section. The
            # measured result was "what is hypertension" answering "What it is
            # — Hyperuricemia (asymptomatic)", rendered verbatim by the
            # extractive path with no model in the loop.
            #
            # No answer is safer than another condition's answer. The real fix
            # is to ingest MC051, not to degrade here.
            logger.info(
                "condition scope matched no chunks; serving no corpus content",
                extra={"condition_codes": sorted(condition_codes)},
            )
            # Cached like any other result: a corpus gap is asked about
            # repeatedly (every hypertension question hits this today), and one
            # fruitless round trip per turn is still a round trip. The TTL
            # bounds how long a newly ingested profile stays invisible, and
            # `reset_retrieval_cache()` clears it immediately after an ingest.
            _cache_put(cache_key, [])
            return []
        # Condition scope already establishes relevance; keep zero-score chunks.
        result = spread_across_conditions(
            _prefer_section(_keyword_rank(rows, message), sections), k
        )
        _cache_put(cache_key, result)
        return result

    rows = await _global_fallback_rows(db, message)
    if not rows:
        return []
    # Unscoped relevance is purely lexical — drop zero-overlap chunks.
    ranked = _prefer_section(_keyword_rank(rows, message), sections)
    result = [c for c in ranked[:k] if c.score > 0]
    _cache_put(cache_key, result)
    return result

"""Embedding client — any OpenAI-compatible /v1/embeddings endpoint.

Configured via EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM. Vectors
are truncated to EMBEDDING_DIM and L2-renormalized: Qwen3-Embedding (and other
Matryoshka-trained models) explicitly support dimension truncation, and our
pgvector column is fixed at vector(1024).

Fail-open: any error returns None and callers fall back to keyword retrieval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

import logging
import math

from app.config import get_settings

logger = logging.getLogger("davi.embeddings")


def embeddings_configured() -> bool:
    s = get_settings()
    return bool(s.embedding_base_url and s.embedding_model)


def _truncate_normalize(vector: list[float], dim: int) -> list[float]:
    v = vector[:dim]
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        return v
    return [x / norm for x in v]


# A single query embedding sits on the CHAT HOT PATH: retrieve_chunks ->
# _hybrid_rank (retrieval.py:372) -> embed_query. Every caller of this module
# fails open to keyword ranking, which is correct — but a flat 180s timeout
# made it a 180-SECOND fail-open, so a cold-starting or overloaded embeddings
# service turned every turn into a multi-minute wait that eventually produced
# the right answer by the slow path. That is the measured cause of the 43s/113s
# turns recorded at orchestrator.py:182.
#
# Batch work (ingest, backfill) legitimately needs a long budget and keeps one;
# an interactive query gets a tight one. Separate connect and read budgets so a
# service that is DOWN fails in ~2s rather than burning the whole read budget.
BATCH_TIMEOUT_S = 180.0
QUERY_CONNECT_S = 2.0
QUERY_READ_S = 6.0


async def embed_texts(
    texts: list[str], *, http_timeout: httpx.Timeout | float | None = None
) -> list[list[float]] | None:
    """Embed a batch. None on any failure (callers fail open to keyword)."""
    if not texts or not embeddings_configured():
        return None
    s = get_settings()
    import httpx  # lazy: the test suite never needs it

    try:
        async with httpx.AsyncClient(
            timeout=BATCH_TIMEOUT_S if http_timeout is None else http_timeout
        ) as client:
            resp = await client.post(
                f"{s.embedding_base_url.rstrip('/')}/embeddings",
                json={"model": s.embedding_model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        # The API may return out of order; sort by index.
        data.sort(key=lambda item: item["index"])
        return [
            _truncate_normalize(item["embedding"], s.embedding_dim)
            for item in data
        ]
    except Exception:  # noqa: BLE001 — embeddings must never break a caller
        logger.warning("embedding request failed", exc_info=True)
        return None


# Qwen3-Embedding-style instruction prefix for QUERIES (documents are
# embedded raw). Improves retrieval on instruct-tuned embedding models and is
# harmless plain text on others.
_QUERY_INSTRUCT = (
    "Instruct: Given a health question, retrieve passages from clinical "
    "condition profiles that answer it\nQuery: "
)


async def embed_query(text: str, instruct: bool = False) -> list[float] | None:
    """Embed ONE query, on an interactive budget.

    Deliberately not the batch timeout: this call blocks a reader waiting for a
    reply, and the caller degrades to keyword ranking on None. Waiting three
    minutes to avoid a slightly worse ranking is the wrong trade.
    """
    import httpx

    payload = _QUERY_INSTRUCT + text if instruct else text
    result = await embed_texts(
        [payload],
        http_timeout=httpx.Timeout(
            QUERY_READ_S,
            connect=QUERY_CONNECT_S,
            read=QUERY_READ_S,
            write=QUERY_CONNECT_S,
            pool=QUERY_CONNECT_S,
        ),
    )
    return result[0] if result else None

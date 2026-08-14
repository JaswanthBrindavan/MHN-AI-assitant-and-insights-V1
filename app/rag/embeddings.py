"""Embedding client — any OpenAI-compatible /v1/embeddings endpoint.

Configured via EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIM. Vectors
are truncated to EMBEDDING_DIM and L2-renormalized: Qwen3-Embedding (and other
Matryoshka-trained models) explicitly support dimension truncation, and our
pgvector column is fixed at vector(1024).

Fail-open: any error returns None and callers fall back to keyword retrieval.
"""

from __future__ import annotations

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


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch. None on any failure (callers fail open to keyword)."""
    if not texts or not embeddings_configured():
        return None
    s = get_settings()
    import httpx  # lazy: the test suite never needs it

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
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
    payload = _QUERY_INSTRUCT + text if instruct else text
    result = await embed_texts([payload])
    return result[0] if result else None

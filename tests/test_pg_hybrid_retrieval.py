"""Hybrid retrieval on real PostgreSQL.

Marked `pg` because it needs a live database with pgvector. Without it,
`_hybrid_rank` short-circuits on the dialect check and the whole path is
untested — which is exactly what drawbacks.md 8.4 describes.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.pg


async def test_the_hybrid_path_is_actually_reached_on_postgres(pg_session, monkeypatch):
    """The point of the whole job: assert _hybrid_rank does NOT return None
    for the dialect reason. On SQLite this test cannot even be meaningful."""
    from app.rag import retrieval

    reached = {"dialect_ok": False}
    real = retrieval._hybrid_rank

    async def _spy(db, codes, message, k):
        bind = db.get_bind()
        reached["dialect_ok"] = getattr(bind, "dialect", None) is not None and (
            bind.dialect.name == "postgresql"
        )
        return await real(db, codes, message, k)

    monkeypatch.setattr(retrieval, "_hybrid_rank", _spy)
    await retrieval.retrieve_chunks(pg_session, set(), "frequent urination and thirst")
    assert reached["dialect_ok"], "the postgres branch was not taken"


async def test_keyword_retrieval_still_works_on_postgres(pg_session):
    """Embeddings are unset in CI, so retrieval must fall back cleanly rather
    than erroring on the real dialect."""
    from app.models.chat import McpChunk
    from app.rag.retrieval import retrieve_chunks

    pg_session.add(
        McpChunk(
            condition_code="MC001",
            chunk_type="symptoms",
            content="Frequent urination and excessive thirst are common symptoms.",
        )
    )
    await pg_session.flush()

    chunks = await retrieve_chunks(pg_session, {"MC001"}, "frequent urination")
    assert chunks
    assert chunks[0].condition_code == "MC001"


async def test_enum_bound_family_consent_query_runs_on_postgres(pg_session):
    """The exclusion lookup compares against a PG enum. SQLite sees a plain
    string and lets anything through, so this only means something here."""
    from app.coredata.service import can_view_document

    # No rows: the call must execute cleanly against the real enum types
    # rather than raising a bind error.
    allowed = await can_view_document(
        pg_session,
        viewer_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        resource_type="reports",
        resource_id=1,
        is_private=False,
    )
    assert allowed in (True, False)

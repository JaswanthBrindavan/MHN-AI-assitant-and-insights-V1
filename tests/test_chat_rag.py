"""Phase 5 — provider + RAG + grounding + receipts via the orchestrator.

Uses the deterministic FakeProvider with scripted answers; no live LLM.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.models.chat import McpChunk, RagTurnReceipt
from scripts.ingest_knowledge import ingest_folder

KNOWLEDGE = Path(__file__).resolve().parent.parent / "knowledge"
USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

DIABETES_Q = "tell me about diabetes and blood sugar"
CLEAN = (
    "Type 2 diabetes relates to blood sugar. An HbA1c above 48 mmol/mol is worth "
    "discussing with a doctor [2]. Everyday habits help too [3]."
)
BAD_MARKER = "An HbA1c above 48 mmol/mol is high [9]."
BAD_MARKER_2 = "An HbA1c above 48 mmol/mol is high [8]."


async def _ingest(db):
    n = await ingest_folder(db, KNOWLEDGE, embed=False)
    await db.commit()
    return n


# --------------------------------------------------------------------------- #
# Retrieval: keyword fallback when embeddings are NULL
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_keyword_fallback_retrieval(db_session):
    n = await _ingest(db_session)
    assert n == 9  # 3 conditions x 3 chunks
    # All embeddings are NULL (no service configured).
    chunks = (await db_session.execute(select(McpChunk))).scalars().all()
    assert all(c.embedding is None for c in chunks)

    provider = FakeProvider(responses=[CLEAN])
    result = await handle_chat(db_session, USER, DIABETES_Q, provider)
    # T2DM chunks were retrieved and used.
    assert result.provenance["used_rag"] is True
    assert result.provenance["conditions"] == ["T2DM"]
    assert len(result.provenance["chunks"]) >= 1


# --------------------------------------------------------------------------- #
# Clean cited answer passes; markers stripped in display
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clean_answer_passes_and_markers_stripped(db_session, set_grounding_mode):
    set_grounding_mode("enforce")
    await _ingest(db_session)
    provider = FakeProvider(responses=[CLEAN])

    result = await handle_chat(db_session, USER, DIABETES_Q, provider)
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"
    assert "[2]" not in result.response_message and "[3]" not in result.response_message
    assert "HbA1c above 48 mmol/mol" in result.response_message
    # No corrective retry was needed.
    assert len(provider.calls) == 1


# --------------------------------------------------------------------------- #
# Invalid marker: log mode keeps answer; enforce retries
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_log_mode_keeps_answer_but_flags(db_session, set_grounding_mode):
    set_grounding_mode("log")
    await _ingest(db_session)
    provider = FakeProvider(responses=[BAD_MARKER])

    result = await handle_chat(db_session, USER, DIABETES_Q, provider)
    assert result.grounding is not None
    assert result.grounding["status"] == "violations"
    # log mode does not retry
    assert len(provider.calls) == 1
    # answer is still returned (markers stripped)
    assert "[9]" not in result.response_message


@pytest.mark.asyncio
async def test_enforce_retries_and_succeeds(db_session, set_grounding_mode):
    set_grounding_mode("enforce")
    await _ingest(db_session)
    provider = FakeProvider(responses=[BAD_MARKER, CLEAN])

    result = await handle_chat(db_session, USER, DIABETES_Q, provider)
    # One corrective retry against the same context.
    assert len(provider.calls) == 2
    assert result.grounding is not None
    assert result.grounding["status"] == "grounded"
    assert "HbA1c above 48 mmol/mol" in result.response_message


@pytest.mark.asyncio
async def test_enforce_retry_failure_falls_back_to_safe_reply(
    db_session, set_grounding_mode
):
    set_grounding_mode("enforce")
    await _ingest(db_session)
    provider = FakeProvider(responses=[BAD_MARKER, BAD_MARKER_2])

    result = await handle_chat(db_session, USER, DIABETES_Q, provider)
    assert len(provider.calls) == 2
    # Degraded to the deterministic safe reply (no fabricated numbers/markers).
    assert "[9]" not in result.response_message
    assert "[8]" not in result.response_message
    assert "clinician" in result.response_message.lower()


# --------------------------------------------------------------------------- #
# Receipts: one row per turn, hashes only
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_receipt_written_with_hash_only(db_session, set_grounding_mode):
    set_grounding_mode("log")
    await _ingest(db_session)
    provider = FakeProvider(responses=[CLEAN])

    await handle_chat(db_session, USER, DIABETES_Q, provider)
    await db_session.commit()

    receipts = (await db_session.execute(select(RagTurnReceipt))).scalars().all()
    assert len(receipts) == 1
    r = receipts[0]
    assert r.query_hash == hashlib.sha256(DIABETES_Q.encode()).hexdigest()
    assert r.used_rag is True
    assert r.grounding_status == "violations" or r.grounding_status == "grounded"
    # The raw message text must not be stored anywhere on the receipt.
    assert DIABETES_Q not in (r.query_hash + (r.model_name or ""))

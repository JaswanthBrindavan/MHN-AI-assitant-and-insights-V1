"""Symptom, conversation, knowledge (RAG), and receipt tables."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import CreatedAt, EmbeddingType, JSONColumn, UUIDPrimaryKey


class SymptomLog(Base, UUIDPrimaryKey, CreatedAt):
    """Per-report triage result."""

    __tablename__ = "symptom_logs"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    symptom: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    matched_terms: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)


class ActiveSymptomState(Base, UUIDPrimaryKey, CreatedAt):
    """A user's currently-active symptoms (dedup on upsert by (user, symptom))."""

    __tablename__ = "active_symptom_states"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "symptom", name="uq_active_symptom"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    symptom: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )


class UserMemory(Base, UUIDPrimaryKey, CreatedAt):
    """Long-term, cross-session memory of what a user has discussed.

    Deduplicated per (user, kind, mem_key); ``mention_count`` + ``last_seen_at``
    track recency and frequency. Stores discussion TOPICS (condition codes +
    display names) and coarse red-flag terms only — never raw message text, so
    no PHI is persisted here (receipts already hash messages).
    """

    __tablename__ = "user_memories"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "kind", "mem_key", name="uq_user_memory"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(sa.String(24), nullable=False)   # condition_topic | flag
    mem_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    value: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    mention_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )


class ConversationSession(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "conversation_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)


class ConversationMessage(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "conversation_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # user|assistant
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    extracted_intent: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)


class ConversationSummary(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "conversation_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    summary: Mapped[dict] = mapped_column(JSONColumn, nullable=False)
    covers_through_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, nullable=True
    )
    token_estimate: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class McpChunk(Base, UUIDPrimaryKey, CreatedAt):
    """Condition knowledge for RAG retrieval."""

    __tablename__ = "mcp_chunks"

    condition_code: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, index=True
    )
    chunk_type: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    embedding = mapped_column(EmbeddingType, nullable=True)
    chunk_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONColumn, nullable=True
    )


class RagTurnReceipt(Base, UUIDPrimaryKey, CreatedAt):
    """Auditable receipt for a single chat turn. Stores hashes, never raw text."""

    __tablename__ = "rag_turn_receipts"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    query_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    retrieved: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    grounding: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    grounding_mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    grounding_status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    used_rag: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

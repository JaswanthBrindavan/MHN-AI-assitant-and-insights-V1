"""Chat endpoints — orchestrated chat, upload triggering, and history."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import (
    ChatMessageInfo,
    ChatRequest,
    ChatResponse,
    ChatSessionInfo,
    ChatUploadRequest,
    ChatUploadResponse,
    UploadedDocumentInfo,
)
from app.auth import authorize_user, get_current_user_id
from app.chat.conversation import add_message, ensure_session, maybe_compact
from app.chat.orchestrator import handle_chat
from app.db import get_db
from app.documents.service import (
    UPLOAD_RESOURCE_TYPE,
    build_upload_reply,
    get_own_unclassified,
    submit_document,
)
from app.llm import get_provider
from app.llm.base import LLMProvider
from app.models.chat import ConversationMessage, ConversationSession

logger = logging.getLogger("davi.chat")

router = APIRouter(tags=["chat"])

SESSION_LIST_LIMIT = 50
MESSAGE_LIST_LIMIT = 200


def get_llm_provider() -> LLMProvider:
    """Injectable provider dependency (overridden in tests)."""
    return get_provider()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> ChatResponse:
    user_id = payload.user_id or current_user
    authorize_user(user_id, current_user)

    result = await handle_chat(
        db, user_id, payload.message, provider, session_id=payload.session_id
    )
    await db.commit()

    return ChatResponse(
        response_message=result.response_message,
        risk_level=result.risk_level,
        recommended_action=result.recommended_action,
        provenance=result.provenance,
        grounding=result.grounding,
        session_id=result.session_id,
        citations=result.citations,
        visual=result.visual,
        language=result.language,
        trace=result.trace,
        documents=result.documents,
    )


@router.post("/chat/upload", response_model=ChatUploadResponse)
async def chat_upload(
    payload: ChatUploadRequest,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ChatUploadResponse:
    """A document was shared in chat: trigger mhn-ai's auto-classifier for it
    and record the exchange in the conversation history.

    Davi does NOTHING with the document itself. The file reaches S3 and the
    ``unclassified_files`` row through Spring's existing upload flow — exactly
    like every other upload in the product — and this endpoint then submits
    the processing run to mhn-ai the same way Spring does. Davi only reads
    the row to check it belongs to the caller.
    """
    row = await get_own_unclassified(db, current_user, payload.document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    filename = row.name or row.filepath.rsplit("/", 1)[-1]

    result = await submit_document(db, current_user, row.id)
    reply = build_upload_reply(filename)

    # The upload is a conversation turn: both sides land in history so
    # follow-ups ("what was in that report?") have context.
    sid = await ensure_session(db, current_user, payload.session_id)
    user_text = f"[uploaded file: {filename}]"
    if payload.message.strip():
        user_text += f" {payload.message.strip()}"
    await add_message(db, sid, "user", user_text[:4000])
    await add_message(db, sid, "assistant", reply)
    await maybe_compact(db, sid)
    await db.commit()

    return ChatUploadResponse(
        response_message=reply,
        session_id=sid,
        document=UploadedDocumentInfo(
            resource_type=UPLOAD_RESOURCE_TYPE,
            doc_id=row.id,
            state="pending",
            triggered=result.accepted,
            run_id=result.run_id,
            item_status=result.item_status,
            trigger_reason=result.reason,
        ),
    )


@router.get("/chat/sessions", response_model=list[ChatSessionInfo])
async def list_sessions(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSessionInfo]:
    """The user's conversation sessions, most recently active first."""
    stats = (
        select(
            ConversationMessage.session_id,
            func.count(ConversationMessage.id).label("n"),
            func.max(ConversationMessage.created_at).label("last_at"),
        )
        .group_by(ConversationMessage.session_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ConversationSession, stats.c.n, stats.c.last_at)
            .join(stats, stats.c.session_id == ConversationSession.id,
                  isouter=True)
            .where(ConversationSession.user_id == current_user)
            .order_by(
                func.coalesce(
                    stats.c.last_at, ConversationSession.created_at
                ).desc()
            )
            .limit(SESSION_LIST_LIMIT)
        )
    ).all()

    session_ids = [s.id for s, _, _ in rows]
    previews: dict[uuid.UUID, str] = {}
    if session_ids:
        first_msgs = (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id.in_(session_ids),
                    ConversationMessage.role == "user",
                )
                .order_by(
                    ConversationMessage.created_at, ConversationMessage.id
                )
            )
        ).scalars().all()
        for m in first_msgs:
            previews.setdefault(m.session_id, m.message[:80])

    return [
        ChatSessionInfo(
            session_id=s.id,
            created_at=s.created_at,
            last_message_at=last_at,
            message_count=int(n or 0),
            preview=previews.get(s.id, ""),
        )
        for s, n, last_at in rows
    ]


@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=list[ChatMessageInfo],
)
async def list_messages(
    session_id: uuid.UUID,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessageInfo]:
    """Full message history of one session (owner only)."""
    session = (
        await db.execute(
            select(ConversationSession).where(
                ConversationSession.id == session_id
            )
        )
    ).scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    authorize_user(session.user_id, current_user)

    messages = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.created_at, ConversationMessage.id)
            .limit(MESSAGE_LIST_LIMIT)
        )
    ).scalars().all()
    return [
        ChatMessageInfo(
            id=m.id, role=m.role, message=m.message, created_at=m.created_at,
            # User turns keep their intent private (it holds triage internals);
            # assistant extras are exactly what the client needs to rebuild
            # cards on restore.
            meta=m.extracted_intent if m.role == "assistant" else None,
        )
        for m in messages
    ]

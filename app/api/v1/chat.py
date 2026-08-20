"""Chat endpoints — orchestrated chat, file uploads, and history retrieval."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import (
    ChatMessageInfo,
    ChatRequest,
    ChatResponse,
    ChatSessionInfo,
    ChatUploadResponse,
    UploadedDocumentInfo,
)
from app.auth import authorize_user, get_current_user_id
from app.chat.conversation import add_message, ensure_session, maybe_compact
from app.chat.orchestrator import handle_chat
from app.config import get_settings
from app.db import get_db
from app.documents.service import build_upload_reply, store_and_trigger
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


def _save_upload_bytes(filename: str, data: bytes) -> str:
    """Persist upload bytes under upload_dir; the returned path is the opaque
    storage key (the chassis/dev stand-in for an S3 key). Falls back to the
    bare filename when the disk write fails — filing must still succeed."""
    try:
        upload_dir = Path(get_settings().upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix[:16]
        key = f"{uuid.uuid4().hex}{suffix}"
        path = upload_dir / key
        path.write_bytes(data)
        return str(path)
    except Exception:  # noqa: BLE001 — storage is best-effort in the chassis
        logger.warning("upload byte storage failed; filing metadata only",
                       exc_info=True)
        return filename


@router.post("/chat/upload", response_model=ChatUploadResponse)
async def chat_upload(
    file: UploadFile = File(...),
    message: str = Form(""),
    session_id: str = Form(""),
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ChatUploadResponse:
    """Upload a file in chat: store it, trigger mhn-ai's auto-classifier
    (classify → file → extract runs THERE), and record the exchange in the
    conversation history."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail="File too large")
    filename = (file.filename or "upload").rsplit("/", 1)[-1][:200]

    parsed_session: uuid.UUID | None = None
    if session_id:
        try:
            parsed_session = uuid.UUID(session_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid session_id"
            ) from exc

    filepath = _save_upload_bytes(filename, data)
    stored = await store_and_trigger(db, current_user, data, filepath)
    reply = build_upload_reply(filename, stored.triggered)

    # The upload is a conversation turn: both sides land in history so
    # follow-ups ("what was in that report?") have context.
    sid = await ensure_session(db, current_user, parsed_session)
    user_text = f"[uploaded file: {filename}]"
    if message.strip():
        user_text += f" {message.strip()}"
    await add_message(db, sid, "user", user_text[:4000])
    await add_message(db, sid, "assistant", reply)
    await maybe_compact(db, sid)
    await db.commit()

    return ChatUploadResponse(
        response_message=reply,
        session_id=sid,
        document=UploadedDocumentInfo(
            resource_type=stored.resource_type,
            doc_id=stored.doc_id,
            state="pending",
            triggered=stored.triggered,
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
            id=m.id, role=m.role, message=m.message, created_at=m.created_at
        )
        for m in messages
    ]

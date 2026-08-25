"""Chat endpoints — orchestrated chat, upload triggering, and history."""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
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
from app.chat.replies import (
    EMERGENCY_DIRECTIVE,
    HIGH_ESCALATION,
    SELF_HARM_REPLY,
    safe_reply,
)
from app.chat.streaming import validated_stream
from app.chat.validation import validate_reply
from app.db import get_db
from app.documents.service import (
    UPLOAD_RESOURCE_TYPE,
    build_upload_reply,
    get_own_unclassified,
    submit_document,
)
from app.i18n.language import LANGUAGE_NAMES
from app.llm import get_provider
from app.llm.base import LLMProvider
from app.models.chat import ConversationMessage, ConversationSession
from app.triage.red_flags import EMERGENCY, HIGH, NONE, triage
from app.voice.service import MAX_AUDIO_BYTES, Transcript, audio_acceptable
from app.voice.service import get_sidecar as get_voice_sidecar

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


def _sse(event: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> StreamingResponse:
    """Streamed chat.

    The reply is produced by the SAME pipeline as POST /chat — triage floor,
    emergency directive, tools, guards — and then delivered incrementally.
    Nothing is streamed that has not passed the banned-phrase check, and the
    whole-answer guards can still retract with a `replace` event.

    Deterministic paths (emergency, scope decline, greeting) arrive as a single
    delta: there is nothing to gain from typing out an emergency directive one
    token at a time.

    Event contract:
        event: delta    data: {"type":"delta","text":"..."}     append
        event: replace  data: {"type":"replace","text":"..."}   discard + show
        event: done     data: {"type":"done", ...}              metadata
    """
    user_id = payload.user_id or current_user
    authorize_user(user_id, current_user)

    result = await handle_chat(
        db, user_id, payload.message, provider, session_id=payload.session_id
    )
    await db.commit()

    async def _events():
        try:
            # The answer is already fully guarded; stream it sentence by
            # sentence so the client can render progressively.
            async for event in validated_stream(
                _sentences_of(result.response_message),
                risk_level=result.risk_level,
                safe_fallback=safe_reply(result.risk_level, result.session_id),
            ):
                yield _sse(event)
            yield _sse(
                {
                    "type": "done",
                    "risk_level": result.risk_level,
                    "recommended_action": result.recommended_action,
                    "session_id": str(result.session_id),
                    "provenance": result.provenance,
                    "citations": result.citations,
                    "documents": result.documents,
                    "visual": result.visual,
                    "language": result.language,
                    "trace": result.trace,
                }
            )
        except Exception:  # noqa: BLE001 — a stream must never 500 mid-flight
            logger.warning("chat stream failed", exc_info=True)
            yield _sse(
                {
                    "type": "replace",
                    "text": safe_reply(result.risk_level, result.session_id),
                    "reason": "stream_error",
                }
            )

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sentences_of(text: str):
    """Chunk a finished reply for progressive delivery."""
    for piece in re.split(r"(?<=[.!?])(\s+)", text):
        if piece:
            yield piece


class ChatVoiceRequest(BaseModel):
    """A voice note. Audio is base64 so the payload stays plain JSON.

    Every field is bounded at the schema, so an oversized body is rejected
    before it is decoded rather than after — the post-decode byte check stays
    as the exact backstop.

    There is deliberately NO `confirmed` flag. A client that has been shown a
    transcript and agrees with it should POST that TEXT to /api/v1/chat, which
    already bounds length, sanitises control characters, runs the triage floor
    and validates the reply. Re-sending the audio would re-run ASR, and
    sampling decoders are not deterministic — the text acted on need not be
    the text the reader saw.
    """

    # base64 expands by 4/3; the exact byte cap is enforced after decoding.
    audio: str = Field(max_length=(MAX_AUDIO_BYTES * 4 // 3) + 8)
    content_type: str = Field(default="audio/ogg", max_length=64)
    language_hint: str = Field(default="", max_length=16)
    session_id: uuid.UUID | None = None


# A literal newline, kept out of the f-string soup below for readability.
NEWLINE = chr(10)


def _safe_confirmation(transcript) -> str:
    """The confirmation question, guaranteed to pass the output validator.

    The prompt quotes raw ASR output, which is a model's guess and untrusted
    from the same direction as vision output. A transcript of "you probably
    have dengue" would otherwise be echoed to the reader verbatim, banned
    phrasing and all. On failure it falls back to the no-text branch, which
    quotes nothing.
    """
    prompt = transcript.confirmation_prompt()
    if validate_reply(prompt, NONE).ok:
        return prompt
    return Transcript(
        "", transcript.language, transcript.confidence
    ).confirmation_prompt()


@router.post("/chat/voice", response_model=ChatResponse)
async def chat_voice(
    payload: ChatVoiceRequest,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
) -> ChatResponse:
    """A spoken message.

    Transcription happens FIRST, and the transcript then enters the SAME
    pipeline as a typed message — triage floor included. There is no separate
    voice path, so a spoken red flag cannot bypass the safety design by virtue
    of the input method.

    A low-confidence transcript is offered back for confirmation instead of
    being acted on: "I can breathe" and "I can't breathe" differ by one
    phoneme and by everything else. The client re-sends with confirmed=true
    once the reader agrees.
    """
    sidecar = get_voice_sidecar()
    if sidecar is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice is not configured",
        )

    try:
        audio = base64.standard_b64decode(payload.audio)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Audio is not valid base64"
        ) from exc

    ok, reason = audio_acceptable(payload.content_type, len(audio))
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=reason
        )

    transcript = await sidecar.transcribe(
        audio, payload.content_type, payload.language_hint
    )
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not transcribe the audio",
        )

    # THE FLOOR IS A FLOOR. It runs on what was heard, even when we are about
    # to ask whether we heard it right.
    #
    # The first shape of this endpoint returned the confirmation question with
    # risk_level=NONE, which is not "the floor did not run" — it is LOWERING
    # it, the one thing a floor forbids. And the asymmetry ran the wrong way:
    # ASR confidence collapses on breathless, slurred, panicked or pained
    # speech, so the gate fired hardest on exactly the people it most needed to
    # protect. A spoken "I cannot breathe" got a chatty clarification question.
    tr = triage(transcript.text)
    if tr.level in (EMERGENCY, HIGH):
        sid = await ensure_session(db, current_user, payload.session_id)
        await db.commit()
        directive = (
            SELF_HARM_REPLY
            if tr.self_harm
            else (EMERGENCY_DIRECTIVE if tr.level == EMERGENCY else HIGH_ESCALATION)
        )
        message = directive
        if not transcript.confident:
            # Escalate FIRST, then check we heard right. Never the reverse.
            message = directive + NEWLINE + NEWLINE + _safe_confirmation(transcript)
        return ChatResponse(
            response_message=message,
            risk_level=tr.level,
            recommended_action=(
                "call_emergency_services"
                if tr.level == EMERGENCY
                else "seek_care_promptly"
            ),
            provenance={
                "path": "voice_triage_floor",
                "confidence": round(transcript.confidence, 3),
                "language": transcript.language,
            },
            session_id=sid,
            language=transcript.language,
            trace=[
                {"step": "Transcription",
                 "detail": "heard a red flag — escalating before anything else"}
            ],
        )

    # Below HIGH, ask rather than guess: "I can breathe" and "I cannot breathe"
    # differ by one phoneme. The pipeline is NOT run on words nobody is sure of.
    if not transcript.confident:
        sid = await ensure_session(db, current_user, payload.session_id)
        await db.commit()
        return ChatResponse(
            response_message=_safe_confirmation(transcript),
            risk_level=tr.level,
            recommended_action="confirm_transcript",
            provenance={
                "path": "voice_confirm",
                "confidence": round(transcript.confidence, 3),
                "language": transcript.language,
            },
            session_id=sid,
            language=transcript.language,
            trace=[
                {"step": "Transcription",
                 "detail": "not confident enough to answer — checking first"}
            ],
        )

    # Bound the text the way every other entry point does. ChatRequest.message
    # is min_length=1 / max_length=4000; a transcript must not be the one way
    # around that.
    text = transcript.text.strip()[:4000]
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing was heard in the audio",
        )

    # From here it is an ordinary turn. The triage floor sees the transcript.
    result = await handle_chat(
        db, current_user, text, provider, session_id=payload.session_id
    )
    await db.commit()

    result.trace.insert(
        0,
        {"step": "Transcription",
         "detail": f"heard as {LANGUAGE_NAMES.get(transcript.language, transcript.language)}"},
    )
    result.provenance["transcript_confidence"] = round(transcript.confidence, 3)

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

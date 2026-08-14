"""Chat endpoint — orchestrated deterministic-floor → RAG → LLM → guardrails."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ChatRequest, ChatResponse
from app.auth import authorize_user, get_current_user_id
from app.chat.orchestrator import handle_chat
from app.db import get_db
from app.llm import get_provider
from app.llm.base import LLMProvider

router = APIRouter(tags=["chat"])


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
    )

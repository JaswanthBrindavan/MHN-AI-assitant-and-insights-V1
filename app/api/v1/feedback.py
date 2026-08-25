"""Feedback endpoints — and the path from a bad reply to a regression case.

The loop only closes if a down-vote can become a test. `GET /feedback/review`
exists so a maintainer can see what readers actually disliked, and
`POST /feedback/{id}/triage` marks one handled once it has been turned into a
quality case. Without that second half this is a suggestion box.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authorize_user, get_current_user_id
from app.db import get_db
from app.models.chat import ConversationMessage, ConversationSession
from app.models.common import utcnow
from app.models.feedback import RATINGS, REASONS, TurnFeedback
from app.telemetry import feedback_received

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    message_id: uuid.UUID
    rating: str
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    id: uuid.UUID
    rating: str
    reason: str | None = None
    recorded: bool = True


class ReviewItem(BaseModel):
    """One down-voted turn, with what is needed to reconstruct it."""

    id: uuid.UUID
    rating: str
    reason: str | None
    comment: str | None
    question: str | None
    reply: str | None
    session_id: uuid.UUID | None
    triaged: bool


@router.post("", response_model=FeedbackOut)
async def submit_feedback(
    payload: FeedbackIn,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> FeedbackOut:
    """Record a verdict on one assistant turn.

    Sending it again is a CORRECTION, not a second vote — a reader who changes
    their mind should be able to, and counting both would skew the very numbers
    this exists to produce.
    """
    if payload.rating not in RATINGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"rating must be one of {list(RATINGS)}",
        )
    if payload.reason is not None and payload.reason not in REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"reason must be one of {list(REASONS)}",
        )

    message = (
        await db.execute(
            select(ConversationMessage).where(
                ConversationMessage.id == payload.message_id
            )
        )
    ).scalars().first()
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # OWNERSHIP FIRST, then shape. Checking the role first would tell a caller
    # holding somebody else's message id whether that turn was theirs or the
    # assistant's before refusing them — a small oracle, and free to close.
    session = (
        await db.execute(
            select(ConversationSession).where(
                ConversationSession.id == message.session_id
            )
        )
    ).scalars().first()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    authorize_user(session.user_id, current_user)

    if message.role != "assistant":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feedback applies to assistant replies",
        )

    existing = (
        await db.execute(
            select(TurnFeedback).where(
                TurnFeedback.user_id == current_user,
                TurnFeedback.message_id == payload.message_id,
            )
        )
    ).scalars().first()

    if existing is not None:
        existing.rating = payload.rating
        existing.reason = payload.reason
        existing.comment = payload.comment
        row = existing
    else:
        row = TurnFeedback(
            user_id=current_user,
            message_id=payload.message_id,
            session_id=message.session_id,
            rating=payload.rating,
            reason=payload.reason,
            comment=payload.comment,
        )
        db.add(row)

    await db.flush()
    await db.commit()
    feedback_received.inc(rating=payload.rating, reason=payload.reason or "none")
    return FeedbackOut(id=row.id, rating=row.rating, reason=row.reason)


@router.get("/review", response_model=list[ReviewItem])
async def review_queue(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    untriaged_only: bool = True,
    # Bounded at both ends: a negative limit reaches SQLite as `LIMIT -1`,
    # which means no limit at all.
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ReviewItem]:
    """Down-voted turns, with the question and the reply beside them.

    Scoped to the caller's own feedback. A cross-user maintainer view is a
    different thing with different authorization — it belongs with the
    clinician review queue (Task 24), not bolted on here.
    """
    stmt = (
        select(TurnFeedback)
        .where(
            TurnFeedback.user_id == current_user,
            TurnFeedback.rating == "down",
        )
        .order_by(TurnFeedback.created_at.desc())
        .limit(limit)
    )
    if untriaged_only:
        stmt = stmt.where(TurnFeedback.triaged_at.is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    items: list[ReviewItem] = []
    for row in rows:
        reply = (
            await db.execute(
                select(ConversationMessage).where(
                    ConversationMessage.id == row.message_id
                )
            )
        ).scalars().first()

        question = None
        if reply is not None:
            # The user turn immediately before it — what was actually asked.
            question_row = (
                await db.execute(
                    select(ConversationMessage)
                    .where(
                        ConversationMessage.session_id == reply.session_id,
                        ConversationMessage.role == "user",
                        ConversationMessage.created_at <= reply.created_at,
                    )
                    .order_by(ConversationMessage.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            question = question_row.message if question_row else None

        items.append(
            ReviewItem(
                id=row.id,
                rating=row.rating,
                reason=row.reason,
                comment=row.comment,
                question=question,
                reply=reply.message if reply else None,
                session_id=row.session_id,
                triaged=row.triaged_at is not None,
            )
        )
    return items


@router.post("/{feedback_id}/triage", response_model=FeedbackOut)
async def mark_triaged(
    feedback_id: uuid.UUID,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> FeedbackOut:
    """Mark one item handled — it has become a quality case.

    This is the half that turns a suggestion box into a loop.
    """
    row = (
        await db.execute(
            select(TurnFeedback).where(TurnFeedback.id == feedback_id)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    authorize_user(row.user_id, current_user)

    row.triaged_at = utcnow()
    await db.flush()
    await db.commit()
    return FeedbackOut(id=row.id, rating=row.rating, reason=row.reason)


@router.get("/summary")
async def summary(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Counts by rating and reason, for the caller."""
    rows = (
        await db.execute(
            select(
                TurnFeedback.rating,
                TurnFeedback.reason,
                func.count(TurnFeedback.id),
            )
            .where(TurnFeedback.user_id == current_user)
            .group_by(TurnFeedback.rating, TurnFeedback.reason)
        )
    ).all()
    by_rating: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for rating, reason, count in rows:
        by_rating[rating] = by_rating.get(rating, 0) + int(count)
        if reason:
            by_reason[reason] = by_reason.get(reason, 0) + int(count)
    return {"by_rating": by_rating, "by_reason": by_reason}

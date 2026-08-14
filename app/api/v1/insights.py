"""Insights read endpoint. Reads NEVER compute; they only serve artifacts."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import InsightOut
from app.auth import authorize_user, get_current_user_id
from app.db import get_db
from app.models.rules import InsightArtifact

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=list[InsightOut])
async def get_insights(
    user_id: uuid.UUID | None = None,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[InsightOut]:
    effective = user_id or current_user
    authorize_user(effective, current_user)

    # Active only — held_for_review (sensitive) artifacts are never surfaced.
    rows = (
        await db.execute(
            select(InsightArtifact)
            .where(
                InsightArtifact.user_id == effective,
                InsightArtifact.status == "active",
            )
            .order_by(InsightArtifact.condition_code)
        )
    ).scalars().all()

    return [
        InsightOut(
            id=r.id,
            condition_code=r.condition_code,
            tier=r.tier,
            title=r.title,
            body=r.body,
            status=r.status,
            template_key=r.template_key,
            template_version=r.template_version,
            pipeline_version=r.pipeline_version,
            content_hash=r.content_hash,
        )
        for r in rows
    ]

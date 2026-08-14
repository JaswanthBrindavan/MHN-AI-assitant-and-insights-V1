"""Pedigree endpoints. Every write triggers a synchronous recompute."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import (
    ConditionOut,
    MemberOut,
    PedigreeOut,
    PedigreePut,
)
from app.auth import authorize_user, get_current_user_id
from app.db import get_db
from app.insights.engine import recompute_insights
from app.models.core import PedigreeCondition, PedigreeMember
from app.services.pedigree import (
    get_or_create_family_risk_grant,
    upsert_condition,
    upsert_member,
)

router = APIRouter(prefix="/pedigree", tags=["pedigree"])


@router.put("", status_code=status.HTTP_200_OK)
async def put_pedigree(
    payload: PedigreePut,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    user_id = payload.user_id or current_user
    authorize_user(user_id, current_user)

    grant = await get_or_create_family_risk_grant(db, user_id, source="api_put_pedigree")

    for member in payload.members:
        await upsert_member(
            db, user_id, member.slot, member.vital_status, member.cause_of_death
        )
        for cond in member.conditions:
            await upsert_condition(
                db,
                user_id,
                member.slot,
                cond.condition_code,
                cond.condition_display,
                cond.onset_band,
                cond.certainty,
                cond.provenance,
                grant.id,
            )

    created = await recompute_insights(db, user_id, reason="pedigree_write")
    await db.commit()
    return {"user_id": str(user_id), "insights_created": len(created)}


@router.get("", response_model=PedigreeOut)
async def get_pedigree(
    user_id: uuid.UUID | None = None,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> PedigreeOut:
    effective = user_id or current_user
    authorize_user(effective, current_user)

    members = (
        await db.execute(
            select(PedigreeMember).where(PedigreeMember.user_id == effective)
        )
    ).scalars().all()
    conditions = (
        await db.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.user_id == effective,
                PedigreeCondition.soft_deleted.is_(False),
            )
        )
    ).scalars().all()

    return PedigreeOut(
        user_id=effective,
        members=[
            MemberOut(
                slot=m.slot,
                vital_status=m.vital_status,
                cause_of_death=m.cause_of_death,
            )
            for m in members
        ],
        conditions=[
            ConditionOut(
                id=c.id,
                slot=c.slot,
                condition_code=c.condition_code,
                condition_display=c.condition_display,
                onset_band=c.onset_band,
                certainty=c.certainty,
                provenance=c.provenance,
            )
            for c in conditions
        ],
    )


@router.delete("/conditions/{condition_id}", status_code=status.HTTP_200_OK)
async def delete_condition(
    condition_id: uuid.UUID,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(
            select(PedigreeCondition).where(PedigreeCondition.id == condition_id)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # Object-level authorization against the row's owner.
    authorize_user(row.user_id, current_user)

    row.soft_deleted = True
    await db.flush()
    await recompute_insights(db, row.user_id, reason="condition_deleted")
    await db.commit()
    return {"deleted": str(condition_id)}

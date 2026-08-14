"""Pedigree write helpers + consent recording, shared by the API and seeds."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import ConsentLedger, PedigreeCondition, PedigreeMember

FAMILY_RISK_PURPOSE = "family_risk_analysis"


async def get_or_create_family_risk_grant(
    db: AsyncSession, user_id: uuid.UUID, source: str
) -> ConsentLedger:
    """Return the user's active family-risk consent grant, creating it once.

    The ledger is append-only: we only ever INSERT a `granted` event; we never
    update or delete existing rows.
    """
    existing = (
        await db.execute(
            select(ConsentLedger)
            .where(
                ConsentLedger.user_id == user_id,
                ConsentLedger.purpose == FAMILY_RISK_PURPOSE,
                ConsentLedger.action == "granted",
            )
            .order_by(ConsentLedger.created_at.desc())
        )
    ).scalars().first()
    if existing is not None:
        return existing

    grant = ConsentLedger(
        user_id=user_id,
        purpose=FAMILY_RISK_PURPOSE,
        action="granted",
        scope={"conditions": "all", "relatives": "pedigree"},
        source=source,
    )
    db.add(grant)
    await db.flush()
    return grant


async def upsert_member(
    db: AsyncSession,
    user_id: uuid.UUID,
    slot: str,
    vital_status: str | None,
    cause_of_death: str | None,
) -> PedigreeMember:
    member = (
        await db.execute(
            select(PedigreeMember).where(
                PedigreeMember.user_id == user_id, PedigreeMember.slot == slot
            )
        )
    ).scalars().first()
    if member is None:
        member = PedigreeMember(user_id=user_id, slot=slot)
        db.add(member)
    member.vital_status = vital_status
    member.cause_of_death = cause_of_death
    await db.flush()
    return member


async def upsert_condition(
    db: AsyncSession,
    user_id: uuid.UUID,
    slot: str,
    condition_code: str,
    condition_display: str,
    onset_band: str,
    certainty: str,
    provenance: str,
    consent_grant_id: uuid.UUID | None,
) -> PedigreeCondition:
    """Upsert one (user, slot, condition) row; un-deletes a soft-deleted match."""
    row = (
        await db.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.user_id == user_id,
                PedigreeCondition.slot == slot,
                PedigreeCondition.condition_code == condition_code,
            )
        )
    ).scalars().first()
    if row is None:
        row = PedigreeCondition(
            user_id=user_id, slot=slot, condition_code=condition_code
        )
        db.add(row)
    row.condition_display = condition_display
    row.onset_band = onset_band
    row.certainty = certainty
    row.provenance = provenance
    row.consent_grant_id = consent_grant_id
    row.soft_deleted = False
    await db.flush()
    return row

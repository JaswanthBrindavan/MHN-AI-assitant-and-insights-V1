"""Seed 3 synthetic users whose pedigrees exercise every rule branch.

Synthetic data only — fake users, fixed UUIDs (so golden snapshots are stable).
Idempotent: clears each synthetic user's pedigree before re-seeding, then
recomputes. Also seeds rules + templates.

Run:  python -m scripts.seed_synthetic
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.insights.engine import recompute_insights
from app.models.core import PedigreeCondition, PedigreeMember, User
from app.services.pedigree import (
    get_or_create_family_risk_grant,
    upsert_condition,
    upsert_member,
)
from scripts.seed_rules_templates import seed_rules_and_templates

# Fixed synthetic identities.
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")  # one-parent DM + HTN
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")  # both-parents DM
USER_C = uuid.UUID("33333333-3333-3333-3333-333333333333")  # early+vertical DM, CAD

DM = ("T2DM", "type 2 diabetes")
HTN = ("HTN", "high blood pressure")
CAD = ("CAD", "coronary artery disease")

# member conditions: (slot, (code, display), onset_band, certainty)
SYNTHETIC: dict[uuid.UUID, list[tuple]] = {
    USER_A: [
        ("mother", DM, "55_59", "confirmed"),
        ("father", HTN, "50_54", "confirmed"),
    ],
    USER_B: [
        ("mother", DM, "60_64", "confirmed"),
        ("father", DM, "55_59", "confirmed"),
    ],
    USER_C: [
        ("mother", DM, "40_44", "confirmed"),
        ("grandmother_maternal", DM, "65_69", "as_far_as_i_know"),
        ("father", CAD, "50_54", "confirmed"),
    ],
}

_USER_META = {
    USER_A: ("Asha Synthetic", "asha"),
    USER_B: ("Bharat Synthetic", "bharat"),
    USER_C: ("Chandra Synthetic", "chandra"),
}


async def _ensure_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    existing = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalars().first()
    if existing is not None:
        return
    name, handle = _USER_META[user_id]
    db.add(
        User(
            id=user_id,
            name=name,
            email=f"{handle}@synthetic.example",
            user_name=f"synthetic_{handle}",
            health_card_number=f"SYN-{handle.upper()}",
            hashcode=f"synthetic-hashcode-{handle}",
        )
    )
    await db.flush()


async def seed_synthetic(db: AsyncSession) -> None:
    await seed_rules_and_templates(db)

    for user_id, rows in SYNTHETIC.items():
        await _ensure_user(db, user_id)
        # Idempotency: wipe this synthetic user's pedigree before re-seeding.
        await db.execute(
            delete(PedigreeCondition).where(PedigreeCondition.user_id == user_id)
        )
        await db.execute(
            delete(PedigreeMember).where(PedigreeMember.user_id == user_id)
        )
        grant = await get_or_create_family_risk_grant(
            db, user_id, source="seed_synthetic"
        )
        for slot, (code, display), onset, certainty in rows:
            await upsert_member(db, user_id, slot, "alive", None)
            await upsert_condition(
                db, user_id, slot, code, display, onset, certainty,
                "self_report", grant.id,
            )
        await recompute_insights(db, user_id, reason="seed_synthetic")


async def _main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        await seed_synthetic(db)
        await db.commit()
    print("Seeded 3 synthetic users (A: one-parent DM+HTN, B: both-parents DM, "
          "C: early+vertical DM + premature CAD).")


if __name__ == "__main__":
    asyncio.run(_main())

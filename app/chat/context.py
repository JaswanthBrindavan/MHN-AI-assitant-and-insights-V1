"""Build patient-context ([P]) text and condition scope from stored data.

Reads only — never computes insights. Serves what recompute already persisted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.core import PedigreeCondition
from app.models.rules import InsightArtifact


async def build_patient_context(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[str, set[str]]:
    """Return (patient_context_text, condition_codes) for a user.

    The text is a short, de-identified summary of family-history conditions and
    active insight tiers, suitable for the [P] block. Condition codes are used
    to scope retrieval.
    """
    conditions = (
        await db.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.user_id == user_id,
                PedigreeCondition.soft_deleted.is_(False),
            )
        )
    ).scalars().all()
    insights = (
        await db.execute(
            select(InsightArtifact).where(
                InsightArtifact.user_id == user_id,
                InsightArtifact.status == "active",
            )
        )
    ).scalars().all()

    codes: set[str] = {c.condition_code for c in conditions}
    codes |= {a.condition_code for a in insights}

    if not conditions and not insights:
        return "", codes

    displays = sorted({c.condition_display for c in conditions})
    lines: list[str] = []
    if displays:
        lines.append("Family history on record includes: " + ", ".join(displays) + ".")
    if insights:
        tiers = sorted({f"{a.condition_code} ({a.tier})" for a in insights})
        lines.append("Active family-history insights: " + ", ".join(tiers) + ".")
    return " ".join(lines), codes

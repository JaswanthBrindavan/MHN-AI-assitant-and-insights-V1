"""Core-data services: documents, family access, vitals/metrics, lifestyle.

All reads honour the core app's privacy model: a family member's documents are
only visible when the family link is accepted, the owner's file-share flag is
on, and the document is not private. Tracker writes go to ``lifestyle_log`` —
the same table the core app writes — on the user's behalf.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.coredata import (
    BodyMeasurement,
    FamilyConnect,
    LifestyleLog,
    MedicineTracking,
    Prescription,
    Relation,
    Report,
    ScanImaging,
    Vaccination,
    VitalReading,
)

# Document kind → (model, human label). Order = search order for "any test".
DOCUMENT_KINDS: dict[str, tuple[type, str]] = {
    "report": (Report, "report"),
    "scan": (ScanImaging, "scan / imaging"),
    "prescription": (Prescription, "prescription"),
    "vaccination": (Vaccination, "vaccination record"),
}


@dataclass(frozen=True)
class DocumentHit:
    kind: str
    doc_id: int
    filepath: str
    created_at: datetime | None
    owner_label: str  # "you" or the relation name


# --------------------------------------------------------------------------- #
# Family resolution
# --------------------------------------------------------------------------- #
async def resolve_family_member(
    db: AsyncSession, user_id: uuid.UUID, relation_term: str
) -> uuid.UUID | None:
    """Resolve "father"/"mother"/… to a connected user id, honouring consent.

    Only accepted links where the OTHER party's file-share flag is on qualify.
    The relation name is matched from the requester's perspective (relations
    row) or its inverse for the acceptor side.
    """
    term = relation_term.strip().lower()
    rows = (
        await db.execute(
            select(FamilyConnect, Relation)
            .join(Relation, FamilyConnect.relation_id == Relation.id, isouter=True)
            .where(
                FamilyConnect.accepted.is_(True),
                (FamilyConnect.requester_id == user_id)
                | (FamilyConnect.acceptor_id == user_id),
            )
        )
    ).all()
    for fc, rel in rows:
        if rel is None:
            continue
        if fc.requester_id == user_id:
            # relations.name describes the acceptor from the requester's view.
            other, other_shares = fc.acceptor_id, bool(fc.acc_file_share)
            name = rel.name
        else:
            other, other_shares = fc.requester_id, bool(fc.req_file_share)
            name = rel.inverse
        if other_shares and term in name.strip().lower():
            return other
    return None


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
async def latest_documents(
    db: AsyncSession,
    owner_id: uuid.UUID,
    kinds: list[str],
    *,
    owner_label: str = "you",
    include_private: bool = True,
    limit: int = 3,
) -> list[DocumentHit]:
    """Newest documents of the requested kinds for one owner.

    ``include_private=False`` is used for family members' documents.
    """
    hits: list[DocumentHit] = []
    for kind in kinds:
        model, _label = DOCUMENT_KINDS[kind]
        query = select(model).where(model.user_id == owner_id)  # type: ignore[attr-defined]
        if not include_private:
            query = query.where(model.private.is_not(True))  # type: ignore[attr-defined]
        rows = (
            await db.execute(
                query.order_by(model.created_at.desc().nulls_last(), model.id.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
        ).scalars().all()
        for r in rows:
            hits.append(
                DocumentHit(
                    kind=kind,
                    doc_id=r.id,
                    filepath=r.filepath,
                    created_at=r.created_at,
                    owner_label=owner_label,
                )
            )
    hits.sort(
        key=lambda h: (h.created_at is None, -(h.created_at.timestamp() if h.created_at else 0))
    )
    return hits[:limit]


# --------------------------------------------------------------------------- #
# Metrics (latest values + trends)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MetricPoint:
    at: datetime
    value: float
    secondary: float | None = None
    unit: str | None = None


async def latest_vital(
    db: AsyncSession, user_id: uuid.UUID, vital_type: str
) -> MetricPoint | None:
    row = (
        await db.execute(
            select(VitalReading)
            .where(
                VitalReading.user_id == user_id,
                VitalReading.vital_type == vital_type,
            )
            .order_by(VitalReading.recorded_at.desc(), VitalReading.id.desc())
        )
    ).scalars().first()
    if row is None:
        return None
    return MetricPoint(
        at=row.recorded_at,
        value=float(row.value_primary),
        secondary=float(row.value_secondary) if row.value_secondary is not None else None,
        unit=row.unit,
    )


async def vital_series(
    db: AsyncSession, user_id: uuid.UUID, vital_type: str, since: datetime
) -> list[MetricPoint]:
    rows = (
        await db.execute(
            select(VitalReading)
            .where(
                VitalReading.user_id == user_id,
                VitalReading.vital_type == vital_type,
                VitalReading.recorded_at >= since,
            )
            .order_by(VitalReading.recorded_at.asc(), VitalReading.id.asc())
        )
    ).scalars().all()
    return [
        MetricPoint(
            at=r.recorded_at,
            value=float(r.value_primary),
            secondary=float(r.value_secondary) if r.value_secondary is not None else None,
            unit=r.unit,
        )
        for r in rows
    ]


async def latest_body_measurement(
    db: AsyncSession, user_id: uuid.UUID, mtype: str
) -> MetricPoint | None:
    row = (
        await db.execute(
            select(BodyMeasurement)
            .where(
                BodyMeasurement.user_id == user_id, BodyMeasurement.type == mtype
            )
            .order_by(
                BodyMeasurement.date.desc().nulls_last(), BodyMeasurement.id.desc()
            )
        )
    ).scalars().first()
    if row is None:
        return None
    return MetricPoint(at=row.date or utcnow(), value=float(row.value))


# --------------------------------------------------------------------------- #
# Lifestyle logs (tracker adds + aggregates)
# --------------------------------------------------------------------------- #
LIFESTYLE_TYPES = ("water", "alcohol", "coffee", "tea", "smoking")
DEFAULT_UNITS = {
    "water": "glass",
    "coffee": "cup",
    "tea": "cup",
    "alcohol": "drink",
    "smoking": "cigarette",
}


async def add_lifestyle_log(
    db: AsyncSession,
    user_id: uuid.UUID,
    log_type: str,
    quantity: float,
    unit: str | None,
    logged_at: datetime | None = None,
) -> LifestyleLog:
    row = LifestyleLog(
        user_id=user_id,
        log_type=log_type,
        quantity=quantity,
        unit=unit or DEFAULT_UNITS.get(log_type, "unit"),
        metadata_json={"source": "davi_chat"},
        logged_at=logged_at or utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def lifestyle_totals(
    db: AsyncSession, user_id: uuid.UUID, since: datetime
) -> dict[str, float]:
    rows = (
        await db.execute(
            select(LifestyleLog.log_type, func.sum(LifestyleLog.quantity))
            .where(
                LifestyleLog.user_id == user_id, LifestyleLog.logged_at >= since
            )
            .group_by(LifestyleLog.log_type)
        )
    ).all()
    return {log_type: float(total) for log_type, total in rows}


def window_start(period: str, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    days = {"week": 7, "month": 30, "year": 365}.get(period, 7)
    return now - timedelta(days=days)


# --------------------------------------------------------------------------- #
# Medications (current, from medicine_tracking) — read only
# --------------------------------------------------------------------------- #
async def active_medications(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = 8
) -> list[str]:
    """Current (non-stopped, non-private) medications as "Name Strength" strings.

    Ordered by name for determinism. PRN ("as needed") meds are included and
    marked. Never returns private rows.
    """
    rows = (
        await db.execute(
            select(MedicineTracking)
            .where(
                MedicineTracking.user_id == user_id,
                MedicineTracking.stopped_at.is_(None),
                MedicineTracking.private.is_(False),
            )
            .order_by(MedicineTracking.name.asc(), MedicineTracking.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    out: list[str] = []
    for r in rows:
        label = r.name if not r.strength else f"{r.name} {r.strength}"
        if r.is_prn:
            label += " (as needed)"
        out.append(label)
    return out

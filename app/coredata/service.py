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
from app.models.core import User
from app.models.coredata import (
    Bill,
    BodyMeasurement,
    Doctor,
    DoctorConnect,
    DoctorSpecialization,
    FamilyConnect,
    FileAccessExclusion,
    Insurance,
    LifestyleLog,
    ManualTracking,
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
    "insurance": (Insurance, "insurance document"),
    "bill": (Bill, "bill"),
}


@dataclass(frozen=True)
class DocumentHit:
    kind: str
    doc_id: int
    filepath: str
    created_at: datetime | None
    owner_label: str  # "you" or the relation name
    # AI classification title (content.ai.classification.title) — production
    # scans have no name column, so this is their canonical display name.
    title: str | None = None



# --------------------------------------------------------------------------- #
# Family resolution
# --------------------------------------------------------------------------- #
def _owner_read_grant(fc: FamilyConnect, owner_is_requester: bool) -> bool:
    """Production read-consent semantics (FileServiceImpl.hasConnectionRead):
    the grant sits on the OWNER's side of the connection — ``req_read`` when
    the owner sent the request, ``acc_read`` when they accepted. Falls back to
    the legacy ``*_file_share`` columns when the new ones are NULL (rows that
    predate the ddl-auto column addition, and older databases)."""
    if owner_is_requester:
        return bool(
            fc.req_read if fc.req_read is not None else fc.req_file_share
        )
    return bool(
        fc.acc_read if fc.acc_read is not None else fc.acc_file_share
    )


async def resolve_family_member(
    db: AsyncSession, user_id: uuid.UUID, relation_term: str
) -> uuid.UUID | None:
    """Resolve "father"/"mother"/… to a connected user id, honouring consent.

    Only accepted links where the OWNER's read grant is on qualify. The
    relation name is matched from the requester's perspective (relations row)
    or its inverse for the acceptor side.
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
            # Viewer sent the request → owner is the acceptor → acc_read.
            other = fc.acceptor_id
            other_shares = _owner_read_grant(fc, owner_is_requester=False)
            name = rel.name
        else:
            # Viewer accepted → owner is the requester → req_read.
            other = fc.requester_id
            other_shares = _owner_read_grant(fc, owner_is_requester=True)
            name = rel.inverse
        if other_shares and term in name.strip().lower():
            return other
    return None


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
# chat document kind → production resource_type_enum value (for exclusions).
_RESOURCE_TYPE = {
    "report": "reports",
    "scan": "scans_imaging",
    "prescription": "prescriptions",
    "vaccination": "vaccinations",
    "insurance": "insurance",
    "bill": "bills",
}


def _ai_title(content) -> str | None:
    """Display title from the mhn-ai envelope (content.ai.classification.title).

    Production scans have no name column — their listing title comes from the
    AI classification, mirroring the Spring/React behaviour.
    """
    try:
        title = (content or {}).get("ai", {}).get("classification", {}).get("title")
        return str(title) if title else None
    except AttributeError:
        return None


async def _viewer_exclusions(
    db: AsyncSession, viewer_id: uuid.UUID
) -> dict[str, set[int]]:
    """resource_type → excluded resource ids for this viewer. Fail-open to {}
    (older/standalone databases without the table)."""
    try:
        rows = (
            await db.execute(
                select(FileAccessExclusion).where(
                    FileAccessExclusion.user_id == viewer_id
                )
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — table may not exist on standalone DBs
        return {}
    out: dict[str, set[int]] = {}
    for r in rows:
        out.setdefault(r.resource_type, set()).add(r.resource_id)
    return out


async def can_view_document(
    db: AsyncSession,
    viewer_id: uuid.UUID,
    owner_id: uuid.UUID,
    resource_type: str,
    resource_id: int,
    *,
    is_private: bool | None,
) -> bool:
    """Production read gate (FileServiceImpl.assertCanRead): owner always;
    otherwise not-private + accepted connection with the owner-side read grant
    + no per-file exclusion for this viewer."""
    if viewer_id == owner_id:
        return True
    if is_private:
        return False
    fc = (
        await db.execute(
            select(FamilyConnect).where(
                FamilyConnect.accepted.is_(True),
                (
                    (FamilyConnect.requester_id == owner_id)
                    & (FamilyConnect.acceptor_id == viewer_id)
                )
                | (
                    (FamilyConnect.requester_id == viewer_id)
                    & (FamilyConnect.acceptor_id == owner_id)
                ),
            )
        )
    ).scalars().first()
    if fc is None:
        return False
    owner_is_requester = fc.requester_id == owner_id
    if not _owner_read_grant(fc, owner_is_requester=owner_is_requester):
        return False
    denied = await _viewer_exclusions(db, viewer_id)
    return resource_id not in denied.get(resource_type, set())


async def latest_documents(
    db: AsyncSession,
    owner_id: uuid.UUID,
    kinds: list[str],
    *,
    owner_label: str = "you",
    include_private: bool = True,
    limit: int = 3,
    viewer_id: uuid.UUID | None = None,
) -> list[DocumentHit]:
    """Newest documents of the requested kinds for one owner.

    ``include_private=False`` is used for family members' documents; pass the
    ``viewer_id`` there too so per-file exclusions (file_access_exclusions —
    production's per-file consent opt-out) are honoured.
    """
    denied = (
        await _viewer_exclusions(db, viewer_id)
        if (viewer_id is not None and viewer_id != owner_id)
        else {}
    )
    hits: list[DocumentHit] = []
    for kind in kinds:
        model, _label = DOCUMENT_KINDS[kind]
        query = select(model).where(model.user_id == owner_id)  # type: ignore[attr-defined]
        if not include_private:
            query = query.where(model.private.is_not(True))  # type: ignore[attr-defined]
        rows = (
            await db.execute(
                query.order_by(model.created_at.desc().nulls_last(), model.id.desc())  # type: ignore[attr-defined]
                .limit(limit + 8)  # headroom: exclusions filter below
            )
        ).scalars().all()
        denied_ids = denied.get(_RESOURCE_TYPE.get(kind, kind), set())
        for r in rows:
            if r.id in denied_ids:
                continue
            hits.append(
                DocumentHit(
                    kind=kind,
                    doc_id=r.id,
                    filepath=r.filepath or "",
                    created_at=r.created_at,
                    owner_label=owner_label,
                    title=_ai_title(getattr(r, "content", None)),
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


# --------------------------------------------------------------------------- #
# Manual tracking (sleep / steps / calories …) — latest value per type
# --------------------------------------------------------------------------- #
# Order in which manual-tracking metrics surface (only these are wellbeing-
# relevant; heart_rate/bp/sugar/spo2 already come from vital_reading).
MANUAL_METRIC_ORDER = ("sleep", "steps", "calories", "water")
_MANUAL_UNIT = {"sleep": "h", "steps": "steps", "calories": "kcal", "water": "glass"}


async def latest_manual_metrics(
    db: AsyncSession, user_id: uuid.UUID, since: datetime
) -> dict[str, MetricPoint]:
    """Latest value per manual-tracking type recorded since ``since``."""
    rows = (
        await db.execute(
            select(ManualTracking)
            .where(
                ManualTracking.user_id == user_id,
                ManualTracking.value.is_not(None),
                ManualTracking.type.in_(MANUAL_METRIC_ORDER),
                ManualTracking.effective_from >= since,
            )
            .order_by(
                ManualTracking.effective_from.desc().nulls_last(),
                ManualTracking.id.desc(),
            )
        )
    ).scalars().all()
    out: dict[str, MetricPoint] = {}
    for r in rows:
        if r.type in out or r.value is None:
            continue
        out[r.type] = MetricPoint(
            at=r.effective_from or utcnow(),
            value=float(r.value),
            unit=r.unit or _MANUAL_UNIT.get(r.type),
        )
    return out


# --------------------------------------------------------------------------- #
# Body measurements — latest value per type
# --------------------------------------------------------------------------- #
BODY_METRIC_ORDER = (
    "weight", "bmi", "body_fat", "muscle_mass", "visceral_fat",
    "bone_mass", "water", "height",
)
_BODY_UNIT = {
    "weight": "kg", "bmi": "kg/m²", "body_fat": "%", "muscle_mass": "%",
    "visceral_fat": "", "bone_mass": "kg", "water": "%", "height": "cm",
}


async def latest_body_metrics(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[str, MetricPoint]:
    """Latest value per body-measurement type."""
    rows = (
        await db.execute(
            select(BodyMeasurement)
            .where(BodyMeasurement.user_id == user_id)
            .order_by(
                BodyMeasurement.date.desc().nulls_last(),
                BodyMeasurement.id.desc(),
            )
        )
    ).scalars().all()
    out: dict[str, MetricPoint] = {}
    for r in rows:
        if r.type in out:
            continue
        out[r.type] = MetricPoint(
            at=r.date or utcnow(), value=float(r.value),
            unit=_BODY_UNIT.get(r.type),
        )
    return out


# --------------------------------------------------------------------------- #
# Lab values — ALL extracted parameters from recent reports + scans
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LabValue:
    name: str
    value: str
    unit: str | None
    at: datetime | None


def _collect_params(content, out: list[tuple[str, str, str | None]]) -> None:
    """Walk a report's extracted JSON, collecting every (name, value, unit).

    Handles the legacy demo shape ({"tests": [{"name","value","unit"}]}) AND
    the production mhn-ai envelope (content.ai.extraction.results[] with
    "test_name" and an authoritative Python-computed "abnormal_flag", which is
    appended to the value so the model sees production's own flagging).
    """
    if isinstance(content, dict):
        name = (
            content.get("name") or content.get("parameter") or content.get("test")
            or content.get("test_name")
        )
        value = None
        for vk in ("value", "result", "reading"):
            if content.get(vk) is not None:
                value = content.get(vk)
                break
        if name and value is not None:
            rendered = str(value).strip()
            flag = content.get("abnormal_flag")
            if flag in ("low", "high"):
                rendered += f" ({flag})"
            out.append((str(name).strip(), rendered, content.get("unit")))
        for v in content.values():
            _collect_params(v, out)
    elif isinstance(content, list):
        for item in content:
            _collect_params(item, out)


async def recent_lab_values(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    report_limit: int = 6,
    max_values: int = 14,
) -> list[LabValue]:
    """Distinct lab values from the user's most recent reports and scans.

    Newest-first; the first occurrence of each parameter name wins (most recent
    value). Capped to keep the [P] block bounded.
    """
    reports = (
        await db.execute(
            select(Report)
            .where(Report.user_id == user_id, Report.content.is_not(None))
            .order_by(Report.created_at.desc().nulls_last(), Report.id.desc())
            .limit(report_limit)
        )
    ).scalars().all()
    scans = (
        await db.execute(
            select(ScanImaging)
            .where(ScanImaging.user_id == user_id, ScanImaging.content.is_not(None))
            .order_by(ScanImaging.created_at.desc().nulls_last(), ScanImaging.id.desc())
            .limit(report_limit)
        )
    ).scalars().all()

    docs = sorted(
        [*reports, *scans],
        key=lambda d: (d.created_at is None,
                       -(d.created_at.timestamp() if d.created_at else 0)),
    )
    seen: set[str] = set()
    out: list[LabValue] = []
    for doc in docs:
        params: list[tuple[str, str, str | None]] = []
        _collect_params(doc.content, params)
        for name, value, unit in params:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(LabValue(name=name, value=value, unit=unit, at=doc.created_at))
            if len(out) >= max_values:
                return out
    return out


# --------------------------------------------------------------------------- #
# Family connections & doctor consults (read-only app-data lookups)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FamilyMemberInfo:
    name: str | None
    relation: str | None  # from the viewer's perspective
    accepted: bool
    shares_documents: bool


async def list_family_connections(
    db: AsyncSession, user_id: uuid.UUID
) -> list[FamilyMemberInfo]:
    """Everyone in the user's Family Connect, with the viewer-side relation
    name and whether the other person currently shares documents."""
    rows = (
        await db.execute(
            select(FamilyConnect, Relation)
            .join(Relation, FamilyConnect.relation_id == Relation.id, isouter=True)
            .where(
                (FamilyConnect.requester_id == user_id)
                | (FamilyConnect.acceptor_id == user_id)
            )
            .order_by(FamilyConnect.id)
        )
    ).all()
    other_ids = [
        fc.acceptor_id if fc.requester_id == user_id else fc.requester_id
        for fc, _rel in rows
    ]
    names: dict[uuid.UUID, str] = {}
    if other_ids:
        try:
            for uid, uname in (
                await db.execute(
                    select(User.id, User.name).where(User.id.in_(other_ids))
                )
            ).all():
                names[uid] = uname
        except Exception:  # noqa: BLE001 — user table may be absent standalone
            names = {}
    out: list[FamilyMemberInfo] = []
    for fc, rel in rows:
        viewer_is_requester = fc.requester_id == user_id
        other = fc.acceptor_id if viewer_is_requester else fc.requester_id
        relation = None
        if rel is not None:
            relation = rel.name if viewer_is_requester else rel.inverse
        shares = _owner_read_grant(
            fc, owner_is_requester=not viewer_is_requester
        )
        out.append(
            FamilyMemberInfo(
                name=names.get(other),
                relation=relation,
                accepted=bool(fc.accepted),
                shares_documents=bool(fc.accepted) and shares,
            )
        )
    return out


@dataclass(frozen=True)
class ConsultInfo:
    doctor_name: str | None
    specialization: str | None
    connected_at: datetime | None
    status: str  # "connected" | "pending"


async def recent_doctor_consults(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 5
) -> list[ConsultInfo]:
    """The user's doctor connections through the app, newest first."""
    rows = (
        await db.execute(
            select(DoctorConnect, Doctor, DoctorSpecialization)
            .join(Doctor, DoctorConnect.doctor_id == Doctor.id)
            .join(
                DoctorSpecialization,
                Doctor.specialization_id == DoctorSpecialization.id,
                isouter=True,
            )
            .where(DoctorConnect.user_id == user_id)
            .order_by(
                DoctorConnect.created_at.desc().nulls_last(), DoctorConnect.id.desc()
            )
            .limit(limit)
        )
    ).all()
    doctor_user_ids = [d.user_id for _dc, d, _sp in rows]
    names: dict[uuid.UUID, str] = {}
    if doctor_user_ids:
        try:
            for uid, uname in (
                await db.execute(
                    select(User.id, User.name).where(User.id.in_(doctor_user_ids))
                )
            ).all():
                names[uid] = uname
        except Exception:  # noqa: BLE001
            names = {}
    return [
        ConsultInfo(
            doctor_name=names.get(d.user_id),
            specialization=sp.name if sp is not None else None,
            connected_at=dc.created_at,
            status=(
                "connected"
                if dc.doctor_acceptance and dc.user_acceptance
                else "pending"
            ),
        )
        for dc, d, sp in rows
    ]

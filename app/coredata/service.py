"""Core-data services: documents, family access, vitals/metrics, lifestyle.

All reads honour the core app's privacy model: a family member's documents are
only visible when the family link is accepted, the owner's file-share flag is
on, and the document is not private. Tracker writes go to ``lifestyle_log`` —
the same table the core app writes — on the user's behalf.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import sqlalchemy as sa
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
    MedicalCondition,
    MedicineTracking,
    PeriodSettings,
    PeriodStatus,
    PeriodTracking,
    Prescription,
    Relation,
    Report,
    ScanImaging,
    Vaccination,
    VitalReading,
)

logger = logging.getLogger("davi.coredata")

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


# Asked-for term → the relation-name words it accepts. Production relation
# rows use both gendered and generic names ("Father"/"Child",
# "Grandparent"/"Grandchild"), so "my grandson" must find a "Grandchild" row
# and "my son" a "Child" row. Matching is whole-word: "son" never matches
# "Grandson", "mother" never matches "Grandmother".
_RELATION_ACCEPTS: dict[str, frozenset[str]] = {
    "father": frozenset({"father", "parent"}),
    "mother": frozenset({"mother", "parent"}),
    "son": frozenset({"son", "child"}),
    "daughter": frozenset({"daughter", "child"}),
    "husband": frozenset({"husband", "spouse"}),
    "wife": frozenset({"wife", "spouse"}),
    "brother": frozenset({"brother", "sibling"}),
    "sister": frozenset({"sister", "sibling"}),
    "grandfather": frozenset({"grandfather", "grandparent"}),
    "grandmother": frozenset({"grandmother", "grandparent"}),
    "grandson": frozenset({"grandson", "grandchild"}),
    "granddaughter": frozenset({"granddaughter", "grandchild"}),
    "grandchild": frozenset({"grandchild", "grandson", "granddaughter"}),
}


def _relation_matches(term: str, name: str | None) -> bool:
    """Whole-word relation matching with gendered↔generic equivalence."""
    if not name:
        return False
    accepts = _RELATION_ACCEPTS.get(term, frozenset({term}))
    tokens = {t.strip(".,()'\"") for t in name.lower().split()}
    return bool(tokens & accepts)


# The gender a term implies ("grandfather" is male) — used to pick the right
# person when the relation row is generic: two "Grandparent" connections are
# told apart by the member's own gender from the user table.
_TERM_GENDER: dict[str, str] = {
    "father": "male", "son": "male", "brother": "male", "husband": "male",
    "grandfather": "male", "grandson": "male", "uncle": "male",
    "nephew": "male",
    "mother": "female", "daughter": "female", "sister": "female",
    "wife": "female", "grandmother": "female", "granddaughter": "female",
    "aunt": "female", "niece": "female",
}

# Generic relation word + member gender → the gendered word for display
# ("Grandchild" + male → "Grandson").
_GENDERED_FORMS: dict[tuple[str, str], str] = {
    ("grandparent", "male"): "grandfather",
    ("grandparent", "female"): "grandmother",
    ("grandchild", "male"): "grandson",
    ("grandchild", "female"): "granddaughter",
    ("parent", "male"): "father",
    ("parent", "female"): "mother",
    ("child", "male"): "son",
    ("child", "female"): "daughter",
    ("sibling", "male"): "brother",
    ("sibling", "female"): "sister",
    ("spouse", "male"): "husband",
    ("spouse", "female"): "wife",
}


def _norm_gender(value: str | None) -> str | None:
    """"Male"/"M"/"male" → "male"; anything else/unset → None (unknown)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in ("male", "m"):
        return "male"
    if v in ("female", "f"):
        return "female"
    return None


def gendered_relation(relation: str | None, gender: str | None) -> str | None:
    """Display form of a relation, gendered when the row is generic.

    "Grandchild" + male → "Grandson"; unknown gender or an already-gendered
    name passes through unchanged.
    """
    if not relation:
        return relation
    g = _norm_gender(gender)
    if g is None:
        return relation
    swapped = _GENDERED_FORMS.get((relation.strip().lower(), g))
    if swapped is None:
        return relation
    return swapped.capitalize() if relation[:1].isupper() else swapped


async def _genders_of(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """user id → normalized gender; {} when the user table is unavailable."""
    if not ids:
        return {}
    try:
        rows = (
            await db.execute(
                select(User.id, User.gender).where(User.id.in_(ids))
            )
        ).all()
    except Exception:  # noqa: BLE001 — user table may be absent standalone
        return {}
    return {uid: _norm_gender(g) for uid, g in rows}


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
            .order_by(FamilyConnect.id)
        )
    ).all()
    candidates: list[uuid.UUID] = []
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
        if other_shares and _relation_matches(term, name):
            candidates.append(other)
    if not candidates:
        return None
    # A gendered ask ("grandmother") against generic relation rows
    # ("Grandparent") is settled by the member's own gender: contradicting
    # candidates are excluded, unknown gender passes (fail-open).
    want = _TERM_GENDER.get(term)
    if want is not None and len({*candidates}) >= 1:
        genders = await _genders_of(db, candidates)
        candidates = [
            c for c in candidates
            if genders.get(c) is None or genders.get(c) == want
        ]
    return candidates[0] if candidates else None


async def resolve_family_member_by_name(
    db: AsyncSession, user_id: uuid.UUID, name_term: str
) -> uuid.UUID | None:
    """Resolve "Bhargava's reports" — a connected member named by name.

    Same consent gate as relation resolution: accepted link + the owner-side
    read grant. The term matches a word of the member's display name or
    their username, prefix-tolerant in both directions ("bhargav" finds
    "Bhargava Ram" and vice versa). None when nobody qualifies — a stray
    possessive then just falls through to the normal not-found reply.
    """
    term = name_term.strip().lower()
    if len(term) < 3:
        return None
    rows = (
        await db.execute(
            select(FamilyConnect).where(
                FamilyConnect.accepted.is_(True),
                (FamilyConnect.requester_id == user_id)
                | (FamilyConnect.acceptor_id == user_id),
            )
        )
    ).scalars().all()
    shared: list[uuid.UUID] = []
    for fc in rows:
        viewer_is_requester = fc.requester_id == user_id
        other = fc.acceptor_id if viewer_is_requester else fc.requester_id
        if _owner_read_grant(fc, owner_is_requester=not viewer_is_requester):
            shared.append(other)
    if not shared:
        return None
    try:
        name_rows = (
            await db.execute(
                select(User.id, User.name, User.user_name).where(
                    User.id.in_(shared)
                )
            )
        ).all()
    except Exception:  # noqa: BLE001 — user table may be absent standalone
        return None
    for uid, display, login in name_rows:
        for word in f"{display or ''} {login or ''}".lower().split():
            if len(word) >= 3 and (
                word.startswith(term) or term.startswith(word)
            ):
                return uid
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
        if kind not in DOCUMENT_KINDS:
            # A caller passing a TABLE name instead of a kind is a bug in
            # the caller, and callers wrap this in a fail-open, so a bare
            # KeyError disappears. Say so and carry on.
            logger.warning("unknown document kind %r; skipping", kind)
            continue
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
    genders: dict[uuid.UUID, str | None] = {}
    if other_ids:
        try:
            for uid, uname, ugender in (
                await db.execute(
                    select(User.id, User.name, User.gender).where(
                        User.id.in_(other_ids)
                    )
                )
            ).all():
                names[uid] = uname
                genders[uid] = ugender
        except Exception:  # noqa: BLE001 — user table may be absent standalone
            names = {}
    out: list[FamilyMemberInfo] = []
    for fc, rel in rows:
        viewer_is_requester = fc.requester_id == user_id
        other = fc.acceptor_id if viewer_is_requester else fc.requester_id
        relation = None
        if rel is not None:
            relation = rel.name if viewer_is_requester else rel.inverse
            # Generic rows read gendered when the member's gender is known:
            # "Grandchild" + male → "Grandson".
            relation = gendered_relation(relation, genders.get(other))
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


@dataclass
class DocumentOwner:
    """Who owns a document and whether it is private — the two facts the
    consent gate needs before any read."""

    owner_id: uuid.UUID
    is_private: bool | None
    filepath: str


async def document_owner(
    db: AsyncSession, kind: str, doc_id: int
) -> DocumentOwner | None:
    """Look up a document's owner and privacy flag by (kind, id).

    Returns None for an unknown kind or a missing row — the caller then has
    nothing to check consent against, and must refuse.
    """
    entry = DOCUMENT_KINDS.get(kind)
    if entry is None:
        return None
    model, _label = entry
    row = (
        await db.execute(select(model).where(model.id == doc_id))
    ).scalars().first()
    if row is None:
        return None
    return DocumentOwner(
        owner_id=row.user_id,
        is_private=getattr(row, "private", None),
        filepath=getattr(row, "filepath", "") or "",
    )


# --------------------------------------------------------------------------- #
# Medical history — conditions, surgeries, allergies
# --------------------------------------------------------------------------- #
# Allergy severities that warrant an unprompted warning. "mild" does not: a
# warning on every antihistamine question would train readers to ignore the
# one that matters.
_WARNING_SEVERITIES = frozenset({"severe", "medium"})


async def medication_allergies(
    db: AsyncSession, user_id: uuid.UUID
) -> list[MedicalCondition]:
    """The reader's own MEDICATION allergies, worst first.

    Own data only — this is never called for a family member. Honours the
    ``private`` flag the owning app honours: a row the reader marked private is
    not something Davi should read back to them in a context they did not ask
    for.
    """
    try:
        rows = (
            await db.execute(
                select(MedicalCondition)
                .where(
                    MedicalCondition.user_id == user_id,
                    MedicalCondition.type == "allergy",
                    MedicalCondition.category == "medication",
                    # NULL means not private (the column's own default).
                    sa.or_(
                        MedicalCondition.private.is_(False),
                        MedicalCondition.private.is_(None),
                    ),
                )
                .order_by(MedicalCondition.id)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — a read must never break a reply
        logger.warning("medication allergy read failed", exc_info=True)
        return []

    order = {"severe": 0, "medium": 1, "mild": 2}
    return sorted(rows, key=lambda r: order.get((r.severity or "").lower(), 3))


def allergy_warning(allergies: list[MedicalCondition]) -> str:
    """A deterministic warning line, or "" when none is warranted.

    Deliberately does NOT try to decide whether the drug asked about is in the
    class the reader reacts to — that is a clinical judgement Davi has no
    dataset for, and guessing it wrong in either direction is worse than
    naming what is on record and letting a pharmacist connect them.
    """
    named = [
        a for a in allergies
        if (a.severity or "").lower() in _WARNING_SEVERITIES and a.name
    ]
    if not named:
        return ""
    items = "; ".join(
        f"{a.name}" + (f" ({a.reaction})" if a.reaction else "")
        for a in named[:3]
    )
    return (
        "Before anything else — your record lists a medication allergy: "
        f"{items}. Check with a pharmacist or your prescriber that this "
        "medicine is safe for you, especially if it is related to what you "
        "react to."
    )


# --------------------------------------------------------------------------- #
# Cycle tracking — OWN DATA ONLY, never a family member's
# --------------------------------------------------------------------------- #
# mhn-spring made this class of data default-private and argued why in its own
# DDL: the family model is default-ALLOW, and shipping cycle data on it would
# hand every accepted connection visibility of contraception and pregnancy
# status nobody opted into. `resource_type_enum` was deliberately not extended
# with a cycle value, so it is not a shareable resource at all.
#
# Davi therefore never reads this for anyone but the reader themselves, and
# there is no viewer_id parameter here to make that mistake possible.


@dataclass(frozen=True)
class CycleSnapshot:
    """What Davi may say about a reader's cycle, already gated."""

    tracking_enabled: bool = False
    stage: str | None = None
    pregnancy: str | None = None
    breastfeeding: bool = False
    diagnosed_pcos: bool = False
    cycles_countable: bool | None = None
    predictions_suppressed: bool | None = None
    last_period_start: date | None = None
    average_cycle_length: int | None = None
    recent_cycles: int = 0
    # Set only when the reader turned the fertile-window display ON. Davi must
    # not state one otherwise -- it is an estimate, not contraception, and the
    # owning team deliberately defaults it off.
    may_show_fertile_window: bool = False

    @property
    def has_anything(self) -> bool:
        return self.tracking_enabled and (
            self.last_period_start is not None or self.stage is not None
        )


async def cycle_snapshot(db: AsyncSession, user_id: uuid.UUID) -> CycleSnapshot:
    """The reader's own cycle picture, or an empty one. Never raises.

    Returns empty when tracking is disabled: an explicit "off" is an answer,
    and reading past it would surface data the reader switched off.
    """
    try:
        settings = (
            await db.execute(
                select(PeriodSettings).where(PeriodSettings.user_id == user_id)
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning("cycle settings read failed", exc_info=True)
        return CycleSnapshot()

    # No row means the reader has never opened cycle tracking. Absent is off.
    if settings is None or settings.enabled is False:
        return CycleSnapshot()

    try:
        # TEMPORAL: the current status is the latest row that has taken effect.
        status = (
            await db.execute(
                select(PeriodStatus)
                .where(PeriodStatus.user_id == user_id)
                .order_by(
                    PeriodStatus.effective_from.desc(), PeriodStatus.id.desc()
                )
                .limit(1)
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning("cycle status read failed", exc_info=True)
        status = None

    try:
        # RECORDED cycles only. A predicted row is an estimate the app drew,
        # not something that happened, and reporting one as fact would be a
        # claim the reader never made.
        cycles = (
            await db.execute(
                select(PeriodTracking)
                .where(
                    PeriodTracking.user_id == user_id,
                    sa.or_(
                        PeriodTracking.is_predicted.is_(False),
                        PeriodTracking.is_predicted.is_(None),
                    ),
                )
                .order_by(PeriodTracking.start_date.desc())
                .limit(6)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        logger.warning("cycle history read failed", exc_info=True)
        cycles = []

    lengths = [c.cycle_length for c in cycles if c.cycle_length]
    last_start = cycles[0].start_date.date() if cycles else None

    return CycleSnapshot(
        tracking_enabled=True,
        stage=status.stage if status else None,
        pregnancy=status.pregnancy if status else None,
        breastfeeding=bool(status.breastfeeding) if status else False,
        diagnosed_pcos=bool(status.diagnosed_pcos) if status else False,
        cycles_countable=status.cycles_countable if status else None,
        predictions_suppressed=status.predictions_suppressed if status else None,
        last_period_start=last_start,
        average_cycle_length=(
            round(sum(lengths) / len(lengths)) if lengths else None
        ),
        recent_cycles=len(cycles),
        may_show_fertile_window=bool(
            settings.show_fertile_window and settings.predict_enabled
        ),
    )


def render_cycle(snapshot: CycleSnapshot) -> str:
    """A deterministic summary for a cycle question. "" when there is nothing.

    States what is recorded. It does NOT predict a next period or a fertile
    window: prediction is the app's job, it has the model for it, and
    `predictions_suppressed` exists precisely because there are states where a
    prediction would be wrong to offer.
    """
    if not snapshot.has_anything:
        return ""

    parts: list[str] = []
    if snapshot.last_period_start:
        parts.append(
            "Your last recorded period started on "
            f"{snapshot.last_period_start.isoformat()}."
        )
    if snapshot.average_cycle_length and snapshot.recent_cycles > 1:
        parts.append(
            f"Across your last {snapshot.recent_cycles} recorded cycles the "
            f"average length is about {snapshot.average_cycle_length} days."
        )
    if snapshot.pregnancy and snapshot.pregnancy != "not_pregnant":
        parts.append(
            f"Your record notes pregnancy status: {snapshot.pregnancy}."
        )
    if snapshot.breastfeeding:
        parts.append("Your record notes that you are breastfeeding.")
    if snapshot.diagnosed_pcos:
        parts.append("Your record notes a PCOS diagnosis.")
    if snapshot.predictions_suppressed:
        parts.append(
            "Cycle predictions are turned off for your current recorded "
            "status, so there is no expected date to give."
        )
    if not parts:
        return ""
    parts.append(
        "This is what is recorded in the app, not a diagnosis — anything that "
        "seems different from usual is worth raising with your doctor."
    )
    return " ".join(parts)


def pregnancy_safety_flag(snapshot: CycleSnapshot) -> str:
    """The ONE cycle fact that belongs in every prompt, or "".

    Pregnancy and breastfeeding change what is safe to say about a great many
    medicines, so this much travels with the reader. Contraception, stage, PCOS
    and cycle dates do NOT: they are not needed to answer a headache question,
    and putting them in every prompt would spread them across logs, caches and
    support screenshots for no benefit.
    """
    if snapshot.pregnancy == "pregnant":
        return "The reader's record notes they are pregnant."
    if snapshot.pregnancy == "postpartum":
        return "The reader's record notes they are postpartum."
    if snapshot.breastfeeding:
        return "The reader's record notes they are breastfeeding."
    return ""

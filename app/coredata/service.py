"""Core-data services: documents, family access, vitals/metrics, lifestyle.

All reads honour the core app's privacy model: a family member's documents are
only visible when the family link is accepted, the owner's file-share flag is
on, and the document is not private. Tracker writes go to ``lifestyle_log`` —
the same table the core app writes — on the user's behalf.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.common import as_utc, utcnow
from app.models.core import User
from app.models.coredata import (
    Bill,
    BodyMeasurement,
    BodyMeasurementGoal,
    Doctor,
    DoctorConnect,
    DoctorSpecialization,
    FamilyConnect,
    FileAccessExclusion,
    Insurance,
    LifestyleDailyTotal,
    LifestyleLimit,
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
    SahhaDailyTotal,
    SahhaGoal,
    SahhaWeeklyTotal,
    ScanImaging,
    UserThpSeries,
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


def _vital_point(row: VitalReading) -> MetricPoint:
    return MetricPoint(
        # UTC-aware, always: sqlite returns a NAIVE datetime for a
        # timestamptz column and PostgreSQL an aware one, so a caller
        # comparing `point.at` to a window boundary works on one and raises on
        # the other. Normalised HERE, where every vital reader routes through.
        at=as_utc(row.recorded_at),
        value=float(row.value_primary),
        secondary=float(row.value_secondary) if row.value_secondary is not None else None,
        unit=row.unit,
    )


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
            # `.first()` without this read EVERY reading of that type for the
            # user and threw all but one away — a reader with years of daily
            # blood-pressure entries transferred the lot, four times a turn.
            .limit(1)
        )
    ).scalars().first()
    return None if row is None else _vital_point(row)


async def latest_vitals(
    db: AsyncSession, user_id: uuid.UUID, vital_types: Sequence[str]
) -> dict[str, MetricPoint]:
    """Latest reading for each of `vital_types`, in ONE round trip.

    The health snapshot wants four (blood pressure, sugar, heart rate, SpO2)
    and asked for them one at a time. Locally that is four cheap queries; on a
    shared database reached over a network it is four times the latency, on the
    path that runs for every personal question.

    ROW_NUMBER() OVER (PARTITION BY ...) keeps it exact rather than fetching a
    window and hoping every type appears in it — one type with far more
    readings than the others cannot crowd the rest out. Supported by both
    PostgreSQL and SQLite (3.25+).
    """
    if not vital_types:
        return {}
    ranked = (
        select(
            VitalReading,
            func.row_number()
            .over(
                partition_by=VitalReading.vital_type,
                order_by=(
                    VitalReading.recorded_at.desc(),
                    VitalReading.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(
            VitalReading.user_id == user_id,
            VitalReading.vital_type.in_(list(vital_types)),
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(aliased(VitalReading, ranked)).where(ranked.c.rn == 1)
        )
    ).scalars().all()
    return {r.vital_type: _vital_point(r) for r in rows}


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

# What mhn-spring stores each log type in, and the noun a reader reads it as.
#
# The authority is `LifestyleMetric` (mhn-spring), whose javadoc is explicit:
# "The one unit this metric is stored in. Totals are plain sums, so a second
# unit for the same metric would silently add glasses to millilitres; a write
# in anything else is rejected and the client converts first." `resolveUnit`
# (ManualTrackingServiceImpl:890-909) enforces that with a 400, and the
# rollups sum `quantity` regardless -- `MetricFanout.of` adds
# `new Measure(LifestyleMetric.primary(type), quantity)` and
# `ManualTrackingReconciler.MEASURES` reads `l.quantity AS amount`.
#
# So `lifestyle_log.quantity` IS the canonical measure on every row the
# platform accepted, and `SUM(quantity) GROUP BY log_type` is unit-safe by
# construction. `volume_ml` feeds only the derived `drink_volume_ml` series
# and `servings` feeds no rollup at all: neither is a total to read.
#
# log_type -> (unit stored in `lifestyle_log.unit`, the noun to print)
LIFESTYLE_UNITS: dict[str, tuple[str, str]] = {
    "water":        ("ml",      "ml"),
    "alcohol":      ("ml",      "ml"),
    "coffee":       ("cup",     "cup"),
    "tea":          ("cup",     "cup"),
    "smoking":      ("count",   "cigarette"),
    "energy_drink": ("serving", "serving"),
    "other_drink":  ("serving", "serving"),
}

# Millilitres per vessel, keyed on the DRINK -- not on the log type. Every
# number is a row of mhn-spring V35's `drink_serving_size` seed, which is
# keyed exactly this way and says why in its own comment: "Per category,
# because 'Large' is 350 ml of coffee, 500 ml of beer and 90 ml of whisky."
#
# Keying on `log_type` collapsed beer, wine and spirits into one pseudo-
# category "alcohol", so "a bottle of wine" was written as 330 ml -- BEER's
# bottle -- and "a glass of whisky" as 150 ml, WINE's glass. Those rows go
# into a shared table the app's charts and the reader's `lifestyle_limit`
# status read, and a 750 ml bottle logged as 330 is a 56% under-report of
# alcohol that outlives the conversation.
#
# A (drink, vessel) pair with no row here has no sanctioned size and is
# REFUSED, not guessed -- wine in a bottle and beer in a glass included; the
# seed has neither. An invented serving size on a shared health chart is
# worse than asking, and the refusal copy already exists.
_VESSEL_ML: dict[tuple[str, str], float] = {
    ("water", "glass"): 250.0,
    ("water", "bottle"): 500.0,
    ("beer", "bottle"): 330.0,
    ("beer", "can"): 500.0,
    ("beer", "pint"): 568.0,
    ("wine", "glass"): 150.0,
    ("spirits", "peg"): 60.0,
    # spirits/Small peg 30, which is also liqueur/Shot 30 -- the two rows the
    # seed gives a 30 ml pour, so "a shot of vodka" is sized, not invented.
    ("spirits", "shot"): 30.0,
}
# The drink a message named -> the V35 serving-size CATEGORY that sizes it.
# A drink absent here ("2 drinks", "some alcohol") names no category and so
# has no sanctioned size at all.
_DRINK_CATEGORY: dict[str, str] = {
    "water": "water",
    "beer": "beer",
    "wine": "wine",
    "whisky": "spirits",
    "whiskey": "spirits",
    "rum": "spirits",
    "vodka": "spirits",
}
_TO_ML = {"ml": 1.0, "millilitre": 1.0, "milliliter": 1.0,
          "litre": 1000.0, "liter": 1000.0, "l": 1000.0}

# Units that are a measure rather than a countable noun: never pluralised.
_MASS_UNITS = frozenset({"ml", "l", "g", "mg", "count"})


def plural_unit(unit: str, n: float) -> str:
    """`cup` -> `cups` for anything but one; `ml` stays `ml`."""
    if n == 1 or unit in _MASS_UNITS:
        return unit
    return unit + ("es" if unit.endswith(("s", "sh", "ch", "x")) else "s")


def canonical_amount(
    log_type: str, quantity: float, unit: str | None, kind: str | None = None
) -> tuple[float, str] | None:
    """``(quantity, unit)`` in the metric's own unit, or None when there is none.

    The one place a spoken unit becomes a stored one. mhn-spring rejects any
    other unit with a 400 and its rollups sum `quantity` whatever the unit
    says, so a row written as `quantity=2, unit='glass'` does not merely look
    wrong: it lands in the reader's own water-in-millilitres series in the app
    as 2 ml, and in their `lifestyle_limit` status.

    ``kind`` is the DRINK the reader named ("wine", "whisky"); the vessel is
    sized against that, because a bottle is 330 ml of beer and 750 of wine.
    It defaults to ``log_type`` so the water path and every existing caller
    are unchanged.

    None means "no sanctioned size for that (drink, vessel)" -- a bare "2
    drinks", a cup of water, a BOTTLE of wine. The caller asks rather than
    inventing one.
    """
    canonical = LIFESTYLE_UNITS.get(log_type, ("serving", "serving"))[0]
    spoken = (unit or canonical).strip().lower()
    if spoken == canonical:
        return quantity, canonical
    if canonical == "ml":
        category = _DRINK_CATEGORY.get((kind or log_type).strip().lower())
        factor = _TO_ML.get(spoken) or (
            _VESSEL_ML.get((category, spoken)) if category else None
        )
        return (quantity * factor, canonical) if factor else None
    # `cup`, `count` and `serving` all count servings, which is why V35's
    # backfill set `servings := quantity` with no unit check at all: a mug, a
    # can and a cigarette are each one. The vessel changes the noun, never the
    # number.
    return quantity, canonical


@dataclass(frozen=True)
class LifestyleTotal:
    """A lifestyle total and the unit it is in. Callers show ``text()``."""

    log_type: str
    total: float
    unit: str

    def text(self) -> str:
        """Reader-facing: "3 cups", "1250 ml", "5 cigarettes"."""
        return f"{self.total:g} {plural_unit(self.unit, self.total)}"


def lifestyle_phrase(total: LifestyleTotal) -> str:
    """``text()`` with the kind appended, unless the unit already names it.

    "3 cups of coffee", but "5 cigarettes" -- not "5 cigarettes of smoking".
    """
    if total.unit in ("cigarette", "beedi"):
        return total.text()
    return f"{total.text()} of {total.log_type}"


async def add_lifestyle_log(
    db: AsyncSession,
    user_id: uuid.UUID,
    log_type: str,
    quantity: float,
    unit: str | None,
    logged_at: datetime | None = None,
    kind: str | None = None,
) -> LifestyleLog:
    """Write one row, in the unit mhn-spring guarantees the column is in.

    Raises ``ValueError`` when the spoken unit has no sanctioned conversion --
    the same answer mhn-spring's own API gives (`resolveUnit` -> 400). This is
    a SHARED table read by the app's charts and by the reader's
    `lifestyle_limit` status, and every writer routes through here, so the
    guard lives in this function rather than in each caller.
    """
    canonical = canonical_amount(log_type, quantity, unit, kind)
    if canonical is None:
        stored = LIFESTYLE_UNITS.get(log_type, ("serving", "serving"))[0]
        raise ValueError(
            f"{log_type} is tracked in {stored}; no sanctioned size for "
            f"'{unit}' of '{kind or log_type}' -- convert before writing."
        )
    quantity, unit = canonical
    # The two derived columns, exactly as mhn-spring fills them for a log with
    # no catalogue drink (ManualTrackingServiceImpl:208-212): the same number
    # as `quantity`, in whichever column the type's own measure is.
    volume = unit == "ml"
    row = LifestyleLog(
        user_id=user_id,
        log_type=log_type,
        quantity=quantity,
        unit=unit,
        volume_ml=quantity if volume else None,
        servings=None if volume else quantity,
        metadata_json={"source": "davi_chat"},
        logged_at=logged_at or utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def lifestyle_totals(
    db: AsyncSession, user_id: uuid.UUID, since: datetime
) -> dict[str, LifestyleTotal]:
    """Per log type, how much was logged -- in the type's own unit.

    A plain ``SUM(quantity)``, which is the same arithmetic
    ``lifestyle_daily_total`` holds for the primary metrics (see
    ``LIFESTYLE_UNITS`` for why that is unit-safe). Reading the log rather
    than the rollup keeps a row Davi just wrote visible: the rollup only
    catches up when Spring reconciles.
    """
    rows = (
        await db.execute(
            select(LifestyleLog.log_type, func.sum(LifestyleLog.quantity))
            .where(LifestyleLog.user_id == user_id, LifestyleLog.logged_at >= since)
            .group_by(LifestyleLog.log_type)
        )
    ).all()
    return {
        log_type: LifestyleTotal(
            log_type=log_type,
            total=float(total),
            unit=LIFESTYLE_UNITS.get(log_type, ("serving", "serving"))[1],
        )
        for log_type, total in rows
    }


async def lifestyle_days(
    db: AsyncSession,
    user_id: uuid.UUID,
    log_type: str,
    *,
    since: date,
    until: date,
) -> dict[date, float]:
    """Which CALENDAR DAYS in ``[since, until)`` hold a log of one type.

    Reads ``lifestyle_daily_total`` rather than grouping ``lifestyle_log`` by
    ``date(logged_at)``: Spring assigned both these buckets and
    ``sahha_daily_total``'s in its own write-time zone, so the two sides of a
    same-day comparison line up. Grouping in SQL would produce UTC days and
    put a late-evening coffee on a different day from the night's sleep.

    The value is the rollup's per-day ``SUM(quantity)``. Counting the DAYS is
    what most callers want; the one sanctioned way to add the numbers up is
    ``lifestyle_calendar_total``, which attaches the metric's unit.
    """
    rows = (
        await db.execute(
            select(LifestyleDailyTotal.bucket_start, LifestyleDailyTotal.total)
            .where(
                LifestyleDailyTotal.user_id == user_id,
                LifestyleDailyTotal.metric == log_type,
                LifestyleDailyTotal.entries >= 1,
                LifestyleDailyTotal.bucket_start >= since,
                LifestyleDailyTotal.bucket_start < until,
            )
        )
    ).all()
    return {bucket: float(total) for bucket, total in rows}


async def first_lifestyle_day(
    db: AsyncSession, user_id: uuid.UUID
) -> date | None:
    """The earliest day the reader holds ANY lifestyle daily total, or None.

    A day before this one is a day the reader was not tracking at all -- not a
    day they went without the habit. ``co_occurrence``'s "did not log" group
    counted those as evidence, so a reader who started logging coffee a week
    ago got a finding built out of 21 days of nothing, which is the most
    likely first question this feature ever sees.
    """
    return (
        await db.execute(
            select(func.min(LifestyleDailyTotal.bucket_start)).where(
                LifestyleDailyTotal.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def latest_lifestyle_day(
    db: AsyncSession, user_id: uuid.UUID, log_type: str
) -> tuple[date, LifestyleTotal] | None:
    """The most recent day the reader logged one metric, at ANY age.

    The lifestyle twin of the unbounded ``wearable_totals`` probe: when the
    asked-about window is empty, naming the last day that was not -- and what
    was on it -- is the difference between "you have not logged any water this
    week" and a reader who thinks the app lost their data.
    """
    row = (
        await db.execute(
            select(LifestyleDailyTotal.bucket_start, LifestyleDailyTotal.total)
            .where(
                LifestyleDailyTotal.user_id == user_id,
                LifestyleDailyTotal.metric == log_type,
                LifestyleDailyTotal.entries >= 1,
            )
            .order_by(LifestyleDailyTotal.bucket_start.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], LifestyleTotal(
        log_type=log_type,
        total=float(row[1]),
        unit=LIFESTYLE_UNITS.get(log_type, ("serving", "serving"))[1],
    )


async def lifestyle_calendar_total(
    db: AsyncSession,
    user_id: uuid.UUID,
    log_type: str,
    *,
    since: date,
    until: date,
) -> tuple[LifestyleTotal | None, int]:
    """An AMOUNT over a half-open calendar span, and how many days carried a log.

    Reads ``lifestyle_daily_total`` rather than summing ``lifestyle_log``
    between two instants. A calendar day is only a calendar day in the zone
    that assigned it, and ``app.tracking.zone`` is not recoverable here --
    ``logged_at >= <some midnight>`` is a UTC day and would put a late-evening
    log on the wrong side of "yesterday". Summing the rollup is unit-safe: the
    platform stores exactly one unit per metric (``LIFESTYLE_UNITS``) and
    rejects any other.

    The cost is freshness -- a row Davi has just written reaches this table
    only when Spring reconciles (03:15 daily) -- which is exactly what the
    returned day count exists to make visible to the reader.
    """
    days = await lifestyle_days(db, user_id, log_type, since=since, until=until)
    if not days:
        return None, 0
    return (
        LifestyleTotal(
            log_type=log_type,
            total=sum(days.values()),
            unit=LIFESTYLE_UNITS.get(log_type, ("serving", "serving"))[1],
        ),
        len(days),
    )


def window_start(period: str, now: datetime | None = None) -> datetime:
    """The ROLLING window an open-ended period opens at. ``None`` for calendar
    periods -- those are ``calendar_window``'s, and are bounded at both ends."""
    now = now or utcnow()
    if period == "today":
        # Midnight, not "now minus nothing": "how much water today" is read off
        # `lifestyle_log` rather than the overnight rollup, and the rolling
        # readers take a datetime floor.
        return datetime.combine(now.date(), time.min, tzinfo=UTC)
    days = {"week": 7, "month": 30, "year": 365}.get(period, 7)
    return now - timedelta(days=days)


def week_start(day: date) -> date:
    """The SUNDAY that opens the tracking week holding ``day``.

    mhn-spring's convention, stated in two places and derived nowhere else:
    ``TrackingGrain.bucketOf`` uses
    ``TemporalAdjusters.previousOrSame(DayOfWeek.SUNDAY)``, and
    ``SahhaRollupDao.bucketExpression(WEEK)`` spells the same thing in SQL as
    ``date_trunc('week', d + 1 day) - 1 day`` -- deliberately NOT PostgreSQL's
    Monday. Python's ``weekday()`` is Monday=0, so the Sunday is
    ``(weekday() + 1) % 7`` days back.

    This computes the CURRENT week from today's date. It never re-derives a
    stored ``bucket_start``: those are read as given and only compared against
    the span this produces.
    """
    return day - timedelta(days=(day.weekday() + 1) % 7)


CALENDAR_PERIODS = ("today", "yesterday", "this_week", "last_week")


def calendar_window(
    period: str, today: date | None = None
) -> tuple[date, date] | None:
    """Half-open ``[since, until)`` in calendar days, or ``None`` if rolling.

    ``this_week`` is the calendar week TO DATE and includes today, so it is
    normally a PARTIAL week -- callers must say so rather than presenting three
    days as a week's total. ``last_week`` is the previous complete week.

    ``today`` defaults to the UTC date, not ``app.tracking.zone``: that zone is
    empty by default in mhn-spring and unrecoverable from the data, so within a
    few hours of midnight a window can be one day out. Same anchor
    ``handle_correlation_query`` already uses.
    """
    today = today or utcnow().date()
    if period == "today":
        return today, today + timedelta(days=1)
    if period == "yesterday":
        return today - timedelta(days=1), today
    if period == "this_week":
        return week_start(today), today + timedelta(days=1)
    if period == "last_week":
        start = week_start(today) - timedelta(days=7)
        return start, start + timedelta(days=7)
    return None


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
                # Soft delete: a course removed in the app keeps
                # `stopped_at IS NULL`, so without this it reads as current.
                MedicineTracking.deleted_at.is_(None),
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
    db: AsyncSession, user_id: uuid.UUID, since: datetime,
    until: datetime | None = None,
) -> dict[str, MetricPoint]:
    """Latest value per manual-tracking type recorded in ``[since, until)``.

    ``until`` is what a calendar period needs: without an upper bound
    "how many steps yesterday" answers with today's entry. ``manual_tracking``
    carries only instants -- no Spring-assigned day column -- so a calendar
    caller has nothing but UTC midnights to bound it with, and is off by the
    tracking zone's offset. The reply names the date it found, so a reader can
    see which day answered.
    """
    where = [
        ManualTracking.user_id == user_id,
        ManualTracking.value.is_not(None),
        ManualTracking.type.in_(MANUAL_METRIC_ORDER),
        ManualTracking.effective_from >= since,
    ]
    if until is not None:
        where.append(ManualTracking.effective_from < until)
    rows = (
        await db.execute(
            select(ManualTracking)
            .where(*where)
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
# Wearable (Sahha) rollups — read only
# --------------------------------------------------------------------------- #
# Per-metric aggregation, ported from mhn-spring's SahhaMetricCatalog. The
# rollups carry NO unit column and their `total` is a SUM regardless, so this
# table is the only thing that stops a week of resting heart rate reading as
# ~420 bpm.
#
# metric -> (display label, unit as stored, is_sum)
SAHHA_METRICS: dict[str, tuple[str, str, bool]] = {
    "steps":                        ("steps",              "count",  True),
    "sleep_duration":               ("sleep",              "minute", True),
    "heart_rate_resting":           ("resting heart rate", "bpm",    False),
    "heart_rate_variability_sdnn":  ("HRV (SDNN)",         "ms",     False),
    "heart_rate_variability_rmssd": ("HRV (RMSSD)",        "ms",     False),
}

# SDNN and RMSSD are DIFFERENT measures of the same thing, and which one a
# device reports is the device's choice. Asking for one and finding nothing is
# not an answer -- read the sibling before saying the reader has no HRV. They
# are never merged: the label names whichever one answered.
HRV_SIBLING = {
    "heart_rate_variability_sdnn": "heart_rate_variability_rmssd",
    "heart_rate_variability_rmssd": "heart_rate_variability_sdnn",
}


def sahha_meta(metric: str) -> tuple[str, str, bool]:
    """Catalogue entry for a metric, or a bare-number default for an unknown one.

    Sahha's vocabulary grows, so an unrecognised metric must degrade to a
    number without a unit -- never a KeyError in a read path.
    """
    return SAHHA_METRICS.get(metric, (metric, "", False))


@dataclass(frozen=True)
class WearablePoint:
    bucket_start: date
    value: float   # the SUM for sum-metrics, total/entries for the rest
    entries: int   # READINGS in the bucket -- a device syncing hourly makes
                   # this many times the day count. Never a day count.
    # Distinct days in the bucket that hold anything (V40: "what a weekly or
    # monthly point is divided by to read 'per day'. Always 1 at day grain").
    # `days_counted < 7` on a weekly point is how a PARTIAL week is known --
    # the week in progress, or a device with gaps.
    days_counted: int


_SAHHA_GRAIN = {"day": SahhaDailyTotal, "week": SahhaWeeklyTotal}


def _headline(metric: str, total: float | Decimal, entries: int) -> float:
    """total for SUM metrics, the mean otherwise — mhn-spring's own formula
    (SahhaHealthServiceImpl.headline: `total` when SUM or entries == 1, else
    total/entries at 2dp HALF_UP). Copied so the two agree.

    The division is done in Decimal with ROUND_HALF_UP, not with `round()`:
    Python rounds a tie to even and Java's BigDecimal rounds it up, so 421/8
    is 52.62 one side and 52.63 the other. Ties land whenever `entries` is a
    power of two, which is ordinary, and chat and the app must not print
    different numbers for the same week.

    An unknown metric defaults to the MEAN, never the sum: a mean of a counter
    reads low, while a sum of a rate reads like a medical emergency.
    """
    is_sum = sahha_meta(metric)[2]
    if is_sum or entries == 1:
        return float(total)
    if entries < 1:
        # No readings means no mean. `wearable_totals` drops these rows; this
        # keeps a direct caller from dividing by zero -- and from the older
        # `entries <= 1` behaviour, which showed a week's SUM of resting heart
        # rate (420 bpm) as if it were the average.
        return 0.0
    mean = (Decimal(str(total)) / Decimal(entries)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(mean)


def wearable_display(metric: str, value: float) -> tuple[float, str]:
    """One headline value in the units a PERSON is shown, and that unit.

    Sleep is stored in MINUTES and shown in hours. This is the only place that
    conversion happens, so the sentence and the chart cannot disagree about the
    same week -- a chart of raw minutes beside a sentence in hours is two
    different answers to one question.
    """
    unit = sahha_meta(metric)[1]
    if unit == "minute":
        return round(value / 60, 1), "h"
    if unit == "count":
        return round(value), "steps"
    # Never more than the 2dp mhn-spring's own mean carries. A single-reading
    # bucket passes numeric(16,4) straight through, and "59.995 bpm" reads as
    # instrument-grade precision no wrist device has.
    return round(value, 2), unit


def format_wearable(metric: str, value: float) -> str:
    """Reader-facing text for one headline value."""
    shown, unit = wearable_display(metric, value)
    if unit == "h":
        return f"{shown:.1f} h"
    if unit == "steps":
        return f"{shown:,.0f} steps"
    return f"{shown:g} {unit}".strip()


async def wearable_totals(
    db: AsyncSession,
    user_id: uuid.UUID,
    metric: str,
    *,
    grain: str = "day",
    limit: int = 7,
    since: date | None = None,
    until: date | None = None,
) -> list[WearablePoint]:
    """The last `limit` buckets for one wearable metric, oldest first.

    Reads the PRE-AGGREGATED rollup rather than recomputing from
    sahha_biomarker: it is the number mhn-spring's own charts show; weekly
    buckets open on SUNDAY, which Python's Monday-based weekday()/isocalendar()
    would shift by a day; `day` was assigned in Spring's write-time zone, which
    is not recoverable from the data; and the rollups were built
    `WHERE value IS NOT NULL`, so non-numeric metrics (sleep_start_time and
    friends) can never appear here.

    A bucket with no readings is an ABSENT ROW, not a zero. Gaps stay gaps.

    ``since``/``until`` bound ``bucket_start`` to a HALF-OPEN CALENDAR span,
    never to a rolling `window_start()` offset: these are calendar buckets, and
    slicing them by "now minus N days" includes or drops an edge bar depending
    on the hour the question is asked. Bounded, because the last N buckets that
    EXIST are not the last N days -- for a sparse or lapsed device they can span
    months, and the chart's bars have to be the days the sentence's total
    covers or a reader who adds them up gets a different figure with no way to
    see why. Unbounded is still available and still means "the last N rows".

    A row with ``entries < 1`` is dropped: a bucket holding no readings carries
    no number, and for an AVERAGE metric there is nothing to divide by.
    """
    model = _SAHHA_GRAIN[grain]
    where = [model.user_id == user_id, model.metric == metric, model.entries >= 1]
    if since is not None:
        where.append(model.bucket_start >= since)
    if until is not None:
        where.append(model.bucket_start < until)
    rows = (
        await db.execute(
            select(model)
            .where(*where)
            .order_by(model.bucket_start.desc())
            .limit(limit)
        )
    ).scalars().all()
    points = [
        WearablePoint(
            bucket_start=r.bucket_start,
            # r.total NOT floated first: numeric(16,4) arrives as a Decimal on
            # Postgres and the exact value is what the HALF_UP mean needs.
            value=_headline(metric, r.total, r.entries),
            entries=r.entries,
            days_counted=r.days_counted,
        )
        for r in rows
    ]
    points.reverse()          # chronological, for charting
    return points


async def wearable_latest(
    db: AsyncSession,
    user_id: uuid.UUID,
    metrics: Sequence[str],
    *,
    since: date,
    grain: str = "week",
) -> dict[str, WearablePoint]:
    """Latest bucket per metric, no older than ``since``, in ONE round trip.

    The batched sibling of ``wearable_totals``, for the caller that wants four
    or five metrics at once: five index lookups are five network hops on the
    one turn that asks for everything. Same table, same ``entries >= 1`` rule
    and the same ``_headline`` formula, so the two readers cannot print
    different numbers for the same week.

    ``since`` is what keeps the scan bounded AND what keeps the answer honest:
    ``wearable_totals`` returns the last rows that EXIST at any age, so a
    device that stopped syncing a year ago would answer "this week" with last
    August's number.
    """
    if not metrics:
        return {}
    model = _SAHHA_GRAIN[grain]
    rows = (
        await db.execute(
            select(model)
            .where(
                model.user_id == user_id,
                model.metric.in_(list(metrics)),
                model.entries >= 1,
                model.bucket_start >= since,
            )
            .order_by(model.bucket_start.desc())
        )
    ).scalars().all()
    out: dict[str, WearablePoint] = {}
    for r in rows:
        if r.metric in out:          # ordered newest-first; the first wins
            continue
        out[r.metric] = WearablePoint(
            bucket_start=r.bucket_start,
            value=_headline(r.metric, r.total, r.entries),
            entries=r.entries,
            days_counted=r.days_counted,
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


async def medical_records(
    db: AsyncSession, user_id: uuid.UUID, *, type_: str | None = None
) -> list[MedicalCondition]:
    """The reader's own conditions / surgeries / allergies — one table, one read.

    THE single place the two invisibility rules are applied, because both are
    columns a reader can easily forget:

    * ``private`` — the owning app hides these; NULL means not private (the
      column's own default), so a pre-column row stays visible.
    * ``deleted_at`` — a SOFT delete. A condition the reader deleted in the app
      keeps ``status = 'active'`` forever, so a query that filters on status
      alone reports a deleted condition as a current one.

    Own data only; there is no family path into it. Callers split by ``type``
    in Python rather than issuing one query per type.
    """
    return list(
        (
            await db.execute(
                select(MedicalCondition)
                .where(
                    MedicalCondition.user_id == user_id,
                    MedicalCondition.deleted_at.is_(None),
                    sa.or_(
                        MedicalCondition.private.is_(False),
                        MedicalCondition.private.is_(None),
                    ),
                    *([MedicalCondition.type == type_] if type_ else []),
                )
                .order_by(MedicalCondition.id)
            )
        ).scalars().all()
    )


async def medication_allergies(
    db: AsyncSession, user_id: uuid.UUID
) -> list[MedicalCondition]:
    """The reader's own MEDICATION allergies, worst first.

    Own data only — this is never called for a family member.
    """
    try:
        rows = [
            r for r in await medical_records(db, user_id, type_="allergy")
            if r.category == "medication"
        ]
    except Exception:  # noqa: BLE001 — a read must never break a reply
        logger.warning("medication allergy read failed", exc_info=True)
        return []

    return sorted(rows, key=allergy_rank)


def allergy_rank(row: MedicalCondition) -> int:
    """Worst first; an unrecorded severity sorts last, never as severe."""
    return {"severe": 0, "medium": 1, "mild": 2}.get(
        (row.severity or "").lower(), 3
    )


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
        # RECORDED cycles only -- which is every row here. `period_tracking`
        # has no `is_predicted` column in any environment, so the filter that
        # used to sit here matched nothing, raised UndefinedColumn and left
        # this read returning an empty list in production. A prediction the
        # app draws is not stored in this table at all; if that ever changes,
        # filter on the column they add rather than one we assumed.
        cycles = (
            await db.execute(
                select(PeriodTracking)
                .where(PeriodTracking.user_id == user_id)
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


# --------------------------------------------------------------------------- #
# Biomarker trend series — mhn-spring's materialised feed (their V31)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SeriesReading:
    """One point on a biomarker's trend line."""

    at: date | None
    value: float
    unit: str | None
    status: str | None
    report_id: int | None


@dataclass(frozen=True)
class ThpSeries:
    """A biomarker's whole history, as the mobile graphs see it."""

    thp_key: str
    name: str
    unit: str | None
    reference_range: str | None
    readings: tuple[SeriesReading, ...]


def _reading_date(raw) -> date | None:
    """Readings carry an ISO date string; tolerate a full timestamp."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


async def thp_series_names(
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[int, str, str]]:
    """(id, thp_key, display name) for every biomarker the reader has on file.

    Deliberately does NOT select ``readings``: a reader with a long lab history
    has one JSON array per biomarker, and the caller only needs the names to
    decide which single series to open.
    """
    rows = (
        await db.execute(
            select(
                UserThpSeries.id, UserThpSeries.thp_key, UserThpSeries.name
            ).where(UserThpSeries.user_id == user_id)
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def thp_series(
    db: AsyncSession, user_id: uuid.UUID, series_id: int
) -> ThpSeries | None:
    """Open one series by id, oldest reading first.

    Owner-scoped: ``user_id`` is in the WHERE clause, not assumed from the id.
    Readings whose value is not numeric are dropped rather than guessed at —
    a lab line like "not detected" is real, but it is not a point on a graph.
    """
    row = (
        await db.execute(
            select(UserThpSeries).where(
                UserThpSeries.id == series_id,
                UserThpSeries.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    points: list[SeriesReading] = []
    for item in row.readings or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("value")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        report_id = item.get("reportId")
        points.append(
            SeriesReading(
                at=_reading_date(item.get("date")),
                value=value,
                unit=item.get("unit") or row.unit,
                status=(str(item.get("status")).strip().lower()
                        if item.get("status") else None),
                report_id=report_id if isinstance(report_id, int) else None,
            )
        )
    # Upstream stores oldest-first; sort anyway so a rebuilt or merged row
    # cannot hand the caller a graph that runs backwards. Undated points sort
    # last so they never masquerade as the earliest reading.
    points.sort(key=lambda p: (p.at is None, p.at or date.min))
    return ThpSeries(
        thp_key=row.thp_key,
        name=row.name,
        unit=row.unit,
        reference_range=row.reference_range,
        readings=tuple(points),
    )


# --------------------------------------------------------------------------- #
# Targets the reader set for themselves
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Target:
    """One self-set goal or limit, ready to state back plainly."""

    kind: str            # "limit" (a ceiling) | "goal" (something aimed at)
    metric: str
    value: float
    unit: str | None
    direction: str | None   # body goals only: lose / gain / maintain
    since: date | None



async def targets(db: AsyncSession, user_id: uuid.UUID) -> list[Target]:
    """Every target the reader has set: lifestyle limits, body goals, wearable
    goals — currently in force, newest per metric.

    Three tables with the same shape, and nothing in Davi read any of them: a
    reader who had set a daily water target in the app and asked about it here
    was told there was nothing on record.

    ONE query, not three. The summary turn has a measured query budget
    (`test_a_summary_turn_stays_within_its_own_budget`) and three round trips
    for three small identical-shape tables is exactly the read that budget
    exists to stop. Same shape, so UNION ALL and let the database do it.
    """
    def _leg(model, metric_col, value_col, kind: str, direction):
        return select(
            sa.literal(kind).label("kind"),
            sa.cast(metric_col, sa.String).label("metric"),
            sa.cast(value_col, sa.Float).label("value"),
            model.unit.label("unit"),
            direction.label("direction"),
            model.effective_from.label("since"),
        ).where(model.user_id == user_id, value_col.is_not(None))

    rows = (await db.execute(
        _leg(LifestyleLimit, LifestyleLimit.metric, LifestyleLimit.limit_value,
             "limit", sa.cast(sa.literal(None), sa.String))
        .union_all(
            _leg(BodyMeasurementGoal, BodyMeasurementGoal.type,
                 BodyMeasurementGoal.goal_value, "goal",
                 sa.cast(BodyMeasurementGoal.direction, sa.String)),
            _leg(SahhaGoal, SahhaGoal.metric, SahhaGoal.goal_value, "goal",
                 sa.cast(sa.literal(None), sa.String)),
        )
    )).all()

    # Newest row per metric whose `effective_from` has actually arrived. These
    # tables are histories, so the newest row is not always the current one: a
    # goal the reader dated for next Monday is a plan, not today's target.
    # Undated rows are treated as always in force.
    today = utcnow().date()
    best: dict[str, Target] = {}
    for kind, metric, value, unit, direction, since in rows:
        # The WHERE already excludes NULL values; the type checker cannot see
        # that through a UNION, and a target with no number is not one anyway.
        if value is None:
            continue
        if since is not None and since > today:
            continue
        prev = best.get(metric)
        if prev is not None and (prev.since or date.min) > (since or date.min):
            continue
        best[metric] = Target(kind, metric, float(value), unit, direction, since)

    return sorted(best.values(), key=lambda t: (t.kind, t.metric))


def target_phrase(t: Target) -> str:
    """Plain words for one target. Descriptive only — never a verdict on
    whether the reader is meeting it."""
    metric = t.metric.replace("_", " ")
    amount = f"{t.value:g}{' ' + t.unit if t.unit else ''}"
    if t.kind == "limit":
        return f"{metric} no more than {amount} a day"
    if t.direction and t.direction != "maintain":
        return f"{metric} {t.direction} to {amount}"
    return f"{metric} {amount}"

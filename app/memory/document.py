"""Build, read and refresh the per-user memory document.

The read path is one primary-key lookup. The build path does the twenty-odd
queries that used to run on every turn, and runs them when something changes
instead.

THE BUDGET IS THE DESIGN. Every token of this block is charged on every turn,
outside the cached prefix, forever — derived at ~$5,472/month per +50 tokens at
1M users (project_docs/model-cost.md). So the question is never "could this be
useful?" but "is this worth paying for on every turn for the rest of the
product's life?". Most things are not, and belong behind a tool the model calls
when the question actually needs them.

WHAT IS DELIBERATELY ABSENT, and why:

* **Anyone else's data.** Family permission is checked live on every read; a
  document that absorbed a relative's result would survive the revocation that
  should have removed it. Family stays a live gated call.
* **Contraception, life stage, PCOS, cycle dates.** Not needed to answer a
  headache question. Only pregnancy/breastfeeding travels, because it changes
  what is safe to say about many medicines.
* **Raw document text, and anything from an unapproved source.**
* **Anything computed here that another service already computes.** Adherence
  is asked of mhn-spring; cycle predictions are the app's job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.profile import get_profile
from app.coredata.service import (
    active_medications,
    cycle_snapshot,
    latest_documents,
    lifestyle_totals,
    medication_allergies,
    pregnancy_safety_flag,
    recent_lab_values,
)
from app.models.common import utcnow
from app.models.memory_document import SCHEMA_VERSION, UserMemoryDocument
from app.rag.prompt import estimate_tokens
from app.telemetry import record_fail_open

logger = logging.getLogger("davi.memory")

# The ceiling. A block over this is trimmed at build time, lowest-value first,
# rather than being allowed to grow into the retrieved-knowledge budget.
MAX_PROMPT_TOKENS = 900

# How stale a document may be before the caller falls back to live assembly.
# One hour: long enough that rebuilds are cheap, short enough that a document
# uploaded this morning is reflected by lunchtime.
FRESHNESS = timedelta(hours=1)

# Caps, chosen so the block cannot grow without a code change.
MAX_LABS = 6
MAX_DOCUMENTS = 5
MAX_MEDICATIONS = 6
MAX_CONDITIONS = 6


@dataclass(frozen=True)
class BuiltDocument:
    document: dict
    prompt_block: str
    source_hash: str
    token_estimate: int


def _hash(document: dict) -> str:
    """Stable across runs: sorted keys, no whitespace drift."""
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _gather(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Everything the document holds. Each part fails open on its own.

    Independently matters: a lab read that breaks must not also cost the
    reader their medication list.
    """
    document: dict = {"schema_version": SCHEMA_VERSION}

    try:
        view = await get_profile(db, user_id)
        data = view.data if view.has_consent else {}
        profile = {
            k: v
            for k, v in {
                "age_band": data.get("age_band"),
                "sex": data.get("sex"),
                "conditions": (data.get("chronic_conditions") or [])[:MAX_CONDITIONS],
                "medications": (data.get("current_medications") or [])[
                    :MAX_MEDICATIONS
                ],
                "allergies": (data.get("allergies") or [])[:4],
            }.items()
            if v
        }
        if profile:
            document["profile"] = profile
    except Exception:  # noqa: BLE001
        logger.warning("profile gather failed", exc_info=True)
        record_fail_open("memory_profile")

    try:
        allergies = await medication_allergies(db, user_id)
        named = [
            {"name": a.name, "severity": a.severity, "reaction": a.reaction}
            for a in allergies
            if (a.severity or "").lower() in ("severe", "medium")
        ][:3]
        if named:
            document["medication_allergies"] = named
    except Exception:  # noqa: BLE001
        logger.warning("allergy gather failed", exc_info=True)
        record_fail_open("memory_allergies")

    try:
        flag = pregnancy_safety_flag(await cycle_snapshot(db, user_id))
        if flag:
            document["safety_note"] = flag
    except Exception:  # noqa: BLE001
        logger.warning("cycle gather failed", exc_info=True)
        record_fail_open("memory_cycle")

    try:
        meds = await active_medications(db, user_id, limit=MAX_MEDICATIONS)
        if meds:
            document["active_medications"] = meds
    except Exception:  # noqa: BLE001
        logger.warning("medication gather failed", exc_info=True)
        record_fail_open("memory_medications")

    try:
        labs = await recent_lab_values(db, user_id, max_values=MAX_LABS)
        rows = [
            {
                "test": v.name,
                "value": v.value,
                "unit": v.unit,
                # Provenance, so a reply can cite it and a stale entry is
                # traceable to the record that changed.
                "on": v.at.date().isoformat() if v.at else None,
            }
            for v in labs[:MAX_LABS]
        ]
        if rows:
            document["recent_labs"] = rows
    except Exception:  # noqa: BLE001
        logger.warning("lab gather failed", exc_info=True)
        record_fail_open("memory_labs")

    try:
        # Own documents only, and NOT private ones: a document the reader
        # marked private is not something to carry into every prompt. The
        # viewer is the owner, so the family gate is not in play here — and
        # deliberately so, since a family member's document must never enter
        # this block (see the module docstring).
        docs = await latest_documents(
            db,
            user_id,
            ["reports", "scans_imaging"],
            include_private=False,
            limit=MAX_DOCUMENTS,
            viewer_id=user_id,
        )
        rows = [
            {
                "title": d.title,
                "on": d.created_at.date().isoformat() if d.created_at else None,
                "id": d.doc_id,
                "kind": d.kind,
            }
            for d in (docs or [])[:MAX_DOCUMENTS]
            if d.title
        ]
        if rows:
            document["recent_documents"] = rows
    except Exception:  # noqa: BLE001
        logger.warning("document gather failed", exc_info=True)
        record_fail_open("memory_documents")

    try:
        totals = await lifestyle_totals(
            db, user_id, utcnow() - timedelta(days=30)
        )
        if totals:
            document["habits_30d"] = {
                k: round(v, 1) for k, v in list(totals.items())[:6]
            }
    except Exception:  # noqa: BLE001
        logger.warning("habit gather failed", exc_info=True)
        record_fail_open("memory_habits")

    return document


def render(document: dict) -> str:
    """The prompt block. Deterministic — same document, same bytes.

    Determinism is not tidiness here: the block sits behind a cache breakpoint,
    and text that varies between identical rebuilds would break the cache for
    that reader on every turn.
    """
    parts: list[str] = []

    # Safety first, and unconditionally first — a reader who is pregnant or
    # severely allergic should not have that fact below their step count.
    if note := document.get("safety_note"):
        parts.append(note)

    if allergies := document.get("medication_allergies"):
        rendered = "; ".join(
            f"{a['name']}"
            + (f" ({a['reaction']})" if a.get("reaction") else "")
            + (f" — {a['severity']}" if a.get("severity") else "")
            for a in allergies
        )
        parts.append(f"Medication allergies on record: {rendered}.")

    profile = document.get("profile") or {}
    bits: list[str] = []
    if profile.get("conditions"):
        bits.append("conditions: " + ", ".join(profile["conditions"]))
    if profile.get("medications"):
        bits.append("self-reported medicines: " + ", ".join(profile["medications"]))
    if meds := document.get("active_medications"):
        bits.append("tracked medicines: " + ", ".join(meds))
    if bits:
        parts.append("On record — " + "; ".join(bits) + ".")

    if labs := document.get("recent_labs"):
        rendered = "; ".join(
            f"{v['test']} {v['value']}{(' ' + v['unit']) if v.get('unit') else ''}"
            + (f" ({v['on']})" if v.get("on") else "")
            for v in labs
        )
        # Dated on purpose: "HbA1c 7.4% (12 Aug)" is honest in a way the bare
        # number is not, and a stale value at least names when it was true.
        parts.append(f"Recent results: {rendered}.")

    if docs := document.get("recent_documents"):
        rendered = "; ".join(
            f"{d['title']}" + (f" ({d['on']})" if d.get("on") else "")
            for d in docs
        )
        parts.append(f"Recent documents: {rendered}.")

    if habits := document.get("habits_30d"):
        rendered = ", ".join(f"{k} {v}" for k, v in sorted(habits.items()))
        parts.append(f"Logged in the last 30 days: {rendered}.")

    if not parts:
        return ""
    parts.append(
        "This is the reader's own recorded data (cite as [P]). It is context, "
        "never a diagnosis, and it does not include anyone else's records."
    )
    return " ".join(parts)


# Trimmed in this order when the block is over budget: cheapest to lose first.
# Safety notes and allergies are absent from this list deliberately — they are
# never trimmed.
_TRIM_ORDER = ("habits_30d", "recent_documents", "recent_labs", "profile")


def _fit(document: dict) -> tuple[dict, str, int]:
    """Render, and drop the lowest-value sections until it fits."""
    document = dict(document)
    block = render(document)
    tokens = estimate_tokens(block) if block else 0
    for key in _TRIM_ORDER:
        if tokens <= MAX_PROMPT_TOKENS:
            break
        if key in document:
            document.pop(key)
            document.setdefault("trimmed", []).append(key)
            block = render(document)
            tokens = estimate_tokens(block) if block else 0
    return document, block, tokens


async def build(db: AsyncSession, user_id: uuid.UUID) -> BuiltDocument:
    """Assemble a document. Never raises."""
    document = await _gather(db, user_id)
    document, block, tokens = _fit(document)
    return BuiltDocument(
        document=document,
        prompt_block=block,
        source_hash=_hash(document),
        token_estimate=tokens,
    )


async def get(
    db: AsyncSession, user_id: uuid.UUID
) -> UserMemoryDocument | None:
    try:
        return (
            await db.execute(
                select(UserMemoryDocument).where(
                    UserMemoryDocument.user_id == user_id
                )
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001
        logger.warning("memory document read failed", exc_info=True)
        return None


def is_fresh(row: UserMemoryDocument | None, now: datetime | None = None) -> bool:
    """Fresh enough to use, and of a shape this code understands.

    A row at an older schema version is treated as stale rather than read: the
    fields it lacks are fields the renderer expects.
    """
    if row is None or row.schema_version != SCHEMA_VERSION:
        return False
    built = row.built_at
    if built.tzinfo is None:
        # SQLite has no timezone type; a value written aware comes back naive.
        from datetime import UTC

        built = built.replace(tzinfo=UTC)
    return (now or utcnow()) - built <= FRESHNESS


async def refresh(
    db: AsyncSession, user_id: uuid.UUID
) -> UserMemoryDocument | None:
    """Rebuild and store. Returns the row, or None if it could not be written.

    Identical inputs are a no-op beyond a timestamp touch: `source_hash` is the
    same, so the stored text does not change and the reader's cached prefix
    survives.
    """
    built = await build(db, user_id)
    try:
        row = await get(db, user_id)
        if row is None:
            row = UserMemoryDocument(
                user_id=user_id,
                document=built.document,
                prompt_block=built.prompt_block,
                source_hash=built.source_hash,
                built_at=utcnow(),
                schema_version=SCHEMA_VERSION,
                token_estimate=built.token_estimate,
            )
            db.add(row)
        elif row.source_hash == built.source_hash and row.schema_version == SCHEMA_VERSION:
            # Nothing changed. Touch the clock so it stops being stale, and
            # leave prompt_block byte-identical so the cache holds.
            row.built_at = utcnow()
        else:
            row.document = built.document
            row.prompt_block = built.prompt_block
            row.source_hash = built.source_hash
            row.built_at = utcnow()
            row.schema_version = SCHEMA_VERSION
            row.token_estimate = built.token_estimate
        await db.flush()
        return row
    except Exception:  # noqa: BLE001 — memory is an optimisation
        logger.warning("memory document write failed", exc_info=True)
        record_fail_open("memory_document_write")
        return None

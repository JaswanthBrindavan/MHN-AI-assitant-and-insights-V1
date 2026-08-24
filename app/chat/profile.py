"""Reading, writing and erasing the user profile.

Three rules this module exists to enforce:

1. **Consent first.** Nothing is stored without a ``chat_personalization``
   grant in the append-only consent ledger.
2. **Show it back.** Whatever is held is renderable in one block, so a reader
   can see exactly what the assistant knows.
3. **Forget on request.** One call erases the profile AND the long-term topic
   memory, because a reader asking to be forgotten does not mean "half of me".

Fail-open on reads: a profile lookup failure must never cost someone an answer.
Fail-CLOSED on writes: if consent cannot be confirmed, nothing is stored.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common import utcnow
from app.models.core import ConsentLedger
from app.models.profile import UserProfile
from app.services.pedigree import FAMILY_RISK_PURPOSE  # noqa: F401  (sibling purpose)

logger = logging.getLogger("davi.profile")

PERSONALIZATION_PURPOSE = "chat_personalization"

# Fields a caller may set. Anything else is ignored rather than stored — an
# unknown key is a bug or an attack, and neither should become a record.
WRITABLE_FIELDS = (
    "age_band",
    "sex",
    "communication_style",
    "preferred_language",
    "chronic_conditions",
    "current_medications",
    "allergies",
    "goals",
    "is_pregnant",
)

_LIST_FIELDS = ("chronic_conditions", "current_medications", "allergies", "goals")
_MAX_ITEMS = 20
_MAX_ITEM_CHARS = 80


@dataclass(frozen=True)
class ProfileView:
    """What is held, in a shape both the API and the prompt can use."""

    data: dict
    has_consent: bool

    @property
    def is_empty(self) -> bool:
        return not any(v for v in self.data.values())


async def has_personalization_consent(
    db: AsyncSession, user_id: uuid.UUID
) -> bool:
    grant = await _latest_grant(db, user_id)
    return grant is not None


async def _latest_grant(
    db: AsyncSession, user_id: uuid.UUID
) -> ConsentLedger | None:
    """The most recent personalization event, if it was a grant.

    The ledger is append-only, so a revocation is a NEW row rather than a
    deletion — the newest event wins.
    """
    row = (
        await db.execute(
            select(ConsentLedger)
            .where(
                ConsentLedger.user_id == user_id,
                ConsentLedger.purpose == PERSONALIZATION_PURPOSE,
            )
            .order_by(ConsentLedger.created_at.desc())
        )
    ).scalars().first()
    if row is None or row.action != "granted":
        return None
    return row


async def grant_personalization(
    db: AsyncSession, user_id: uuid.UUID, source: str = "api"
) -> ConsentLedger:
    """Record consent. Idempotent — an existing grant is returned as-is."""
    existing = await _latest_grant(db, user_id)
    if existing is not None:
        return existing
    grant = ConsentLedger(
        user_id=user_id,
        purpose=PERSONALIZATION_PURPOSE,
        action="granted",
        scope={"fields": list(WRITABLE_FIELDS)},
        source=source,
    )
    db.add(grant)
    await db.flush()
    return grant


async def revoke_personalization(
    db: AsyncSession, user_id: uuid.UUID, source: str = "api"
) -> None:
    """Append a revocation AND erase what was stored.

    Recording the revocation without deleting the data would be a consent
    theatre: the ledger would say "no" while the rows said otherwise.
    """
    db.add(
        ConsentLedger(
            user_id=user_id,
            purpose=PERSONALIZATION_PURPOSE,
            action="revoked",
            scope=None,
            source=source,
        )
    )
    await forget_everything(db, user_id)
    await db.flush()


def _clean(field: str, value):
    """Normalise one field, or return None to skip it."""
    if value is None:
        return None
    if field in _LIST_FIELDS:
        if not isinstance(value, list):
            return None
        items = [
            str(v).strip()[:_MAX_ITEM_CHARS]
            for v in value
            if str(v).strip()
        ]
        return items[:_MAX_ITEMS]
    if field == "is_pregnant":
        return bool(value)
    return str(value).strip()[:64] or None


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> ProfileView:
    """Read the profile. Never raises."""
    try:
        consented = await has_personalization_consent(db, user_id)
        row = (
            await db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001 — a profile read must never cost an answer
        logger.warning("profile read failed", exc_info=True)
        return ProfileView(data={}, has_consent=False)

    if row is None:
        return ProfileView(data={}, has_consent=consented)
    return ProfileView(
        data={f: getattr(row, f) for f in WRITABLE_FIELDS},
        has_consent=consented,
    )


async def update_profile(
    db: AsyncSession, user_id: uuid.UUID, changes: dict
) -> ProfileView:
    """Write profile fields. Requires consent — raises PermissionError without.

    Fail-CLOSED deliberately: storing personal health details without a
    recorded grant is the one failure here that cannot be walked back.
    """
    grant = await _latest_grant(db, user_id)
    if grant is None:
        raise PermissionError(
            "chat_personalization consent is required before storing a profile"
        )

    row = (
        await db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
    ).scalars().first()
    if row is None:
        row = UserProfile(user_id=user_id, consent_grant_id=grant.id)
        db.add(row)

    for field in WRITABLE_FIELDS:
        if field not in changes:
            continue
        cleaned = _clean(field, changes[field])
        setattr(row, field, cleaned)

    row.consent_grant_id = grant.id
    row.updated_at = utcnow()
    await db.flush()
    return await get_profile(db, user_id)


async def forget_everything(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Erase the profile AND the long-term topic memory.

    One call, both stores. A reader asking to be forgotten does not mean
    "forget half of me", and making them find two switches would be a dark
    pattern. The consent LEDGER is append-only and is never deleted — the
    record that consent existed is itself the audit trail.
    """
    from app.models.chat import UserMemory

    deleted = {"profile": 0, "memories": 0}
    try:
        result = await db.execute(
            delete(UserProfile).where(UserProfile.user_id == user_id)
        )
        deleted["profile"] = getattr(result, "rowcount", 0) or 0
        result = await db.execute(
            delete(UserMemory).where(UserMemory.user_id == user_id)
        )
        deleted["memories"] = getattr(result, "rowcount", 0) or 0
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.warning("erase failed", exc_info=True)
        raise
    return deleted


def render_for_prompt(view: ProfileView) -> str:
    """Render the profile as a [P]-block fragment, or "" when there is nothing.

    Framed as what the reader TOLD US, not as a medical record — the same
    framing the compacted-context block uses, and for the same reason: the
    model must not present self-reported context as established fact.
    """
    if not view.has_consent or view.is_empty:
        return ""

    d = view.data
    parts: list[str] = []
    who = [d.get("age_band"), d.get("sex")]
    who = [w.replace("_", "-") for w in who if w]
    if who:
        parts.append("age/sex band: " + ", ".join(who))
    if d.get("is_pregnant"):
        parts.append("currently pregnant")
    for label, field in (
        ("ongoing conditions", "chronic_conditions"),
        ("medications", "current_medications"),
        ("allergies", "allergies"),
        ("goals", "goals"),
    ):
        values = d.get(field)
        if values:
            parts.append(f"{label}: " + ", ".join(values))
    if not parts:
        return ""
    style = d.get("communication_style")
    tone = ""
    if style == "plain":
        tone = (
            " They have asked for plain, straightforward explanations — keep "
            "it short and skip the jargon."
        )
    elif style == "detailed":
        tone = (
            " They have asked for detail — it is fine to explain the mechanism "
            "and name the specifics."
        )
    return (
        "What the reader has told us about themselves (self-reported, NOT a "
        "medical record — never present it as an established diagnosis): "
        + "; ".join(parts)
        + "."
        + tone
    )

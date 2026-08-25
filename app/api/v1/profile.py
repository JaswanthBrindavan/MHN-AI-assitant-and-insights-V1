"""Profile endpoints — see what is remembered, change it, or erase it.

This surface is not optional decoration. A store of self-reported health
details that a reader cannot inspect or delete is not something this product
should have, so the read and the erase ship in the same commit as the write.

Every endpoint is object-level authorized against the token identity, like the
rest of the API.
"""

from __future__ import annotations

import uuid
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.chat.erasure import cancel_erasure, pending_request, request_erasure
from app.chat.profile import (
    get_profile,
    grant_personalization,
    revoke_personalization,
    update_profile,
)
from app.config import get_settings
from app.db import get_db
from app.models.profile import AGE_BANDS, COMMUNICATION_STYLES

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileOut(BaseModel):
    has_consent: bool
    age_band: str | None = None
    sex: str | None = None
    communication_style: str | None = None
    preferred_language: str | None = None
    chronic_conditions: list[str] | None = None
    current_medications: list[str] | None = None
    allergies: list[str] | None = None
    goals: list[str] | None = None
    is_pregnant: bool | None = None


class ProfileUpdate(BaseModel):
    """Every field optional — a PATCH, not a replace.

    Enumerations are validated here so an invalid band is a 422 rather than a
    silently stored string nobody notices until it reaches a prompt.
    """

    age_band: str | None = Field(default=None)
    sex: str | None = None
    communication_style: str | None = None
    preferred_language: str | None = None
    chronic_conditions: list[str] | None = None
    current_medications: list[str] | None = None
    allergies: list[str] | None = None
    goals: list[str] | None = None
    is_pregnant: bool | None = None


def _validate(payload: ProfileUpdate) -> dict:
    if payload.age_band is not None and payload.age_band not in AGE_BANDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"age_band must be one of {list(AGE_BANDS)}",
        )
    if (
        payload.communication_style is not None
        and payload.communication_style not in COMMUNICATION_STYLES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"communication_style must be one of {list(COMMUNICATION_STYLES)}",
        )
    return payload.model_dump(exclude_unset=True)


def _to_out(view) -> ProfileOut:
    return ProfileOut(has_consent=view.has_consent, **view.data)


@router.get("", response_model=ProfileOut)
async def read_profile(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Everything the assistant holds about you."""
    return _to_out(await get_profile(db, current_user))


@router.post("/consent", response_model=ProfileOut)
async def give_consent(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Allow the assistant to remember personal context."""
    await grant_personalization(db, current_user, source="api_profile_consent")
    await db.commit()
    return _to_out(await get_profile(db, current_user))


@router.delete("/consent", response_model=ProfileOut)
async def withdraw_consent(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Withdraw consent AND erase what was stored.

    Recording the withdrawal while keeping the rows would be consent theatre.
    """
    await revoke_personalization(db, current_user, source="api_profile_revoke")
    await db.commit()
    return _to_out(await get_profile(db, current_user))


@router.patch("", response_model=ProfileOut)
async def patch_profile(
    payload: ProfileUpdate,
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Update stored context. 403 without consent."""
    changes = _validate(payload)
    try:
        view = await update_profile(db, current_user, changes)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    await db.commit()
    return _to_out(view)


def _iso(value) -> str | None:
    """UTC ISO-8601, always with an offset.

    SQLite has no timezone type, so a datetime written as aware comes back
    naive. Without this the SAME field serialises differently depending on
    whether the row was just created or reloaded, and a client parsing both
    gets two different instants for one moment.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class ErasureOut(BaseModel):
    """What was scheduled, and when it becomes irreversible."""

    status: str
    requested_at: str | None = None
    scheduled_for: str | None = None
    grace_days: int
    note: str


@router.delete("", status_code=status.HTTP_200_OK, response_model=ErasureOut)
@router.post("/forget-me", response_model=ErasureOut)
async def forget_me(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ErasureOut:
    """Schedule complete erasure of everything Davi holds about the caller.

    Reachable at the original ``DELETE /profile`` as well as the clearer
    ``POST /profile/forget-me``. The old route is kept because mhn-react
    already calls it -- changing an API contract is not this fix's to make,
    and the semantics are a strict improvement on the same intent.

    Two things happen, and the first is immediate:

    1. **The assistant stops using the data now.** Every per-user memory read
       and write is suppressed from this moment
       (`app/chat/memory_assembly.py`), so the next turn genuinely does not
       know the reader.
    2. **The rows are destroyed after a grace period.** The window exists so an
       accidental — or coerced — deletion can be withdrawn. It is fixed on the
       request when it is made, so changing the configured grace later cannot
       move a promise already given.

    This used to delete three of eleven per-user tables immediately. It now
    covers all of them, later.
    """
    settings = get_settings()
    record = await request_erasure(
        db, current_user, grace_days=settings.erasure_grace_days
    )
    await db.commit()
    return ErasureOut(
        status=record.status,
        requested_at=_iso(record.requested_at),
        scheduled_for=_iso(record.scheduled_for),
        grace_days=settings.erasure_grace_days,
        note=(
            "Davi has stopped using your information already. It will be "
            "permanently deleted on the date shown. You can cancel until then."
        ),
    )


@router.post("/forget-me/cancel", response_model=ErasureOut)
async def cancel_forget_me(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ErasureOut:
    """Withdraw a scheduled erasure. This is what the grace period is for."""
    settings = get_settings()
    record = await cancel_erasure(db, current_user)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No scheduled erasure to cancel",
        )
    await db.commit()
    return ErasureOut(
        status=record.status,
        requested_at=_iso(record.requested_at),
        scheduled_for=None,
        grace_days=settings.erasure_grace_days,
        note="Your information is no longer scheduled for deletion.",
    )


@router.get("/forget-me", response_model=ErasureOut)
async def erasure_status(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ErasureOut:
    """Whether an erasure is scheduled, and when it happens."""
    settings = get_settings()
    record = await pending_request(db, current_user)
    if record is None:
        return ErasureOut(
            status="none",
            grace_days=settings.erasure_grace_days,
            note="No erasure is scheduled.",
        )
    return ErasureOut(
        status=record.status,
        requested_at=_iso(record.requested_at),
        scheduled_for=_iso(record.scheduled_for),
        grace_days=settings.erasure_grace_days,
        note="Davi has already stopped using your information.",
    )



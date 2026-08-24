"""Profile endpoints — see what is remembered, change it, or erase it.

This surface is not optional decoration. A store of self-reported health
details that a reader cannot inspect or delete is not something this product
should have, so the read and the erase ship in the same commit as the write.

Every endpoint is object-level authorized against the token identity, like the
rest of the API.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.chat.profile import (
    forget_everything,
    get_profile,
    grant_personalization,
    revoke_personalization,
    update_profile,
)
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


@router.delete("", status_code=status.HTTP_200_OK)
async def forget_me(
    current_user: uuid.UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Erase the profile and the long-term topic memory.

    Consent stays granted — this is "forget what you know", not "stop
    remembering". Use DELETE /profile/consent for the latter.
    """
    deleted = await forget_everything(db, current_user)
    await db.commit()
    return {"deleted": deleted}

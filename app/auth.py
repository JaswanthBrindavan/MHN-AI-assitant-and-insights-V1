"""JWT auth (HS256) and object-level authorization helpers.

Auth is gated by ``AUTH_ENABLED``. When disabled (dev/tests) the caller's
identity is taken from an ``X-User-Id`` header, falling back to a fixed dev
user. When enabled, a Bearer JWT is required and ``sub`` carries the user UUID.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Header, HTTPException, status
from jose import JWTError, jwt

from app.config import get_settings

# Stable dev identity used when AUTH_ENABLED=false and no header is supplied.
DEV_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    return authorization.split(" ", 1)[1].strip()


async def get_current_user_id(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> UUID:
    """Resolve the authenticated user id from the request.

    Raises 401 when auth is enabled and the token is missing/invalid.
    """
    settings = get_settings()

    if not settings.auth_enabled:
        if x_user_id:
            try:
                return UUID(x_user_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid X-User-Id",
                ) from exc
        return DEV_USER_ID

    token = _parse_bearer(authorization)
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )
    try:
        return UUID(str(sub))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is not a valid user id",
        ) from exc


def authorize_user(requested_id: UUID, current_id: UUID) -> None:
    """Object-level authorization: 403 when acting on another user's data."""
    if requested_id != current_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this user",
        )

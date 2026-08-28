"""JWT auth and object-level authorization helpers.

Aligned with the mhn-spring production backend: session tokens are HS512 JWTs
whose ``sub`` claim carries the user UUID, signed with the shared JWT_SECRET —
which Spring Base64-DECODES before use as the HMAC key (JwtService.java:
``Decoders.BASE64.decode(jwtSecret)`` → ``hmacShaKeyFor``). We mirror that via
``jwt_secret_base64`` (default on; falls back to the raw string when the value
isn't valid Base64).

Two accepted credentials when ``AUTH_ENABLED``:
  1. A user session JWT (Bearer) — validated here, identity from ``sub``.
  2. A static service token (Bearer) + ``X-User-Id`` — the Spring↔mhn-ai
     pattern (AI_TOKEN / MHN_SERVICE_TOKEN): the caller is a trusted backend
     that already authenticated the user. Constant-time compared; disabled
     unless SERVICE_TOKEN is configured (≥32 chars).

When ``AUTH_ENABLED=false`` (dev/tests) identity comes from ``X-User-Id``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from contextvars import ContextVar
from uuid import UUID

from fastapi import Header, HTTPException, status
from jose import JWTError, jwt

from app.config import get_settings

# Stable dev identity used when AUTH_ENABLED=false and no header is supplied.
logger = logging.getLogger("davi.auth")

DEV_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

# The raw end-user JWT for the current request, captured at the API layer so a
# server-to-server call to mhn-spring can act AS the reader. Spring has no
# service-token path — its only auth filter parses a user JWT and loads THAT
# user (JwtAuthenticationFilter) — so a per-user write must present the
# reader's own token. None outside a request, or when the caller reached Davi
# via the service-token path (there is no user JWT to forward).
_current_user_jwt: ContextVar[str | None] = ContextVar(
    "current_user_jwt", default=None
)


def set_current_user_jwt(authorization: str | None) -> None:
    """Stash the request's bearer token (JWT only, minus the scheme)."""
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
        # Do not forward Davi's OWN service token (service-to-server path):
        # it is not a user JWT and Spring would reject it anyway.
        settings = get_settings()
        if candidate and candidate != settings.service_token:
            token = candidate
    _current_user_jwt.set(token)


def current_user_jwt() -> str | None:
    """The reader's JWT for outbound Spring calls, or None when unavailable."""
    return _current_user_jwt.get()


def _hmac_key(settings) -> str | bytes:
    """The HMAC key bytes, Base64-decoding the secret the way Spring does.

    Falls back to the raw string when decoding fails or is disabled, so dev
    setups with a plain-text secret keep working.
    """
    if settings.jwt_secret_base64:
        try:
            return base64.b64decode(settings.jwt_secret, validate=True)
        except (binascii.Error, ValueError):
            # Spring ALWAYS Base64-decodes its secret. Falling back silently
            # meant a non-Base64 value verified nothing Spring signed — every
            # request 401'd with no clue why (audit medium).
            logger.warning(
                "JWT_SECRET is not valid Base64 but JWT_SECRET_BASE64 is on; "
                "using the raw string — Spring-signed tokens will NOT verify"
            )
            return settings.jwt_secret
    return settings.jwt_secret


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

    # Service-token path (server-to-server, e.g. the React BFF or Spring):
    # constant-time compare; the caller must say WHO the user is.
    if (
        settings.service_token
        and len(settings.service_token) >= 32
        and hmac.compare_digest(token, settings.service_token)
    ):
        if not x_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-User-Id required with a service token",
            )
        try:
            return UUID(x_user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-User-Id",
            ) from exc

    try:
        payload = jwt.decode(
            token,
            _hmac_key(settings),
            algorithms=[settings.jwt_algorithm],
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

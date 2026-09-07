"""Plan entitlements — asked of mhn-spring, never inferred here.

``GET /entitlements/{userId}`` answers ``{premium, ai_context,
family_context}``. Ink does not own subscriptions and must not reconstruct
them from tables it happens to be able to read: mhn-spring is the authority
on who is on Care+ and what that buys.

``family_context`` is the one this module exists for. mhn-spring PR #78 made
it mean *"on Care+ AND at least one accepted connection whose other party has
granted this person AI context"* — the INBOUND direction. Ink still checks
the per-member consent itself, from the shared database, because consent is
per relationship and Spring's flag is a single boolean about the plan.

**Fail CLOSED, unlike every other Spring client here.** ``fetch_adherence``
and the document fetch fail *open* because they are enrichment: no answer
just means a thinner reply. This one guards a consent gate, so unconfigured,
unreachable, slow, non-200 or an unparseable body all read as "not
entitled" — the capability simply does not engage, and since it is purely
additive that costs an existing answer nothing.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from app.auth import current_user_jwt
from app.config import get_settings

logger = logging.getLogger("davi.entitlements")

_PATH = "/entitlements/{user_id}"


def _base() -> str | None:
    """Same shape as the other Spring clients (adherence, medicines, files):
    empty configuration means the call cannot be made, and a missing scheme
    defaults to http:// because Railway private networking is http."""
    raw = (get_settings().mhn_spring_base_url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


async def family_context_entitled(
    user_id: uuid.UUID, *, client: httpx.AsyncClient | None = None
) -> bool:
    """Does this reader's plan allow family context? False whenever unsure.

    Never raises: the caller runs inside a chat turn, and an entitlements
    lookup must not be able to break a reply.
    """
    base = _base()
    if not base:
        return False
    # Spring's only auth filter parses a USER JWT (see app/auth.py) — the
    # service token is rejected. No token captured ⇒ no call to make.
    jwt = current_user_jwt()
    if not jwt:
        return False

    url = base + _PATH.format(user_id=user_id)
    headers = {
        "Authorization": f"Bearer {jwt}",
        # Parity with the other Spring clients; the JWT is what authenticates.
        "X-User-Id": str(user_id),
    }
    timeout = get_settings().mhn_spring_timeout_seconds
    try:
        if client is not None:
            resp = await client.get(url, headers=headers, timeout=timeout)
        else:
            async with httpx.AsyncClient() as owned:
                resp = await owned.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            logger.info("entitlements unavailable (status %s)", resp.status_code)
            return False
        payload = resp.json()
        # Spring serialises snake_case here; accept camelCase too rather than
        # let a Jackson naming strategy silently switch the gate off (the same
        # compatibility the medicines client keeps for id/trackingId).
        return payload.get("family_context", payload.get("familyContext")) is True
    except Exception:  # noqa: BLE001 — a consent gate must fail closed, not raise
        logger.warning("entitlements lookup failed; family context off", exc_info=True)
        return False

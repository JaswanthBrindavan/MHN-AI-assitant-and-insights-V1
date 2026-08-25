"""Medication adherence — asked of mhn-spring, never recomputed.

`medicine_dose_log` is right there and the arithmetic is four lines, which is
exactly why this is worth writing down: **Davi must not compute this number.**

mhn-spring already serves it, at `GET /courses/{trackingId}/adherence`
(`MedicineController.java:149`), and its semantics are not the obvious ones
(`MedicineTrackingServiceImpl.java:383-419`):

* a **30-day** default window, not 7 or 14 (`DEFAULT_ADHERENCE_DAYS = 30`);
* `from = today.minusDays(window - 1)` — **inclusive of today**;
* "today" is `LocalDate.now(userZone.of(user))` — **the reader's timezone**,
  not the server's;
* as-needed (PRN) doses excluded outright, because there is no schedule to
  have adhered to;
* pending doses excluded from the denominator, because counting not-yet-due
  doses as missed shows a figure that climbs through the day.

Get any one of those wrong and the reader sees two different numbers for the
same medication on the same phone. A reader in Asia/Kolkata who took their
09:00 dose and asks at 10:00 would see the app say 92.3% and Davi say 64.3% —
a 14-day UTC window excludes this morning's dose entirely and, at 04:30 UTC,
is still on yesterday.

So Davi asks. This costs a network call on a question that is asked rarely, and
buys agreement with the product the reader is looking at.

Fail-open: no configuration, no answer, a timeout — all return None, and the
caller says it could not look it up rather than guessing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.telemetry import record_fail_open

logger = logging.getLogger("davi.medicines")

# @RequestMapping("/medicine") + @GetMapping("/courses/{trackingId}/adherence").
# Built off the configured base the same way app/documents/fetch.py does.
_ADHERENCE_PATH = "/medicine/courses/{tracking_id}/adherence"


@dataclass(frozen=True)
class Adherence:
    """What mhn-spring reports. Field names mirror its AdherenceResponse."""

    tracking_id: int
    from_date: str
    to_date: str
    total: int
    taken: int
    skipped: int
    forgotten: int
    pending: int
    percentage: float | None

    @property
    def due(self) -> int:
        """Doses that were actually due — the denominator Spring uses."""
        return self.taken + self.skipped + self.forgotten


def _base() -> str | None:
    raw = (get_settings().mhn_spring_base_url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


async def fetch_adherence(
    user_id: uuid.UUID,
    tracking_id: int,
    *,
    days: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> Adherence | None:
    """Ask mhn-spring. None when it cannot be asked or does not answer.

    ``days`` is passed through only when given: omitting it lets Spring apply
    its own default, which is the point — Davi should not carry a copy of a
    window length that belongs to the other service.
    """
    base = _base()
    settings = get_settings()
    if not base or not settings.mhn_spring_token:
        return None

    url = base + _ADHERENCE_PATH.format(tracking_id=tracking_id)
    headers = {
        "Authorization": f"Bearer {settings.mhn_spring_token}",
        # Spring authorizes for THIS user and resolves their timezone from it.
        "X-User-Id": str(user_id),
    }
    params = {"days": days} if days else None

    try:
        if client is not None:
            resp = await client.get(
                url, headers=headers, params=params,
                timeout=settings.mhn_spring_timeout_seconds,
            )
        else:
            async with httpx.AsyncClient() as owned:
                resp = await owned.get(
                    url, headers=headers, params=params,
                    timeout=settings.mhn_spring_timeout_seconds,
                )
        if resp.status_code != 200:
            # 403/404 is Spring's authorization answering, and it is the
            # authority here. Not an error to alert on.
            logger.info("adherence unavailable (status %s)", resp.status_code)
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001 — a lookup must never break a reply
        logger.warning("adherence lookup failed", exc_info=True)
        record_fail_open("adherence")
        return None

    try:
        percentage = payload.get("adherencePct")
        return Adherence(
            tracking_id=int(payload["trackingId"]),
            from_date=str(payload.get("from") or ""),
            to_date=str(payload.get("to") or ""),
            total=int(payload.get("total") or 0),
            taken=int(payload.get("taken") or 0),
            skipped=int(payload.get("skipped") or 0),
            forgotten=int(payload.get("forgotten") or 0),
            pending=int(payload.get("pending") or 0),
            percentage=float(percentage) if percentage is not None else None,
        )
    except Exception:  # noqa: BLE001 — a shape change must not crash a turn
        logger.warning("adherence response shape unexpected", exc_info=True)
        record_fail_open("adherence_shape")
        return None


def render_adherence(name: str, adherence: Adherence) -> str:
    """A deterministic, validator-safe sentence.

    Renders the PERCENTAGE, not "12 of 14 doses". The numeric-fidelity guard
    (`app/grounding/fidelity.py`) only recognises number+unit shapes, so a bare
    count is invisible to it — and a number the guard cannot see is a number
    nothing checks.
    """
    if adherence.percentage is None or adherence.due == 0:
        return (
            f"There are no scheduled doses on record for {name} in that "
            "period, so there is nothing to measure yet."
        )
    return (
        f"Over {adherence.from_date} to {adherence.to_date} you took "
        f"{adherence.percentage:.1f}% of the scheduled doses of {name}. "
        "As-needed doses are not counted. If keeping up is difficult, that is "
        "worth raising with your prescriber — do not change a dose on your own."
    )

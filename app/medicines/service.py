"""Medication write client — the CRUD is mhn-spring's, never Davi's.

Adding, stopping, or removing a medication is a domain action (it drives dose
schedules, reminders, adherence), so Davi does not touch the rows: it calls
mhn-spring's MedicineController the same way the app does, and lets Spring's
own logic run.

    POST   /medicine/courses                  add a course      (AddCourseRequest)
    GET    /medicine/courses?activeOnly=true   list, to resolve a name → trackingId
    POST   /medicine/courses/{id}/stop         mark stopped/completed
    DELETE /medicine/courses/{id}              remove

Auth: the END-USER's JWT is forwarded (``app.auth.current_user_jwt``). Spring
has no service-token path — its only filter parses a user JWT and loads THAT
user — so acting on the reader's own medications means presenting the reader's
own token. Unset token / unconfigured base ⇒ the call cannot be made.

A WRITE never fails *open* the way a read does: if Spring cannot be reached or
refuses, the handler tells the reader it could not be saved. It must never
confirm a save that did not happen.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import httpx

from app.auth import current_user_jwt
from app.config import get_settings

logger = logging.getLogger("davi.medicines")

_COURSES = "/medicine/courses"


@dataclass(frozen=True)
class Course:
    tracking_id: int
    name: str
    is_prn: bool = False
    active: bool = True


@dataclass(frozen=True)
class MedResult:
    """Outcome of a write (or a resolve). ``ok`` is True only on a real 2xx.

    ``reason`` is a short, non-leaking label for the caller to branch on
    ("not_configured", "no_token", "not_found", "http_401", "connect_error").
    """

    ok: bool
    reason: str | None = None
    course: Course | None = None
    courses: tuple[Course, ...] = field(default_factory=tuple)


def _base() -> str | None:
    raw = (get_settings().mhn_spring_base_url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


def _headers(user_id: uuid.UUID) -> dict[str, str] | None:
    """Forward the reader's own JWT. None ⇒ nothing to present, so no call."""
    jwt = current_user_jwt()
    if not jwt:
        return None
    return {
        "Authorization": f"Bearer {jwt}",
        # Kept for parity with the other Spring clients / any future service
        # path; Spring's current filter authenticates from the JWT alone.
        "X-User-Id": str(user_id),
    }


def _course(payload: dict) -> Course | None:
    tid = payload.get("trackingId")
    if not isinstance(tid, int):
        return None
    return Course(
        tracking_id=tid,
        name=str(payload.get("name") or ""),
        is_prn=bool(payload.get("isPrn") or payload.get("prn") or False),
        active=payload.get("stoppedAt") in (None, "") if "stoppedAt" in payload
        else True,
    )


async def _request(
    method: str,
    path: str,
    user_id: uuid.UUID,
    *,
    json: dict | None = None,
    params: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, object] | None:
    """One authenticated call. None ⇒ could not even be attempted/reached."""
    base = _base()
    if base is None:
        return None
    headers = _headers(user_id)
    if headers is None:
        return None
    settings = get_settings()
    url = base + path
    try:
        if client is not None:
            resp = await client.request(
                method, url, headers=headers, json=json, params=params,
                timeout=settings.mhn_spring_timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(
                timeout=settings.mhn_spring_timeout_seconds
            ) as owned:
                resp = await owned.request(
                    method, url, headers=headers, json=json, params=params,
                )
        body: object = None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — DELETE may return no/empty body
            body = None
        return resp.status_code, body
    except Exception:  # noqa: BLE001 — a write must never break a reply
        logger.warning("spring medicine %s %s failed", method, path, exc_info=True)
        return None


async def list_courses(
    user_id: uuid.UUID, *, active_only: bool = True,
    client: httpx.AsyncClient | None = None,
) -> MedResult:
    got = await _request(
        "GET", _COURSES, user_id,
        params={"activeOnly": "true"} if active_only else None, client=client,
    )
    if got is None:
        base = _base()
        return MedResult(ok=False, reason="not_configured" if base is None
                         else "no_token")
    status, body = got
    if status != 200 or not isinstance(body, list):
        return MedResult(ok=False, reason=f"http_{status}")
    courses = tuple(c for c in (_course(x) for x in body if isinstance(x, dict))
                    if c is not None)
    return MedResult(ok=True, courses=courses)


async def add_course(
    user_id: uuid.UUID, name: str, *, strength: str | None = None,
    is_prn: bool = False, schedule_pattern: str | None = None,
    day_pattern: str = "daily", client: httpx.AsyncClient | None = None,
) -> MedResult:
    """Create a course. Spring needs EITHER isPrn OR a schedulePattern+dayPattern:
    its non-PRN path calls MedicineSchedule.parsePattern(schedulePattern) and
    throws on a null, so a schedule-less, non-PRN payload 500s. When neither a
    schedule nor as-needed is given we fall back to as-needed, so a write is
    always valid — the reader can set a schedule in the app."""
    payload: dict = {"name": name[:255]}
    if strength:
        payload["strength"] = strength[:100]
    if not is_prn and schedule_pattern:
        payload["schedulePattern"] = schedule_pattern[:4]
        payload["dayPattern"] = day_pattern
    else:
        payload["isPrn"] = True
    got = await _request("POST", _COURSES, user_id, json=payload, client=client)
    if got is None:
        base = _base()
        return MedResult(ok=False, reason="not_configured" if base is None
                         else "no_token")
    status, body = got
    if status not in (200, 201) or not isinstance(body, dict):
        return MedResult(ok=False, reason=f"http_{status}")
    return MedResult(ok=True, course=_course(body))


async def _resolve(
    user_id: uuid.UUID, name: str, client: httpx.AsyncClient | None = None,
    *, active_only: bool = True,
) -> MedResult:
    """Find the course whose name matches ``name``. Reused by stop/remove.

    ``active_only`` is True for STOP (you can only stop a running course) and
    False for REMOVE (a course you already stopped must still be removable —
    resolving remove against active-only was why 'remove X' after 'stop X'
    reported nothing to remove)."""
    listed = await list_courses(user_id, active_only=active_only, client=client)
    if not listed.ok:
        return listed
    want = name.strip().lower()
    matches = [c for c in listed.courses if want in c.name.lower()
               or c.name.lower() in want]
    if not matches:
        return MedResult(ok=False, reason="not_found")
    if len(matches) > 1:
        # Prefer an exact name hit; otherwise it is genuinely ambiguous.
        exact = [c for c in matches if c.name.lower() == want]
        if len(exact) != 1:
            return MedResult(ok=False, reason="ambiguous", courses=tuple(matches))
        matches = exact
    return MedResult(ok=True, course=matches[0])


async def stop_course(
    user_id: uuid.UUID, name: str, client: httpx.AsyncClient | None = None
) -> MedResult:
    resolved = await _resolve(user_id, name, client)
    if not resolved.ok or resolved.course is None:
        return resolved
    tid = resolved.course.tracking_id
    got = await _request("POST", f"{_COURSES}/{tid}/stop", user_id, client=client)
    if got is None:
        return MedResult(ok=False, reason="no_token")
    status, _ = got
    if status not in (200, 204):
        return MedResult(ok=False, reason=f"http_{status}")
    return MedResult(ok=True, course=resolved.course)


async def delete_course(
    user_id: uuid.UUID, name: str, client: httpx.AsyncClient | None = None
) -> MedResult:
    # active_only=False: a stopped course is still removable.
    resolved = await _resolve(user_id, name, client, active_only=False)
    if not resolved.ok or resolved.course is None:
        return resolved
    tid = resolved.course.tracking_id
    got = await _request("DELETE", f"{_COURSES}/{tid}", user_id, client=client)
    if got is None:
        return MedResult(ok=False, reason="no_token")
    status, _ = got
    if status not in (200, 204):
        return MedResult(ok=False, reason=f"http_{status}")
    return MedResult(ok=True, course=resolved.course)

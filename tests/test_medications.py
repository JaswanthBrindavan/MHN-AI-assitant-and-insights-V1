"""Medication CRUD via chat: parsing, the mhn-spring write client (forwarded
JWT), and the handler's honest confirm/decline behavior.

The write is Spring's — Davi calls MedicineController as the reader. A write
that does not land must NEVER read back as a success.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.auth import set_current_user_jwt
from app.chat.abilities import parse_medication_command
from app.chat.data_handlers import handle_medication_command
from app.medicines import service as med

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _spring_configured(monkeypatch):
    """Point the client at a base and give the request a forwarded JWT."""
    monkeypatch.setenv("MHN_SPRING_BASE_URL", "http://spring.internal:8080")
    from app.config import get_settings

    get_settings.cache_clear()
    set_current_user_jwt("Bearer user-jwt-123")
    yield
    set_current_user_jwt(None)
    get_settings.cache_clear()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("msg", "action", "name", "strength"),
    [
        ("add metformin 500mg tablet twice daily", "add", "metformin", "500 mg"),
        ("start me on amlodipine 5 mg", "add", "amlodipine", "5 mg"),
        ("I stopped my amoxicillin tablets", "stop", "amoxicillin", None),
        ("completed my augmentin course", "stop", "augmentin", None),
        ("remove atorvastatin from my meds", "remove", "atorvastatin", None),
        ("delete the metformin pill", "remove", "metformin", None),
    ],
)
def test_parse(msg, action, name, strength):
    cmd = parse_medication_command(msg)
    assert cmd is not None
    assert (cmd.action, cmd.name, cmd.strength) == (action, name, strength)


@pytest.mark.parametrize(
    "msg",
    [
        "tell me about metformin",
        "what is metformin used for",
        "stop worrying so much",
        "add 2 glasses of water",
        "I take metformin every day",  # a statement, not a command
    ],
)
def test_parse_rejects_non_commands(msg):
    assert parse_medication_command(msg) is None


def test_parse_prn():
    cmd = parse_medication_command("add paracetamol tablet as needed")
    assert cmd is not None and cmd.is_prn is True


# --------------------------------------------------------------------------- #
# Spring client — forwarded JWT, and writes never fail-open to success
# --------------------------------------------------------------------------- #
async def test_add_course_forwards_user_jwt():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(201, json={"trackingId": 7, "name": "Metformin"})

    async with _client(handler) as c:
        res = await med.add_course(USER, "metformin", strength="500 mg", client=c)
    assert res.ok and res.course is not None and res.course.tracking_id == 7
    assert seen["auth"] == "Bearer user-jwt-123"  # the READER's token, forwarded
    assert seen["path"] == "/medicine/courses"
    import json
    body = json.loads(seen["body"])
    # No schedule and not as-needed given -> defaults to as-needed so Spring's
    # non-PRN path (which needs a schedulePattern) never 500s.
    assert body == {"name": "metformin", "strength": "500 mg", "isPrn": True}


async def test_add_course_scheduled_sends_pattern_not_prn():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(201, json={"trackingId": 8, "name": "Metformin"})

    async with _client(handler) as c:
        res = await med.add_course(
            USER, "metformin", strength="500 mg",
            schedule_pattern="ME", client=c,
        )
    assert res.ok
    import json
    body = json.loads(seen["body"])
    assert body == {
        "name": "metformin", "strength": "500 mg",
        "schedulePattern": "ME", "dayPattern": "daily",
    }
    assert "isPrn" not in body  # a scheduled course is not as-needed


async def test_add_course_as_needed_sends_prn():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(201, json={"trackingId": 9, "name": "Paracetamol"})

    async with _client(handler) as c:
        await med.add_course(USER, "paracetamol", is_prn=True, client=c)
    import json
    body = json.loads(seen["body"])
    assert body == {"name": "paracetamol", "isPrn": True}


async def test_no_token_means_no_write():
    set_current_user_jwt(None)  # no forwarded JWT this request
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"trackingId": 1})

    async with _client(handler) as c:
        res = await med.add_course(USER, "metformin", client=c)
    assert not res.ok and res.reason == "no_token"
    assert called is False  # never even attempted without the reader's identity


async def test_stop_resolves_name_then_stops():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"trackingId": 3, "name": "Amoxicillin 500mg"},
                {"trackingId": 9, "name": "Metformin"},
            ])
        assert request.url.path == "/medicine/courses/3/stop"
        return httpx.Response(200, json={"trackingId": 3, "name": "Amoxicillin 500mg"})

    async with _client(handler) as c:
        res = await med.stop_course(USER, "amoxicillin", client=c)
    assert res.ok and res.course is not None and res.course.tracking_id == 3


async def test_stop_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"trackingId": 9, "name": "Metformin"}])

    async with _client(handler) as c:
        res = await med.delete_course(USER, "amoxicillin", client=c)
    assert not res.ok and res.reason == "not_found"


async def test_http_error_is_not_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _client(handler) as c:
        res = await med.add_course(USER, "metformin", client=c)
    assert not res.ok and res.reason == "http_500"


# --------------------------------------------------------------------------- #
# Handler — the reply matches reality
# --------------------------------------------------------------------------- #
async def test_handler_confirms_add(db_session, monkeypatch):
    async def _ok(user_id, name, **kw):
        return med.MedResult(ok=True, course=med.Course(tracking_id=5, name="Metformin"))

    monkeypatch.setattr(med, "add_course", _ok)
    r = await handle_medication_command(db_session, USER, "add metformin 500mg tablet")
    assert r is not None
    assert r["action"] == "medication_updated"
    assert "Added" in r["reply"] and "Metformin" in r["reply"]
    assert r["provenance"]["ok"] is True


async def test_handler_declines_when_unavailable_never_false_success(
    db_session, monkeypatch
):
    async def _down(user_id, name, **kw):
        return med.MedResult(ok=False, reason="not_configured")

    monkeypatch.setattr(med, "add_course", _down)
    r = await handle_medication_command(db_session, USER, "add metformin tablet")
    assert r is not None
    assert r["action"] == "none"
    assert r["provenance"]["ok"] is False
    # The reply must NOT claim it was added.
    assert "Added" not in r["reply"]
    assert "can't update your medications" in r["reply"].lower()


async def test_handler_not_found_is_honest(db_session, monkeypatch):
    async def _missing(user_id, name, **kw):
        return med.MedResult(ok=False, reason="not_found")

    monkeypatch.setattr(med, "stop_course", _missing)
    r = await handle_medication_command(db_session, USER, "stopped my amoxicillin tablets")
    assert r is not None
    assert "couldn't find" in r["reply"].lower()
    assert "marked" not in r["reply"].lower()


async def test_handler_none_for_non_command(db_session):
    assert await handle_medication_command(db_session, USER, "tell me about metformin") is None


# --------------------------------------------------------------------------- #
# Agentic tool — structured frequency maps to a valid schedule, and confirms it
# --------------------------------------------------------------------------- #
def _capture_add(seen: dict):
    async def _add(user_id, name, **kw):
        seen.update(kw)
        seen["name"] = name
        return med.MedResult(ok=True, course=med.Course(tracking_id=1, name=name))
    return _add


async def test_tool_add_maps_times_per_day_to_slots(db_session, monkeypatch):
    from app.chat.tools import executors

    seen: dict = {}
    monkeypatch.setattr(med, "add_course", _capture_add(seen))
    out = await executors.add_medication(
        db_session, USER,
        {"name": "metformin", "strength": "500 mg", "times_per_day": 2}, None,
    )
    assert out is not None
    assert seen["schedule_pattern"] == "ME"  # twice a day -> morning + evening
    assert seen["is_prn"] is False
    assert "twice daily (morning and evening)" in out["deterministic_reply"]


async def test_tool_add_as_needed_maps_to_prn(db_session, monkeypatch):
    from app.chat.tools import executors

    seen: dict = {}
    monkeypatch.setattr(med, "add_course", _capture_add(seen))
    out = await executors.add_medication(
        db_session, USER, {"name": "paracetamol", "as_needed": True}, None
    )
    assert out is not None
    assert seen["is_prn"] is True and seen["schedule_pattern"] is None
    assert "as needed" in out["deterministic_reply"]


async def test_tool_add_unknown_frequency_defaults_to_prn(db_session, monkeypatch):
    """A safety net: no frequency given still yields a VALID course, never a 500."""
    from app.chat.tools import executors

    seen: dict = {}
    monkeypatch.setattr(med, "add_course", _capture_add(seen))
    await executors.add_medication(db_session, USER, {"name": "metformin"}, None)
    assert seen["is_prn"] is True and seen["schedule_pattern"] is None


async def test_courses_parse_springs_real_field_names():
    """Spring's CourseResponse serialises `id`, not `trackingId` (only the
    URL path variable carries that name). Requiring trackingId made every
    listed course parse to None — the list was always empty, so every
    stop/remove/adherence said "couldn't find" for medications visibly on
    the reader's list (live bug, caught by the user's app screenshot)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[
                # the REAL Spring shape
                {"id": 11, "name": "insulin", "isPrivate": False,
                 "schedulePattern": "N", "stoppedAt": None},
                {"id": 12, "name": "Dolo 650", "stoppedAt": None},
            ])
        assert request.url.path == "/medicine/courses/11/stop"
        return httpx.Response(200, json={"id": 11, "name": "insulin"})

    async with _client(handler) as c:
        res = await med.stop_course(USER, "insulin", client=c)
    assert res.ok and res.course is not None
    assert res.course.tracking_id == 11

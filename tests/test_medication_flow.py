"""Deterministic medication flow: parsing, the multi-turn state machine, the
LLM capture fallback, and the two write-resolution bug fixes.

The transaction is deterministic so it completes every time; the model is used
only to READ messy phrasing into fields, never to decide the flow.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat import medication_flow as mf
from app.medicines import service as med

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("msg", "action", "name"),
    [
        ("get me started on dolo 650", "add", "dolo 650"),
        ("add metformin 500mg", "add", "metformin 500 mg"),
        ("add my amlodipine tablet", "add", "amlodipine"),
        ("i stopped my amoxicillin", "stop", "amoxicillin"),
        ("remove atorvastatin from my meds", "remove", "atorvastatin"),
        ("list my active medications", "list", ""),
        ("what medications am i on", "list", ""),
    ],
)
def test_detect_intent(msg, action, name):
    intent = mf.detect_intent(msg)
    assert intent is not None
    assert intent["action"] == action
    if name:
        assert name in intent["name"].lower()


@pytest.mark.parametrize(
    "msg",
    [
        "should i stop my metformin",       # a question, not a command
        "can i take dolo 650",              # a question
        "what is metformin used for",       # a question
        "how are you",                      # unrelated
    ],
)
def test_detect_intent_ignores_questions(msg):
    assert mf.detect_intent(msg) is None


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("as needed", (None, True)),
        ("only when i have pain", (None, True)),
        ("twice a day", ("ME", False)),
        ("3 times a day", ("MAE", False)),
        ("morning, afternoon and evening", ("MAE", False)),
        ("just at night", ("N", False)),
        ("once daily", ("M", False)),
    ],
)
def test_parse_schedule(msg, expected):
    assert mf.parse_schedule(msg) == expected


def test_parse_schedule_unreadable():
    assert mf.parse_schedule("i'm not really sure honestly") is None


@pytest.mark.parametrize(
    ("msg", "expected"),
    [("yes", True), ("yeah go ahead", True), ("that's right", True),
     ("no", False), ("cancel", False), ("nope", False),
     ("maybe later idk", None)],
)
def test_parse_yes_no(msg, expected):
    assert mf.parse_yes_no(msg) is expected


# --------------------------------------------------------------------------- #
# The state machine — add: intent -> ask schedule -> confirm -> write
# --------------------------------------------------------------------------- #
async def test_add_asks_for_schedule_when_missing(db_session):
    r = await mf.handle_medication_turn(db_session, USER, "add dolo 650", None)
    assert r is not None
    assert "how often" in r["reply"].lower()
    pm = r["pending_med"]
    assert pm["stage"] == "await_schedule" and pm["action"] == "add"
    assert "dolo" in pm["name"].lower()


async def test_add_with_inline_schedule_goes_straight_to_confirm(db_session):
    r = await mf.handle_medication_turn(
        db_session, USER, "add dolo 650 three times a day", None)
    pm = r["pending_med"]
    assert pm["stage"] == "confirm" and pm["schedule_pattern"] == "MAE"
    assert "confirm" in r["reply"].lower()


async def test_schedule_answer_advances_to_confirm(db_session):
    pending = {"stage": "await_schedule", "action": "add", "name": "dolo 650",
               "strength": "650"}
    r = await mf.handle_medication_turn(
        db_session, USER, "morning, afternoon and evening", pending)
    pm = r["pending_med"]
    assert pm["stage"] == "confirm" and pm["schedule_pattern"] == "MAE"


async def test_confirm_yes_performs_the_write(db_session, monkeypatch):
    seen = {}

    async def _write(db, user_id, action, name, **kw):
        seen.update({"action": action, "name": name, **kw})
        return {"reply": f"Added {name}.", "action": "medication_updated",
                "provenance": {"ok": True}}

    monkeypatch.setattr(mf, "perform_medication_write", _write, raising=False)
    # perform_medication_write is imported lazily inside the handler, so patch
    # it on its home module too.
    import app.chat.data_handlers as dh
    monkeypatch.setattr(dh, "perform_medication_write", _write)

    pending = {"stage": "confirm", "action": "add", "name": "dolo 650",
               "strength": "650", "schedule_pattern": "MAE", "is_prn": False}
    r = await mf.handle_medication_turn(db_session, USER, "yes", pending)
    assert r["pending_med"] is None
    assert seen["action"] == "add" and seen["schedule_pattern"] == "MAE"


async def test_confirm_no_cancels_without_writing(db_session, monkeypatch):
    called = False

    async def _write(*a, **k):
        nonlocal called
        called = True
        return {"reply": "x", "action": "none", "provenance": {}}

    import app.chat.data_handlers as dh
    monkeypatch.setattr(dh, "perform_medication_write", _write)
    pending = {"stage": "confirm", "action": "add", "name": "dolo 650"}
    r = await mf.handle_medication_turn(db_session, USER, "no thanks", pending)
    assert called is False and r["pending_med"] is None
    assert "won't" in r["reply"].lower()


async def test_await_schedule_releases_on_a_fresh_unrelated_command(db_session):
    """A user who abandons the flow is not trapped — control returns to the
    normal pipeline (None) rather than re-asking forever."""
    pending = {"stage": "await_schedule", "action": "add", "name": "dolo 650",
               "reasked": True}
    r = await mf.handle_medication_turn(
        db_session, USER, "actually what is my blood pressure", pending)
    assert r is None


# --------------------------------------------------------------------------- #
# LLM capture fallback — long-tail phrasing the regex misses
# --------------------------------------------------------------------------- #
async def test_llm_capture_catches_phrasing_the_parser_misses(db_session):
    class _Extractor:
        model_name = "fake"

        async def generate(self, system, user):
            return ('{"is_command": true, "action": "add", "name": "dolo 650", '
                    '"strength": "650", "times_per_day": 3, "as_needed": false}')

    # A phrasing detect_intent does not confidently parse, but which has a med
    # signal -> the LLM extractor is consulted and drives the SAME flow.
    msg = "put dolo 650 tablet onto my medication list please"
    r = await mf.handle_medication_turn(db_session, USER, msg, None, _Extractor())
    assert r is not None
    pm = r["pending_med"]
    assert pm["stage"] == "confirm" and pm["schedule_pattern"] == "MAE"


async def test_bare_add_of_a_drug_with_no_dose_uses_the_llm(db_session):
    """"start me on amlodipine" has no dose signal, so the deterministic parser
    stays out of it (it can't tell a drug from a foodstuff) — the LLM does."""
    assert mf.detect_intent("start me on amlodipine") is None  # no hard signal

    class _Extractor:
        model_name = "fake"

        async def generate(self, system, user):
            return ('{"is_command": true, "action": "add", '
                    '"name": "amlodipine", "strength": null, '
                    '"times_per_day": null, "as_needed": false}')

    r = await mf.handle_medication_turn(
        db_session, USER, "start me on amlodipine", None, _Extractor())
    assert r is not None
    pm = r["pending_med"]
    assert pm["stage"] == "await_schedule" and "amlodipine" in pm["name"].lower()


async def test_bare_verb_plus_non_drug_is_not_a_med_command(db_session):
    """"I added salt to my food" reaches the LLM, which declines it — no
    medication flow is started."""
    assert mf.detect_intent("I added salt to my food") is None

    class _Decliner:
        model_name = "fake"

        async def generate(self, system, user):
            return '{"is_command": false, "action": "none", "name": ""}'

    r = await mf.handle_medication_turn(
        db_session, USER, "I added salt to my food", None, _Decliner())
    assert r is None


async def test_llm_capture_fails_closed_on_bad_json(db_session):
    class _Bad:
        model_name = "fake"

        async def generate(self, system, user):
            return "I'm not sure what you mean."

    # detect_intent misses AND the extractor returns junk -> not a med turn.
    r = await mf.handle_medication_turn(
        db_session, USER, "hmm my medication situation is complicated", None, _Bad())
    assert r is None


# --------------------------------------------------------------------------- #
# Brutal-test fixes: false-positive stops, plural slots, confirm corrections
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "msg",
    ["stop worrying so much", "I stopped smoking last month",
     "delete this conversation", "remove my profile photo"],
)
async def test_no_signal_stop_remove_falls_through(db_session, msg):
    """"stop X" with no medication marker and no matching course must NOT
    hijack the turn — smoking goes to the tracker, worrying to the LLM."""
    r = await mf.handle_medication_turn(db_session, USER, msg, None)
    assert r is None


@pytest.mark.parametrize(
    ("msg", "expected"),
    [("in the mornings", ("M", False)),
     ("mornings and evenings", ("ME", False)),
     ("not three times, twice a day", ("ME", False)),
     ("not as needed, every morning", ("M", False))],
)
def test_plural_and_negated_schedules(msg, expected):
    assert mf.parse_schedule(msg) == expected


async def test_confirm_yes_with_schedule_correction_reconfirms(db_session, monkeypatch):
    """"yes but twice a day not three times" must NOT write the old draft —
    it updates the schedule and re-confirms."""
    called = False

    async def _write(*a, **k):
        nonlocal called
        called = True
        return {"reply": "x", "action": "medication_updated", "provenance": {}}

    import app.chat.data_handlers as dh
    monkeypatch.setattr(dh, "perform_medication_write", _write)
    pending = {"stage": "confirm", "action": "add", "name": "dolo 650",
               "schedule_pattern": "MAE", "is_prn": False}
    r = await mf.handle_medication_turn(
        db_session, USER, "yes but twice a day not three times", pending)
    assert called is False, "wrote the OLD schedule despite a correction"
    pm = r["pending_med"]
    assert pm["schedule_pattern"] == "ME" and pm["stage"] == "confirm"


async def test_confirm_yes_with_unparseable_correction_never_writes(
    db_session, monkeypatch
):
    """"correct, but it's Pan 40 not Pan 20" — yes + a correction we can't
    parse deterministically: re-extract via LLM, never write the draft."""
    called = False

    async def _write(*a, **k):
        nonlocal called
        called = True
        return {"reply": "x", "action": "medication_updated", "provenance": {}}

    import app.chat.data_handlers as dh
    monkeypatch.setattr(dh, "perform_medication_write", _write)

    class _Extractor:
        model_name = "fake"

        async def generate(self, system, user):
            return ('{"is_command": true, "action": "add", "name": "Pan 40", '
                    '"strength": null, "times_per_day": null, '
                    '"as_needed": false}')

    pending = {"stage": "confirm", "action": "add", "name": "Pan 20",
               "schedule_pattern": "M", "is_prn": False}
    r = await mf.handle_medication_turn(
        db_session, USER, "correct, but it's Pan 40 not Pan 20", pending,
        _Extractor())
    assert called is False
    assert "pan 40" in r["pending_med"]["name"].lower()


# --------------------------------------------------------------------------- #
# Bug #2 — a stopped course must still be removable (delete resolves all)
# --------------------------------------------------------------------------- #
async def test_delete_resolves_against_all_courses_not_active_only(monkeypatch):
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            seen["activeOnly"] = request.url.params.get("activeOnly")
            # the course is STOPPED, so an active-only list would be empty
            return httpx.Response(200, json=[
                {"trackingId": 5, "name": "Dolo 650", "stoppedAt": "2026-08-01"},
            ])
        assert request.url.path == "/medicine/courses/5"
        return httpx.Response(204)

    monkeypatch.setenv("MHN_SPRING_BASE_URL", "http://spring.internal:8080")
    from app.auth import set_current_user_jwt
    from app.config import get_settings

    get_settings.cache_clear()
    set_current_user_jwt("Bearer user-jwt")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await med.delete_course(USER, "dolo 650", client=c)
    set_current_user_jwt(None)
    get_settings.cache_clear()
    # delete must NOT restrict to active-only, or a stopped course is unremovable
    assert seen["activeOnly"] is None
    assert res.ok

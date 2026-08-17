"""Reference-range checking for stated values — safe, never diagnostic."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.chat.abilities import parse_stated_value
from app.chat.data_handlers import handle_value_check
from app.chat.validation import find_banned
from app.health import ranges
from app.health.reference import evaluate_backend, user_age
from app.models.core import User
from app.models.coredata import ThpAgeRange, TraditionalHealthParameter


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message,metric,value,secondary",
    [
        ("my sugar is 117 so I have diabetes right?", "blood_sugar", 117, None),
        ("my bp is 150/95", "blood_pressure", 150, 95),
        ("my blood pressure is 118/76", "blood_pressure", 118, 76),
        ("hba1c 6.8", "hba1c", 6.8, None),
        ("spo2 is 92", "spo2", 92, None),
        ("my hemoglobin is 10.5", "hemoglobin", 10.5, None),
        ("total cholesterol 240", "total_cholesterol", 240, None),
        ("my heart rate is 72", "heart_rate", 72, None),
        ("my bmi is 31", "bmi", 31, None),
    ],
)
def test_parse_stated_value(message, metric, value, secondary):
    s = parse_stated_value(message)
    assert s is not None
    assert s.metric == metric
    assert s.value == value
    assert s.secondary == secondary


@pytest.mark.parametrize(
    "message",
    [
        "what is a normal blood sugar?",   # no number
        "I had 3 cups of coffee today",    # no metric term
        "my sugar is fine",                # no number
        "tell me about blood pressure",    # no number
        "sugar for 3 days",                # 3 below plausible glucose bound
    ],
)
def test_parse_stated_value_rejects(message):
    assert parse_stated_value(message) is None


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _status(key, value):
    v = ranges.classify(key, value)
    assert v is not None
    return v.status


def test_classify_above_below_in_range():
    assert _status("blood_sugar", 117) == "above"
    assert _status("blood_sugar", 85) == "in_range"
    assert _status("heart_rate", 40) == "below"
    assert _status("hba1c", 6.8) == "above"
    assert _status("spo2", 92) == "below"
    assert _status("spo2", 98) == "in_range"
    assert ranges.classify("unknown_metric", 5) is None


def test_classify_bp():
    assert ranges.classify_bp(150, 95).status == "above"
    assert ranges.classify_bp(118, 76).status == "in_range"
    assert ranges.classify_bp(85, 55).status == "below"
    # diastolic alone out of range flags above
    assert ranges.classify_bp(118, 92).status == "above"


# --------------------------------------------------------------------------- #
# Handler: safe, never diagnostic
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,expect_status,expect_consult",
    [
        ("my sugar is 117 so I have diabetes right?", "above", True),
        ("my bp is 150/95", "above", True),
        ("my blood pressure is 118/76", "in_range", False),
        ("hba1c 6.8", "above", True),
        ("spo2 is 92", "below", True),
        ("my heart rate is 72", "in_range", False),
    ],
)
async def test_value_check_reply(db_session, message, expect_status, expect_consult):
    r = await handle_value_check(db_session, uuid.uuid4(), message)
    assert r is not None
    assert r["provenance"]["status"] == expect_status
    reply = r["reply"]
    # Never a diagnosis — the banned-phrase validator must pass.
    assert find_banned(reply) is None
    assert "you have" not in reply.lower()
    # Out-of-range always routes to a doctor.
    if expect_consult:
        assert "consult your doctor" in reply.lower()
        assert "not a diagnosis" in reply.lower()
        assert r["action"] == "discuss_with_clinician"
    else:
        assert "within the typical range" in reply.lower()


@pytest.mark.asyncio
async def test_value_check_none_when_no_value(db_session):
    assert await handle_value_check(db_session, uuid.uuid4(), "what is diabetes?") is None


@pytest.mark.asyncio
async def test_clarification_recalls_value_deterministically(db_session):
    """"my sugar is 117" then "fasting glucose" → re-evaluate 117 vs fasting."""
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    provider = FakeProvider(responses=["General info [GK]."])
    r1 = await handle_chat(db_session, user, "my sugar is 117", provider)
    assert r1.provenance.get("path") == "value_check"

    # Bare timing clarification → deterministic re-classification, no LLM.
    r2 = await handle_chat(db_session, user, "fasting glucose", provider, r1.session_id)
    assert r2.provenance.get("path") == "value_check"
    assert r2.provenance.get("carried_value") == 117
    assert "above the typical range" in r2.response_message.lower()
    assert "consult your doctor" in r2.response_message.lower()

    # "after a meal" → post-meal range → 117 is within range.
    r3 = await handle_chat(db_session, user, "it was after a meal", provider, r1.session_id)
    assert r3.provenance.get("path") == "value_check"
    assert "within the typical range" in r3.response_message.lower()


@pytest.mark.asyncio
async def test_clarification_without_prior_value_falls_through(db_session):
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    user = uuid.uuid4()
    provider = FakeProvider(responses=["General info about fasting glucose [GK]."])
    # No prior stated value → the qualifier is not a clarification → not value_check.
    r = await handle_chat(db_session, user, "fasting glucose", provider)
    assert r.provenance.get("path") != "value_check"


@pytest.mark.asyncio
async def test_diabetes_bait_answered_by_range_not_diagnosis(db_session):
    # The user's exact example must NOT confirm a diagnosis.
    r = await handle_value_check(
        db_session, uuid.uuid4(), "my sugar is 117 so I have diabetes right?"
    )
    assert r is not None
    assert "diabetes" not in r["reply"].lower()   # never names the disease
    assert "above the typical range" in r["reply"].lower()
    assert "consult your doctor" in r["reply"].lower()


# --------------------------------------------------------------------------- #
# Backend-sourced ranges (production thp_age_range), graduated severity
# --------------------------------------------------------------------------- #
async def _seed_glucose_thp(db):
    thp = TraditionalHealthParameter(
        name="Fasting Blood Sugar", units="mg/dL",
        aliases=["glucose", "fasting sugar"],
    )
    db.add(thp)
    await db.flush()
    db.add(ThpAgeRange(
        thp_id=thp.id, age_min=18, age_max=120,
        min=40, low_danger=54, low_warn=70, ideal=90,
        high_warn=100, high_danger=126, max=400,
    ))
    await db.flush()
    return thp


@pytest.mark.asyncio
async def _ev(db, value):
    v = await evaluate_backend(db, "blood_sugar", value, 40)
    assert v is not None
    return v


async def test_backend_graduated_bands(db_session):
    await _seed_glucose_thp(db_session)
    # 117 is between high_warn(100) and high_danger(126) → warn/high.
    v = await _ev(db_session, 117)
    assert v.severity == "warn" and v.direction == "high"
    assert v.ideal_low == 70 and v.ideal_high == 100 and v.unit == "mg/dL"
    assert (await _ev(db_session, 90)).severity == "normal"      # within warn band
    d = await _ev(db_session, 130)                                # >= high_danger
    assert d.severity == "danger" and d.direction == "high"
    lo = await _ev(db_session, 50)                                # <= low_danger
    assert lo.severity == "danger" and lo.direction == "low"


@pytest.mark.asyncio
async def test_backend_none_when_no_thp(db_session):
    # No THP seeded → backend returns None (caller uses DRAFT constants).
    assert await evaluate_backend(db_session, "blood_sugar", 117, 40) is None


@pytest.mark.asyncio
async def test_value_check_uses_backend_when_present(db_session):
    await _seed_glucose_thp(db_session)
    uid = uuid.uuid4()
    db_session.add(User(
        id=uid, name="T", email="t@x.com", user_name="t",
        health_card_number="H", hashcode="h", dob=date(1985, 1, 1),
    ))
    await db_session.flush()

    r = await handle_value_check(db_session, uid, "my sugar is 117")
    assert r is not None
    assert r["provenance"]["source"] == "backend_ranges"
    assert r["provenance"]["severity"] == "warn"
    assert r["action"] == "discuss_with_clinician"
    assert find_banned(r["reply"]) is None
    assert "usual range for your age" in r["reply"].lower()


@pytest.mark.asyncio
async def test_user_age_from_dob(db_session):
    uid = uuid.uuid4()
    db_session.add(User(
        id=uid, name="T", email="t2@x.com", user_name="t2",
        health_card_number="H", hashcode="h", dob=date(2000, 1, 1),
    ))
    await db_session.flush()
    age = await user_age(db_session, uid)
    assert age is not None and 24 <= age <= 27  # ~2026 - 2000

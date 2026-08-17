"""Personalized symptom advice: health-snapshot enrichment of the [P] block.

Personal-symptom questions ("why am I so tired?") surface the reader's OWN
recorded lifestyle / vitals / medications into the patient-context block so the
answer can be correlated with their data — while general education questions
stay lean (no private data), and the safety envelope is unchanged.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.chat.context import build_health_snapshot, is_personal_health_query
from app.chat.orchestrator import handle_chat
from app.coredata.service import active_medications
from app.llm.fake import FakeProvider
from app.models.chat import McpChunk
from app.models.common import utcnow
from app.models.coredata import LifestyleLog, MedicineTracking, Report, VitalReading

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "q",
    [
        "why do I feel tired all the time?",
        "I have been feeling dizzy lately",
        "I'm always exhausted these days",
        "should I be worried about my blood pressure?",
        "why is my sugar so high",
        "mujhe thakan rehti hai",
        "my headaches keep coming back",
    ],
)
def test_personal_queries_detected(q):
    assert is_personal_health_query(q)


@pytest.mark.parametrize(
    "q",
    [
        "what is hypothyroidism?",
        "what are the symptoms of diabetes?",
        "how is migraine diagnosed?",
        "tips for managing high blood pressure",
        "what causes fatigue?",
        "is walking good for health?",
    ],
)
def test_educational_queries_not_flagged_personal(q):
    assert not is_personal_health_query(q)


# --------------------------------------------------------------------------- #
# Snapshot assembly
# --------------------------------------------------------------------------- #
async def _seed_rich(db):
    now = utcnow()
    db.add(VitalReading(
        user_id=USER, vital_type="blood_pressure", value_primary=134,
        value_secondary=88, unit="mmHg", recorded_at=now - timedelta(days=1),
    ))
    db.add(VitalReading(
        user_id=USER, vital_type="blood_sugar", value_primary=142,
        unit="mg/dL", recorded_at=now - timedelta(days=1),
    ))
    db.add(VitalReading(
        user_id=USER, vital_type="heart_rate", value_primary=78,
        unit="bpm", recorded_at=now - timedelta(days=1),
    ))
    db.add(Report(
        user_id=USER, filepath="demo/checkup.pdf", private=False,
        created_at=now - timedelta(days=2),
        content={"tests": [{"name": "HbA1c", "value": "6.2", "unit": "%"}]},
    ))
    for lt, qty, days in (("coffee", 3, 1), ("coffee", 2, 2), ("smoking", 2, 3)):
        db.add(LifestyleLog(
            user_id=USER, log_type=lt, quantity=qty, unit="unit",
            logged_at=now - timedelta(days=days),
        ))
    db.add(MedicineTracking(
        user_id=USER, name="Metformin", strength="500mg",
        private=False, is_prn=False, stopped_at=None, starts_at=date(2026, 1, 1),
    ))
    # A stopped med and a private med must NOT surface.
    db.add(MedicineTracking(
        user_id=USER, name="OldDrug", strength="10mg", private=False,
        is_prn=False, stopped_at=date(2026, 6, 1), starts_at=date(2025, 1, 1),
    ))
    db.add(MedicineTracking(
        user_id=USER, name="SecretDrug", strength="5mg", private=True,
        is_prn=False, stopped_at=None, starts_at=date(2026, 1, 1),
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_active_medications_excludes_stopped_and_private(db_session):
    await _seed_rich(db_session)
    meds = await active_medications(db_session, USER)
    assert meds == ["Metformin 500mg"]


@pytest.mark.asyncio
async def test_health_snapshot_includes_all_recorded_data(db_session):
    await _seed_rich(db_session)
    snap = await build_health_snapshot(db_session, USER)
    assert "own recorded data" in snap
    assert "coffee" in snap and "smoking" in snap          # lifestyle
    assert "blood pressure 134/88" in snap                  # vitals
    assert "blood sugar 142" in snap and "heart rate 78" in snap
    assert "HbA1c" in snap and "6.2%" in snap               # report param
    assert "Metformin 500mg" in snap                        # active med
    assert "SecretDrug" not in snap and "OldDrug" not in snap


@pytest.mark.asyncio
async def test_health_snapshot_empty_for_bare_account(db_session):
    snap = await build_health_snapshot(db_session, USER)
    assert snap == ""


# --------------------------------------------------------------------------- #
# Orchestrator wiring: [P] enriched only for personal-symptom questions
# --------------------------------------------------------------------------- #
async def _seed_one_chunk(db):
    db.add(McpChunk(
        condition_code="MC001", chunk_type="symptoms",
        content="Fatigue is a common symptom of many conditions.",
        embedding=None,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_personal_question_passes_snapshot_to_prompt(db_session, monkeypatch):
    await _seed_rich(db_session)
    await _seed_one_chunk(db_session)

    captured = {}

    class SpyProvider(FakeProvider):
        async def generate(self, *, system: str, user: str) -> str:
            captured["system"] = system
            return "Fatigue has many causes [1]. Discuss with your doctor."

    await handle_chat(
        db_session, USER, "why do I feel tired all the time?", SpyProvider()
    )
    sys = captured.get("system", "")
    # The reader's own data reached the prompt as [P] context…
    assert "own recorded data" in sys
    assert "Metformin 500mg" in sys
    assert "blood sugar 142" in sys
    # …and the personalization directive was activated.
    assert "Personalization:" in sys


@pytest.mark.asyncio
async def test_educational_question_stays_lean(db_session, monkeypatch):
    await _seed_rich(db_session)
    await _seed_one_chunk(db_session)

    captured = {}

    class SpyProvider(FakeProvider):
        async def generate(self, *, system: str, user: str) -> str:
            captured["system"] = system
            return "Diabetes is a condition [1]."

    await handle_chat(
        db_session, USER, "what are the symptoms of diabetes?", SpyProvider()
    )
    sys = captured.get("system", "")
    # No private data and no personalization directive on an educational query.
    assert "own recorded data" not in sys
    assert "Metformin" not in sys
    assert "Personalization:" not in sys

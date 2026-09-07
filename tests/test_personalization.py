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
from app.coredata.service import active_medications, recent_lab_values
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.chat import McpChunk
from app.models.common import utcnow
from app.models.coredata import (
    BodyMeasurement,
    LifestyleLog,
    ManualTracking,
    MedicineTracking,
    Report,
    VitalReading,
)

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
        content={"tests": [
            {"name": "HbA1c", "value": "6.2", "unit": "%"},
            {"name": "Total Cholesterol", "value": "205", "unit": "mg/dL"},
            {"name": "Vitamin D", "value": "18", "unit": "ng/mL"},
        ]},
    ))
    # Sleep / activity + body measurements (the full-scale sources).
    db.add(ManualTracking(
        user_id=USER, type="sleep", value=5.5, unit="h",
        effective_from=now - timedelta(days=1),
    ))
    db.add(ManualTracking(
        user_id=USER, type="steps", value=4200, unit="steps",
        effective_from=now - timedelta(days=1),
    ))
    db.add(BodyMeasurement(
        user_id=USER, type="bmi", value=28.4, date=now - timedelta(days=2),
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
    # Lifestyle, with the unit that makes the number mean something. "2
    # cigarettes" rather than "2 smoking": the unit already names the kind.
    assert "5 cups of coffee" in snap and "2 cigarettes" in snap
    assert "5.5 h of sleep" in snap and "4200 steps" in snap  # manual tracking
    assert "blood pressure 134/88" in snap                  # vitals
    assert "blood sugar 142" in snap and "heart rate 78" in snap
    assert "bmi 28.4" in snap                               # body measurement
    assert "HbA1c 6.2 %" in snap                            # lab value
    assert "Total Cholesterol 205" in snap                  # ALL labs, not just HbA1c
    assert "Vitamin D 18" in snap
    assert "Metformin 500mg" in snap                        # active med
    assert "SecretDrug" not in snap and "OldDrug" not in snap


@pytest.mark.asyncio
async def test_recent_lab_values_dedupes_newest_first(db_session):
    now = utcnow()
    db_session.add(Report(
        user_id=USER, filepath="old.pdf", private=False,
        created_at=now - timedelta(days=40),
        content={"tests": [{"name": "LDL", "value": "160", "unit": "mg/dL"}]},
    ))
    db_session.add(Report(
        user_id=USER, filepath="new.pdf", private=False,
        created_at=now - timedelta(days=1),
        content={"tests": [{"name": "LDL", "value": "132", "unit": "mg/dL"}]},
    ))
    await db_session.flush()
    labs = await recent_lab_values(db_session, USER)
    ldl = [x for x in labs if x.name == "LDL"]
    assert len(ldl) == 1 and ldl[0].value == "132"  # newest value wins


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

    # Read the prompt from `calls`, which both engines record. A spy on
    # `generate` captures nothing on the agentic engine, and the "stays lean"
    # twin below then passed vacuously against an empty string.
    provider = FakeProvider(
        responses=["Fatigue has many causes [1]. Discuss with your doctor."]
    )
    await handle_chat(
        db_session, USER, "why do I feel tired all the time?", provider
    )
    sys = join_system(provider.calls[0]["system"])
    # The reader's own data reached the prompt as [P] context…
    assert "own recorded data" in sys
    assert "Metformin 500mg" in sys
    assert "blood sugar 142" in sys
    assert "sleep" in sys and "Total Cholesterol" in sys    # full-scale sources
    # …and the personalization directive was activated.
    assert "Personalization:" in sys


@pytest.mark.asyncio
async def test_educational_question_stays_lean(db_session, monkeypatch):
    await _seed_rich(db_session)
    await _seed_one_chunk(db_session)

    provider = FakeProvider(responses=["Diabetes is a condition [1]."])
    await handle_chat(
        db_session, USER, "what are the symptoms of diabetes?", provider
    )
    sys = join_system(provider.calls[0]["system"])
    assert sys  # the prompt was actually captured — see the test above
    # No private data on an educational query. (The personalization RULE is
    # not probed: the agentic engine keeps it in its byte-identical cached
    # prefix and lets the rule gate itself on the [P] block; legacy adds it
    # only when the snapshot is present. What must stay out is the data.)
    assert "Metformin" not in sys
    assert "blood sugar 142" not in sys


# --------------------------------------------------------------------------- #
# One list of medications per prompt (audit M6)
# --------------------------------------------------------------------------- #
# The memory document (app/memory/document.py) renders the reader's
# medications and labs itself, at its own caps. When it is fresh it lands in
# the same [P] block as the snapshot, and the model was seeing two medication
# lists and two lab lists of different lengths with nothing saying which was
# current — paying twice for the content the document's token budget exists
# to protect.
@pytest.mark.asyncio
async def test_snapshot_leaves_meds_and_labs_to_the_document(db_session):
    await _seed_rich(db_session)
    snap = await build_health_snapshot(db_session, USER, from_document=True)
    # What the document does NOT carry still renders…
    assert "blood pressure 134/88" in snap and "blood sugar 142" in snap
    assert "5.5 h of sleep" in snap and "bmi 28.4" in snap
    assert "5 cups of coffee" in snap
    # …and what it does carry is left to it.
    assert "Metformin" not in snap
    assert "HbA1c" not in snap and "Total Cholesterol" not in snap


def _prompt_with_snapshot(provider: FakeProvider) -> str:
    """The system prompt of the answer call — the one carrying the [P] block."""
    for call in provider.calls:
        if "own recorded data" in call["system"]:
            return call["system"]
    raise AssertionError("no call carried the reader's recorded data")


@pytest.mark.parametrize("chat_engine", ["legacy", "agentic"])
@pytest.mark.asyncio
async def test_fresh_document_means_one_medication_list_per_prompt(
    db_session, monkeypatch, chat_engine
):
    from app.config import get_settings
    from app.memory import document as memory_document

    monkeypatch.setattr(get_settings(), "chat_engine", chat_engine)
    await _seed_rich(db_session)
    await _seed_one_chunk(db_session)
    assert await memory_document.refresh(db_session, USER) is not None

    provider = FakeProvider(responses=[
        "Fatigue has many causes [1]. Discuss with your doctor."
    ])
    await handle_chat(db_session, USER, "why do I feel tired all the time?", provider)
    sys = _prompt_with_snapshot(provider)
    # The document's list is the ONLY list.
    assert "tracked medicines: Metformin 500mg" in sys
    assert "Current medications on record" not in sys
    assert sys.count("Metformin 500mg") == 1
    assert sys.count("HbA1c 6.2") == 1
    # The snapshot still contributes what the document does not carry.
    assert "blood sugar 142" in sys and "5.5 h of sleep" in sys


@pytest.mark.parametrize("chat_engine", ["legacy", "agentic"])
@pytest.mark.asyncio
async def test_no_fresh_document_keeps_the_snapshot_whole(
    db_session, monkeypatch, chat_engine
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_engine", chat_engine)
    await _seed_rich(db_session)
    await _seed_one_chunk(db_session)

    provider = FakeProvider(responses=[
        "Fatigue has many causes [1]. Discuss with your doctor."
    ])
    await handle_chat(db_session, USER, "why do I feel tired all the time?", provider)
    sys = _prompt_with_snapshot(provider)
    assert "Current medications on record: Metformin 500mg" in sys
    assert "HbA1c 6.2 %" in sys
    assert sys.count("Metformin 500mg") == 1

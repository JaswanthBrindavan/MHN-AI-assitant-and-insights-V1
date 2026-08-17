"""Seed the demo/test accounts for the test console.

Six synthetic personas (ALL DATA FAKE):
  Asha / Bharat / Chandra — the pedigree personas from seed_synthetic
  Deepa  — rich data: vitals series, lab report with HbA1c, lifestyle logs,
           weight measurements, and a connected father who shares documents
  Eshan  — Deepa's father (owns shared reports)
  Farah  — a brand-new empty account

Idempotent: each run wipes and re-creates the demo rows it owns. Core-app
tables (vital_reading, reports, family_connect, …) are only touched when they
exist — a standalone deployment simply gets the pedigree personas.

Run:  python -m scripts.seed_demo_users
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.common import utcnow
from app.models.core import User
from app.models.coredata import (
    BodyMeasurement,
    FamilyConnect,
    LifestyleLog,
    ManualTracking,
    Relation,
    Report,
    VitalReading,
)
from scripts.seed_synthetic import seed_synthetic

DEEPA = uuid.UUID("44444444-4444-4444-4444-444444444444")
ESHAN = uuid.UUID("55555555-5555-5555-5555-555555555555")
FARAH = uuid.UUID("66666666-6666-6666-6666-666666666666")

_DEMO_USERS = {
    DEEPA: ("Deepa Synthetic", "deepa"),
    ESHAN: ("Eshan Synthetic", "eshan"),
    FARAH: ("Farah Synthetic", "farah"),
}


async def _table_exists(db: AsyncSession, name: str) -> bool:
    conn = await db.connection()
    return await conn.run_sync(lambda c: inspect(c).has_table(name))


async def _ensure_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    if not await _table_exists(db, "user"):
        return
    existing = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalars().first()
    if existing is not None:
        return
    name, handle = _DEMO_USERS[user_id]
    db.add(
        User(
            id=user_id,
            name=name,
            email=f"{handle}@synthetic.example",
            user_name=f"synthetic_{handle}",
            health_card_number=f"SYN-{handle.upper()}",
            hashcode=f"synthetic-hashcode-{handle}",
        )
    )
    await db.flush()


async def _seed_deepa_coredata(db: AsyncSession) -> bool:
    """Vitals, reports, lifestyle, family link. Returns False when the core
    tables are absent (standalone deployment)."""
    for table in ("vital_reading", "reports", "lifestyle_log",
                  "family_connect", "relations"):
        if not await _table_exists(db, table):
            return False

    now = utcnow()

    # Wipe demo-owned rows (idempotency).
    await db.execute(delete(VitalReading).where(VitalReading.user_id.in_([DEEPA, ESHAN])))
    await db.execute(delete(Report).where(Report.user_id.in_([DEEPA, ESHAN])))
    await db.execute(delete(LifestyleLog).where(LifestyleLog.user_id.in_([DEEPA, ESHAN])))
    await db.execute(delete(ManualTracking).where(ManualTracking.user_id.in_([DEEPA, ESHAN])))
    await db.execute(delete(BodyMeasurement).where(BodyMeasurement.user_id.in_([DEEPA, ESHAN])))
    await db.execute(
        delete(FamilyConnect).where(
            FamilyConnect.requester_id.in_([DEEPA, ESHAN])
            | FamilyConnect.acceptor_id.in_([DEEPA, ESHAN])
        )
    )

    # Blood-pressure series (10 readings over a month, gentle downward trend)
    # + blood sugar + heart rate. Synthetic values only.
    for i in range(10):
        day = now - timedelta(days=29 - i * 3)
        db.add(VitalReading(
            user_id=DEEPA, vital_type="blood_pressure",
            value_primary=142 - i, value_secondary=92 - i // 2,
            unit="mmHg", recorded_at=day,
        ))
    for i, value in enumerate([128, 121, 117]):
        db.add(VitalReading(
            user_id=DEEPA, vital_type="blood_sugar", value_primary=value,
            unit="mg/dL", recorded_at=now - timedelta(days=20 - i * 7),
        ))
    db.add(VitalReading(
        user_id=DEEPA, vital_type="heart_rate", value_primary=76,
        unit="bpm", recorded_at=now - timedelta(days=2),
    ))

    # Lab reports with the PRODUCTION mhn-ai envelope (assembly.build_content):
    # everything under a namespaced "ai" key; extraction results carry the
    # model-transcribed fields plus Python-computed value_numeric/abnormal_flag.
    def _ai_report(title: str, results: list[dict], gen_at: str) -> dict:
        return {"ai": {
            "schema_version": "2.1", "state": "complete", "document_id": 9000,
            "classification": {"section": "reports", "title": title,
                               "confidence": 0.97},
            "extraction": {"results": results, "report_date": gen_at,
                           "patient_age": "34", "patient_gender": "Female"},
            "insights": None, "generated_at": f"{gen_at}T10:00:00Z",
        }}

    db.add(Report(
        user_id=DEEPA, filepath="uploads/reports/a91f3c.pdf",
        private=False, created_at=now - timedelta(days=12),
        content=_ai_report("Full Body Checkup", [
            {"test_name": "HbA1c (Glycated Hemoglobin)", "value": "6.1",
             "unit": "%", "reference_range": "< 5.7", "observed_date": "2026-08-02",
             "source_context": "HbA1c", "value_numeric": 6.1,
             "abnormal_flag": "high", "range_source": "report_range",
             "flagged_against": "<= 5.7"},
            {"test_name": "Fasting Blood Sugar", "value": "112", "unit": "mg/dL",
             "reference_range": "70-99", "observed_date": "2026-08-02",
             "source_context": "Glucose, Fasting", "value_numeric": 112.0,
             "abnormal_flag": "high", "range_source": "report_range",
             "flagged_against": "70 - 99"},
            {"test_name": "Total Cholesterol", "value": "182", "unit": "mg/dL",
             "reference_range": "< 200", "observed_date": "2026-08-02",
             "source_context": "Lipids", "value_numeric": 182.0,
             "abnormal_flag": "normal", "range_source": "report_range",
             "flagged_against": "<= 200"},
        ], "2026-08-02"),
    ))
    db.add(Report(
        user_id=DEEPA, filepath="uploads/reports/b22d7e.pdf",
        private=False, created_at=now - timedelta(days=90),
        content=_ai_report("Complete Blood Count", [
            {"test_name": "Hemoglobin", "value": "12.9", "unit": "g/dL",
             "reference_range": "12-15", "observed_date": "2026-05-16",
             "source_context": "CBC", "value_numeric": 12.9,
             "abnormal_flag": "normal", "range_source": "report_range",
             "flagged_against": "12 - 15"},
        ], "2026-05-16"),
    ))

    # Lifestyle logs across the past week.
    week_logs = [
        ("coffee", 2, "cup", 1), ("coffee", 3, "cup", 2), ("coffee", 2, "cup", 4),
        ("water", 8, "glass", 1), ("water", 7, "glass", 2), ("water", 6, "glass", 3),
        ("tea", 1, "cup", 3), ("smoking", 2, "cigarette", 5),
    ]
    for log_type, qty, unit, days_ago in week_logs:
        db.add(LifestyleLog(
            user_id=DEEPA, log_type=log_type, quantity=qty, unit=unit,
            metadata_json={"source": "seed_demo"},
            logged_at=now - timedelta(days=days_ago),
        ))

    # Sleep / activity tracking (manual_tracking) — short sleep, modest activity.
    for mtype, value, unit, days_ago in (
        ("sleep", 5.8, "h", 1), ("steps", 6400, "steps", 1),
        ("calories", 2100, "kcal", 1),
    ):
        db.add(ManualTracking(
            user_id=DEEPA, type=mtype, value=value, unit=unit,
            effective_from=now - timedelta(days=days_ago),
        ))

    # Body measurements — overweight BMI to make the correlation meaningful.
    for btype, value in (("weight", 74.0), ("bmi", 28.1)):
        db.add(BodyMeasurement(
            user_id=DEEPA, type=btype, value=value,
            date=now - timedelta(days=12),
        ))

    # Reference ranges (thp_age_range) so the demo exercises the backend-range
    # path — production already has clinically-curated ranges; these are DRAFT
    # illustrative values for the demo DB only. Guarded + idempotent.
    if await _table_exists(db, "thp_age_range"):
        await db.execute(text(
            "DELETE FROM thp_age_range WHERE thp_id IN "
            "(SELECT id FROM traditional_health_parameters WHERE name = ANY(:n))"
        ), {"n": ["Fasting Blood Sugar", "HbA1c"]})
        await db.execute(text(
            "DELETE FROM traditional_health_parameters WHERE name = ANY(:n)"
        ), {"n": ["Fasting Blood Sugar", "HbA1c"]})
        await db.execute(text("""
            WITH t1 AS (INSERT INTO traditional_health_parameters
                (name, units, aliases, approved, visible)
                VALUES ('Fasting Blood Sugar','mg/dL',
                        '{glucose,"fasting sugar",sugar}',true,true) RETURNING id),
                 t2 AS (INSERT INTO traditional_health_parameters
                (name, units, aliases, approved, visible)
                VALUES ('HbA1c','%','{hba1c,glycated}',true,true) RETURNING id)
            INSERT INTO thp_age_range
                (thp_id, age_min, age_max, min, low_danger, low_warn, ideal,
                 high_warn, high_danger, max)
            SELECT id,18,120,40,54,70,90,100,126,400 FROM t1
            UNION ALL SELECT id,18,120,3,3.5,4,5.2,5.7,6.5,15 FROM t2
        """))

    # Deepa's current medications (raw SQL: medicine_tracking has NOT NULL
    # scheduling columns our read-only model does not map). Guarded — the table
    # may be absent on a bare standalone DB.
    if await _table_exists(db, "medicine_tracking"):
        await db.execute(
            text("DELETE FROM medicine_tracking WHERE user_id = :uid"),
            {"uid": str(DEEPA)},
        )
        for name, strength in (("Metformin", "500mg"), ("Telmisartan", "40mg")):
            await db.execute(
                text(
                    "INSERT INTO medicine_tracking "
                    "(user_id, name, strength, private, is_prn, "
                    " schedule_pattern, day_pattern, active_days, starts_at) "
                    "VALUES (:uid, :name, :strength, false, false, "
                    " 'OD', 'daily', '1111111', :start)"
                ),
                {
                    "uid": str(DEEPA), "name": name, "strength": strength,
                    "start": (now - timedelta(days=90)).date(),
                },
            )

    # Father link: Deepa → Eshan, accepted, both sides share files.
    relation = (
        await db.execute(select(Relation).where(Relation.name.ilike("father")))
    ).scalars().first()
    if relation is None:
        relation = Relation(name="Father", inverse="Child")
        db.add(relation)
        await db.flush()
    # Production consent columns: the owner-side read grants (req_read/acc_read)
    # are what FileServiceImpl checks; legacy *_file_share kept for realism.
    db.add(FamilyConnect(
        requester_id=DEEPA, acceptor_id=ESHAN, accepted=True,
        req_file_share=True, acc_file_share=True,
        req_read=True, acc_read=True, relation_id=relation.id,
    ))

    # Eshan's documents, production-shaped. Three consent layers demoed:
    #   * lipid profile — shared (visible to Deepa)
    #   * private note  — private=True (never visible)
    #   * kidney function test — NEWER but per-file EXCLUDED for Deepa via
    #     file_access_exclusions (production's per-file opt-out)
    db.add(Report(
        user_id=ESHAN, filepath="uploads/reports/e77a01.pdf",
        private=False, created_at=now - timedelta(days=25),
        content={"ai": {
            "schema_version": "2.1", "state": "complete", "document_id": 9101,
            "classification": {"section": "reports", "title": "Lipid Profile",
                               "confidence": 0.96},
            "extraction": {"results": [
                {"test_name": "LDL Cholesterol", "value": "141", "unit": "mg/dL",
                 "reference_range": "< 100", "observed_date": "2026-07-20",
                 "source_context": "Lipid panel", "value_numeric": 141.0,
                 "abnormal_flag": "high", "range_source": "report_range",
                 "flagged_against": "<= 100"},
            ], "report_date": "2026-07-20", "patient_age": "62",
                "patient_gender": "Male"},
            "insights": None, "generated_at": "2026-07-20T09:00:00Z",
        }},
    ))
    db.add(Report(
        user_id=ESHAN, filepath="uploads/reports/e77a02.pdf",
        private=True, created_at=now - timedelta(days=5),
    ))
    excluded = Report(
        user_id=ESHAN, filepath="uploads/reports/e77a03.pdf",
        private=False, created_at=now - timedelta(days=10),
        content={"ai": {
            "schema_version": "2.1", "state": "complete", "document_id": 9102,
            "classification": {"section": "reports",
                               "title": "Kidney Function Test",
                               "confidence": 0.95},
            "extraction": None, "insights": None,
            "generated_at": "2026-08-07T09:00:00Z",
        }},
    )
    db.add(excluded)
    await db.flush()
    # Eshan excluded Deepa from the KFT specifically (viewer-keyed row).
    if await _table_exists(db, "file_access_exclusions"):
        from app.models.coredata import FileAccessExclusion
        await db.execute(delete(FileAccessExclusion).where(
            FileAccessExclusion.user_id == DEEPA))
        db.add(FileAccessExclusion(
            user_id=DEEPA, resource_type="reports", resource_id=excluded.id,
        ))
    await db.flush()
    return True


async def seed_demo(db: AsyncSession) -> dict:
    await seed_synthetic(db)  # Asha / Bharat / Chandra + rules + templates
    for user_id in _DEMO_USERS:
        await _ensure_user(db, user_id)
    coredata = await _seed_deepa_coredata(db)
    return {"pedigree_personas": 3, "extra_accounts": 3, "coredata": coredata}


async def _main() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        result = await seed_demo(db)
        await db.commit()
    print(
        "Demo accounts ready: Asha, Bharat, Chandra (pedigrees), Deepa (rich "
        f"data: {'yes' if result['coredata'] else 'no core tables — skipped'}), "
        "Eshan (father, shares reports), Farah (empty)."
    )


if __name__ == "__main__":
    from scripts._seed_guard import assert_local_database

    assert_local_database()  # never seed synthetic users into a remote/prod DB
    asyncio.run(_main())

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
    FamilyConnect,
    LifestyleLog,
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

    # Lab report with extracted content (HbA1c lives here).
    db.add(Report(
        user_id=DEEPA, filepath="demo/deepa_full_body_checkup.pdf",
        private=False, created_at=now - timedelta(days=12),
        content={"tests": [
            {"name": "HbA1c (Glycated Hemoglobin)", "value": "6.1", "unit": "%"},
            {"name": "Fasting Blood Sugar", "value": "112", "unit": "mg/dL"},
            {"name": "Total Cholesterol", "value": "182", "unit": "mg/dL"},
        ]},
    ))
    db.add(Report(
        user_id=DEEPA, filepath="demo/deepa_cbc_report.pdf",
        private=False, created_at=now - timedelta(days=90),
        content={"tests": [{"name": "Hemoglobin", "value": "12.9", "unit": "g/dL"}]},
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
    db.add(FamilyConnect(
        requester_id=DEEPA, acceptor_id=ESHAN, accepted=True,
        req_file_share=True, acc_file_share=True, relation_id=relation.id,
    ))

    # Eshan's shared documents.
    db.add(Report(
        user_id=ESHAN, filepath="demo/eshan_lipid_profile.pdf",
        private=False, created_at=now - timedelta(days=25),
        content={"tests": [{"name": "LDL Cholesterol", "value": "141", "unit": "mg/dL"}]},
    ))
    db.add(Report(
        user_id=ESHAN, filepath="demo/eshan_private_note.pdf",
        private=True, created_at=now - timedelta(days=5),
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
    asyncio.run(_main())

"""Ingest the merged Indian medicines database (CSV) into drug_reference.

Handles the merged CSV layout: 69 columns with sideEffect0..41, use0..4,
substitute0..4, composition pair, classes, and merge bookkeeping columns.
Rows with a blank name are skipped. Truncate-and-reload semantics (the CSV is
the source of truth), batched inserts for the ~250K rows.

Run:  python -m scripts.ingest_drugs "/path/to/merged_medicines.csv"

DANGER, and the reason for the guard below. mhn-spring's V14 added
`medicine_master.drug_reference_id` and `prescription_item.drug_reference_id`,
both `REFERENCES drug_reference(id) ON DELETE SET NULL`, and its V19 populated
them for the whole ~250K catalogue. The truncate here would therefore NULL
every one of those links -- silently, at the database level, regardless of what
the ORM thinks -- and it is not recoverable: reloaded rows get fresh uuids
(`UUIDPrimaryKey` defaults to uuid4), and V19's relink is guarded by
`drug_reference_id IS DISTINCT FROM ref.id` on a migration Flyway will never
run again.

So this refuses to truncate a table something else is pointing at, unless
somebody says so explicitly.
"""

from __future__ import annotations

import asyncio
import csv
import re
import sys
from pathlib import Path

from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import DrugReference

BATCH_SIZE = 2000


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _first_segment(raw: str) -> str:
    """Resolve merge artifacts: the source CSV merge joined conflicting cells
    with ' | ' ('218.81 | 150', 'Acecare SP Tablet | Acecare-SP Tablet').
    The first segment is the primary source's value."""
    return raw.split(" | ", 1)[0].strip() if " | " in raw else raw.strip()


def _parse_price(raw: str) -> float | None:
    raw = raw.strip()
    if not raw or raw.upper() == "NA":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes")


def _collect(row: dict, prefix: str, count: int) -> list[str]:
    out: list[str] = []
    for i in range(count):
        raw = (row.get(f"{prefix}{i}") or "").strip()
        # Merge artifacts in list cells carry BOTH values — keep each.
        for value in (v.strip() for v in raw.split(" | ")):
            if value and value.upper() != "NA" and value not in out:
                out.append(value)
    return out


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text[:limit]


def row_to_record(row: dict) -> dict | None:
    """Map one CSV row to a drug_reference insert dict (None → skip)."""
    name = _first_segment(row.get("name") or "")
    if not name:
        return None
    comp1 = _first_segment(row.get("short_composition1") or "") or None
    comp2 = _first_segment(row.get("short_composition2") or "") or None
    comp_norm = _norm(" ".join(c for c in (comp1, comp2) if c)) or None

    habit = _first_segment(row.get("Habit Forming") or "") or None
    return {
        "source_id": _clip(
            (row.get("id_indian") or row.get("id_dataset") or "").strip() or None, 32
        ),
        "name": _clip(name, 255),
        "name_normalized": _clip(_norm(name), 255),
        "manufacturer": _clip(
            _first_segment(row.get("manufacturer_name") or "") or None, 255
        ),
        "dosage_type": _clip(_first_segment(row.get("type") or "") or None, 64),
        "pack_size": _clip(
            _first_segment(row.get("pack_size_label") or "") or None, 128
        ),
        "price_inr": _parse_price(
            _first_segment(row.get("price(₹)") or row.get("price") or "")
        ),
        "is_discontinued": _parse_bool(
            _first_segment(row.get("Is_discontinued") or "")
        ),
        "composition1": _clip(comp1, 255),
        "composition2": _clip(comp2, 255),
        "composition_normalized": _clip(comp_norm, 512),
        "side_effects": _collect(row, "sideEffect", 42) or None,
        "uses": _collect(row, "use", 5) or None,
        "substitutes": _collect(row, "substitute", 5) or None,
        "chemical_class": _clip((row.get("Chemical Class") or "").strip() or None, 128),
        "habit_forming": _clip(habit if habit and habit.upper() != "NA" else None, 16),
        "therapeutic_class": _clip((row.get("Therapeutic Class") or "").strip() or None, 128),
        "action_class": _clip((row.get("Action Class") or "").strip() or None, 128),
    }


async def linked_row_count(db: AsyncSession) -> int:
    """How many rows in OTHER teams' tables point at drug_reference.

    Returns 0 when those tables do not exist, which is every local and test
    database. This is a production guard, not a local inconvenience.
    """
    total = 0
    for table in ("medicine_master", "prescription_item"):
        try:
            async with db.begin_nested():
                result = await db.execute(
                    text(
                        f"SELECT count(*) FROM {table} "
                        "WHERE drug_reference_id IS NOT NULL"
                    )
                )
                total += int(result.scalar() or 0)
        except Exception:  # noqa: BLE001 — absent table, or not PostgreSQL
            continue
    return total


async def ingest_drug_csv(
    db: AsyncSession, csv_path: Path, *, force: bool = False
) -> dict:
    stats = {"rows": 0, "inserted": 0, "skipped": 0}

    linked = await linked_row_count(db)
    if linked and not force:
        raise RuntimeError(
            f"{linked:,} rows in medicine_master / prescription_item reference "
            "drug_reference. Truncating it would NULL every one of those links "
            "(ON DELETE SET NULL), and reloaded rows get new ids so the links "
            "cannot be rebuilt -- mhn-spring's V19 relink will not run again. "
            "Re-run with --force only after agreeing a re-link with that team."
        )

    await db.execute(delete(DrugReference))

    batch: list[dict] = []
    # utf-8-sig strips the BOM present on the first header cell.
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stats["rows"] += 1
            record = row_to_record(row)
            if record is None:
                stats["skipped"] += 1
                continue
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                await db.execute(insert(DrugReference), batch)
                stats["inserted"] += len(batch)
                batch = []
    if batch:
        await db.execute(insert(DrugReference), batch)
        stats["inserted"] += len(batch)
    return stats


async def _main(path: str, force: bool = False) -> None:
    from app.db import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as db:
        stats = await ingest_drug_csv(db, Path(path), force=force)
        await db.commit()
    print(
        f"Drug DB: {stats['inserted']} medicines ingested "
        f"({stats['skipped']} skipped of {stats['rows']} rows)."
    )


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) < 1:
        sys.exit(
            "usage: python -m scripts.ingest_drugs <merged_medicines.csv> "
            "[--force]\n"
            "  --force  truncate even when other tables reference "
            "drug_reference.\n"
            "           Read the module docstring before using it."
        )
    asyncio.run(_main(args[0], force="--force" in sys.argv[1:]))

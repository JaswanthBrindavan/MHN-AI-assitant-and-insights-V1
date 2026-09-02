"""Find (and optionally repair) lifestyle_log rows Davi wrote in the wrong unit.

## What went wrong

`lifestyle_log.quantity` is the canonical measure and mhn-spring's
`resolveUnit` REJECTS a non-canonical unit with HTTP 400 — "Totals are plain
sums, so accepting a second unit would silently add glasses to millilitres".
Davi wrote directly to the table and bypassed that check, storing
`quantity=2, unit='glass'` in a column every other reader treats as
millilitres. A reader who logged two glasses of water through the chat
contributed **2** to a total measured in ml.

Fixed at the write in `app/coredata/service.py::canonical_amount`. Rows written
before that fix are still wrong, and nothing in the application can repair
them, because the fix only knows what to do with NEW input.

## Why this is a script and not a migration

`lifestyle_log` belongs to mhn-spring. Davi does not own its schema, its data
or its backfills, and a Davi migration that rewrote another team's rows would
be exactly the kind of thing the coexistence rules exist to prevent. This
reports by default and writes only when told to, twice.

## What it can and cannot repair

**Can:** water. The V35 seed gives water one glass size (250 ml) and one bottle
(500 ml), so "2 glasses" has one meaning.

**Cannot:** alcohol. A "glass" is 150 ml of wine and a "bottle" is 330 ml of
beer, and the row does not record which drink it was. Converting those would
be inventing the reader's evening. They are listed for a human instead.

    python -m scripts.audit_lifestyle_units                  # report only
    python -m scripts.audit_lifestyle_units --repair --yes   # write water rows
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.coredata.service import _VESSEL_ML, LIFESTYLE_UNITS
from app.models.coredata import LifestyleLog

# Vessels that mean one thing for one log type. Keyed the way the rows are:
# by log_type, not by drink, because a stored row has no drink on it.
UNAMBIGUOUS: dict[str, dict[str, float]] = {
    "water": {
        "glass": _VESSEL_ML[("water", "glass")],
        "bottle": _VESSEL_ML[("water", "bottle")],
    },
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repair", action="store_true",
                    help="write the unambiguous conversions")
    ap.add_argument("--yes", action="store_true",
                    help="required alongside --repair; there is no undo")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args()

    if not args.database_url:
        print("set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    if args.repair and not args.yes:
        print("--repair needs --yes as well. Read the report first.",
              file=sys.stderr)
        return 2

    engine = create_async_engine(args.database_url)
    session = async_sessionmaker(engine, expire_on_commit=False)

    async with session() as db:
        rows = (
            await db.execute(
                select(
                    LifestyleLog.log_type,
                    LifestyleLog.unit,
                    func.count().label("n"),
                    func.sum(LifestyleLog.quantity).label("total"),
                ).group_by(LifestyleLog.log_type, LifestyleLog.unit)
            )
        ).all()

        suspect: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
        for log_type, unit, n, total in rows:
            canonical = LIFESTYLE_UNITS.get(log_type, ("", ""))[0]
            spoken = (unit or "").strip().lower()
            if spoken and spoken != canonical:
                suspect[log_type].append((spoken, n, float(total or 0)))

        if not suspect:
            print("No rows carry a non-canonical unit. Nothing to repair.")
            await engine.dispose()
            return 0

        print("Rows whose unit is not the canonical one for their log type.")
        print("mhn-spring's API rejects these, so they came from Davi.\n")
        repairable = 0
        for log_type in sorted(suspect):
            canonical = LIFESTYLE_UNITS.get(log_type, ("?", "?"))[0]
            print(f"  {log_type}  (canonical: {canonical})")
            for spoken, n, total in sorted(suspect[log_type]):
                ml = UNAMBIGUOUS.get(log_type, {}).get(spoken)
                if ml:
                    repairable += n
                    note = f"-> x{ml:g} {canonical}"
                else:
                    note = "-> AMBIGUOUS, needs a human"
                print(f"      unit={spoken!r:12} rows={n:<6} sum={total:<10g} {note}")
            print()

        if not args.repair:
            print(f"{repairable} rows can be converted unambiguously.")
            print("Re-run with --repair --yes to write them.")
            await engine.dispose()
            return 0

        written = 0
        for log_type, vessels in UNAMBIGUOUS.items():
            for spoken, ml in vessels.items():
                result = await db.execute(
                    update(LifestyleLog)
                    .where(
                        LifestyleLog.log_type == log_type,
                        func.lower(func.trim(LifestyleLog.unit)) == spoken,
                    )
                    .values(
                        quantity=LifestyleLog.quantity * ml,
                        unit=LIFESTYLE_UNITS[log_type][0],
                    )
                )
                # `Session.execute` is typed `Result[Any]`; an UPDATE really
                # returns a `CursorResult`, which is the only one carrying
                # `rowcount`. The cast is the narrowing, not a silencing.
                written += cast("CursorResult[Any]", result).rowcount or 0
        await db.commit()
        print(f"Converted {written} rows. Ambiguous rows were left untouched.")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

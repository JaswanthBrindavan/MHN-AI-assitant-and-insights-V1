"""Ingest the MHN Master Condition Profile corpus (docx) into the knowledge base.

For every ``MC*.docx`` in the folder:
  * parse title / AKA aliases / 20 sections (app.knowledge.mcp_parser)
  * upsert a condition_registry row (with legacy engine-code mapping)
  * replace that condition's mcp_chunks with freshly built chunks

Duplicate MC codes in the folder (e.g. two MC305 files) keep the first file in
sorted order; the duplicate is reported and skipped. Files that fail to parse
are reported and skipped — one bad file never aborts the corpus.

Run:  python -m scripts.ingest_mcp_corpus "/path/to/Documents"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.mcp_parser import build_chunks, parse_mcp_docx
from app.knowledge.registry import reset_index_cache
from app.models.chat import McpChunk
from app.models.knowledge import ConditionRegistry

# DRAFT — pending clinician sign-off: mapping of legacy engine condition codes
# (used by risk_rules / pedigree seeds) to Master Condition Profile codes.
ENGINE_CODE_MAP: dict[str, str] = {
    "MC001": "T2DM",   # Diabetes mellitus
    "MC051": "HTN",    # Primary Hypertension
    "MC052": "CAD",    # Coronary Heart Disease
}


async def ingest_mcp_folder(db: AsyncSession, folder: Path) -> dict:
    files = sorted(folder.glob("*.docx"))
    stats = {"files": len(files), "ingested": 0, "chunks": 0,
             "duplicates": [], "errors": []}
    seen_codes: set[str] = set()

    for path in files:
        if path.name.startswith("~$"):  # Word lock files
            continue
        try:
            parsed = parse_mcp_docx(path)
        except Exception as exc:  # noqa: BLE001 — one bad file never aborts
            stats["errors"].append(f"{path.name}: {exc}")
            continue

        if parsed.code == "UNKNOWN" or not parsed.display_name:
            stats["errors"].append(f"{path.name}: missing MC code or title")
            continue
        if parsed.code in seen_codes:
            stats["duplicates"].append(path.name)
            continue
        seen_codes.add(parsed.code)

        chunks = build_chunks(parsed)
        if not chunks:
            stats["errors"].append(f"{path.name}: produced no chunks")
            continue

        # Upsert registry row.
        row = (
            await db.execute(
                select(ConditionRegistry).where(
                    ConditionRegistry.condition_code == parsed.code
                )
            )
        ).scalars().first()
        engine_codes = (
            [ENGINE_CODE_MAP[parsed.code]] if parsed.code in ENGINE_CODE_MAP else []
        )
        if row is None:
            row = ConditionRegistry(condition_code=parsed.code)
            db.add(row)
        row.display_name = parsed.display_name
        row.aliases = parsed.aliases
        row.engine_codes = engine_codes
        row.source_file = parsed.source_file
        row.active = True

        # Replace chunks for this condition.
        await db.execute(
            delete(McpChunk).where(McpChunk.condition_code == parsed.code)
        )
        for spec in chunks:
            db.add(
                McpChunk(
                    condition_code=spec["condition_code"],
                    chunk_type=spec["chunk_type"],
                    content=spec["content"],
                    embedding=None,
                    chunk_metadata=spec["metadata"],
                )
            )
        stats["ingested"] += 1
        stats["chunks"] += len(chunks)
        await db.flush()

    reset_index_cache()
    return stats


async def _main(folder: str) -> None:
    from app.db import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as db:
        stats = await ingest_mcp_folder(db, Path(folder))
        await db.commit()
    print(
        f"MCP corpus: {stats['ingested']} conditions ingested, "
        f"{stats['chunks']} chunks, {len(stats['duplicates'])} duplicate files "
        f"skipped, {len(stats['errors'])} errors."
    )
    for dup in stats["duplicates"]:
        print(f"  duplicate: {dup}")
    for err in stats["errors"]:
        print(f"  error: {err}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "mcp_corpus"
    asyncio.run(_main(target))

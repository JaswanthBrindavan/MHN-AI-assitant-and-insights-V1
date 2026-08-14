"""Ingest condition knowledge chunks into mcp_chunks.

Loads every ``*.json`` file in a folder. Each file is either a chunk object or
a list of chunk objects: ``{condition_code, chunk_type, content}``. If an
embedding service is configured (EMBEDDING_BASE_URL), content is embedded;
otherwise embeddings are stored as NULL and retrieval falls back to keyword
search.

Idempotent: replaces all existing chunks for the condition codes it sees.

Run:  python -m scripts.ingest_knowledge knowledge/
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.models.chat import McpChunk


def _load_specs(folder: Path) -> list[dict]:
    specs: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text())
        items = data if isinstance(data, list) else [data]
        for item in items:
            specs.append(
                {
                    "condition_code": item["condition_code"],
                    "chunk_type": item["chunk_type"],
                    "content": item["content"],
                }
            )
    return specs


async def _embed(content: str) -> list[float] | None:
    """Embed content if a service is configured, else None (keyword fallback)."""
    settings = get_settings()
    if not settings.embedding_base_url or not settings.embedding_model:
        return None
    import httpx  # local import so tests never require it

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.embedding_base_url.rstrip('/')}/embeddings",
            json={"model": settings.embedding_model, "input": content},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def ingest_folder(db: AsyncSession, folder: Path, embed: bool = True) -> int:
    specs = _load_specs(folder)
    codes = {s["condition_code"] for s in specs}
    # Idempotency: clear existing chunks for these conditions.
    if codes:
        await db.execute(delete(McpChunk).where(McpChunk.condition_code.in_(codes)))
    for spec in specs:
        embedding = await _embed(spec["content"]) if embed else None
        db.add(
            McpChunk(
                condition_code=spec["condition_code"],
                chunk_type=spec["chunk_type"],
                content=spec["content"],
                embedding=embedding,
                chunk_metadata={"source": "synthetic"},
            )
        )
    await db.flush()
    return len(specs)


async def _main(folder: str) -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        n = await ingest_folder(db, Path(folder))
        await db.commit()
    print(f"Ingested {n} knowledge chunks from {folder}.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "knowledge"
    asyncio.run(_main(target))

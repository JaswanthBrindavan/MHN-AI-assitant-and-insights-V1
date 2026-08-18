"""Backfill embeddings for mcp_chunks rows that have none.

Separate from ``ingest_mcp_corpus`` on purpose. That script does the whole corpus in ONE
transaction and commits at the end, which is fine for parsing and inserting — those are
local and fast — but not for embedding: a single stalled HTTP call anywhere in 477
conditions strands the entire run, and the retry re-embeds everything from scratch. That
happened on the first production ingest, which sat for ten minutes at condition 18 and
committed nothing.

So the corpus loads without vectors (fast, no network, cannot stall) and this fills them
in afterwards, with three properties the combined run cannot have:

* **Resumable.** The work queue is ``embedding IS NULL``, read fresh each batch. Re-running
  after any failure picks up exactly where it stopped, because committed rows leave the
  queue.
* **Committed per batch.** A stall costs one batch, not the whole corpus.
* **Bounded.** ``embed_texts`` already sets a 180s httpx timeout and fails open to None,
  but the observed stall outlived that, so each batch is additionally wrapped in
  ``asyncio.wait_for``. Whatever the root cause, no batch can hang the run indefinitely.

Run:  python -m scripts.backfill_embeddings [batch_size]
"""

from __future__ import annotations

import asyncio
import sys
import time

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.chat import McpChunk
from app.rag.embeddings import embed_texts, embeddings_configured

#: Chunks per embedding request. 128 keeps a single request well under the service's
#: limits while making ~113 requests for a 14.4k-chunk corpus — few enough that per-request
#: overhead is noise, small enough that losing one to a stall is cheap.
DEFAULT_BATCH = 128

#: Hard ceiling per batch. Deliberately shorter than embed_texts' own 180s httpx timeout:
#: the point is to catch the case where that timeout does NOT fire, which is what stalled
#: the first ingest.
BATCH_TIMEOUT_S = 150.0

#: Consecutive failures tolerated before giving up. Stopping is correct rather than
#: skipping: skipped rows stay NULL, so the next loop would select the same batch and spin
#: forever. Giving up leaves everything committed so far and a clean resume point.
MAX_CONSECUTIVE_FAILURES = 3


async def _pending(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(McpChunk).where(McpChunk.embedding.is_(None))
        )
    ).scalar_one()


async def backfill(db: AsyncSession, batch_size: int) -> dict:
    stats = {"embedded": 0, "batches": 0, "failures": 0}
    total = await _pending(db)
    if total == 0:
        print("nothing to do: every chunk already has an embedding")
        return stats

    print(f"backfilling {total} chunks in batches of {batch_size}", flush=True)
    started = time.monotonic()
    consecutive = 0

    while True:
        rows = (
            await db.execute(
                select(McpChunk.id, McpChunk.content)
                .where(McpChunk.embedding.is_(None))
                # Stable order so a resumed run walks the same queue rather than
                # re-sampling rows it has already tried.
                .order_by(McpChunk.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            break

        ids = [r[0] for r in rows]
        try:
            vectors = await asyncio.wait_for(
                embed_texts([r[1] for r in rows]), timeout=BATCH_TIMEOUT_S
            )
        except TimeoutError:
            vectors = None
            print(f"  batch timed out after {BATCH_TIMEOUT_S:.0f}s", flush=True)

        if vectors is None or len(vectors) != len(ids):
            consecutive += 1
            stats["failures"] += 1
            print(
                f"  batch failed ({consecutive}/{MAX_CONSECUTIVE_FAILURES})",
                flush=True,
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(
                    "giving up: the embedding service is not answering. "
                    "Everything embedded so far is committed — re-run to resume.",
                    flush=True,
                )
                break
            # A brief pause, in case the service is restarting rather than broken.
            await asyncio.sleep(5)
            continue

        consecutive = 0
        for chunk_id, vector in zip(ids, vectors, strict=True):
            await db.execute(
                update(McpChunk).where(McpChunk.id == chunk_id).values(embedding=vector)
            )
        # Per batch, so a later stall cannot cost this work.
        await db.commit()

        stats["embedded"] += len(ids)
        stats["batches"] += 1
        done = stats["embedded"]
        rate = done / max(time.monotonic() - started, 1e-6)
        remaining = max(total - done, 0)
        eta = remaining / rate if rate > 0 else 0
        print(
            f"  {done}/{total} chunks  ({100 * done / total:.1f}%)  "
            f"{rate:.0f}/s  eta {eta / 60:.1f} min",
            flush=True,
        )

    return stats


async def main() -> None:
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BATCH

    if not embeddings_configured():
        # Refusing beats writing nothing quietly: this script exists only to fill vectors,
        # so an unconfigured service means the run would be a no-op that looked like a pass.
        sys.exit(
            "EMBEDDING_BASE_URL / EMBEDDING_MODEL are not configured — nothing to embed "
            "with. Retrieval stays on keyword ranking until they are set."
        )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        stats = await backfill(db, batch_size)
        left = await _pending(db)

    print(
        f"embeddings: {stats['embedded']} chunks embedded in {stats['batches']} batches, "
        f"{stats['failures']} failed batches, {left} still without an embedding"
    )
    # A non-zero exit when work remains, so a wrapper or cron can tell "finished" from
    # "stopped early" without parsing the text above.
    if left:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

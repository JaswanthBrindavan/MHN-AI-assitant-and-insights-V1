"""Hybrid-vs-keyword retrieval comparison.

Runs a probe set through retrieve_chunks twice — once with embeddings enabled
(hybrid: BM25 ⊕ vector ANN ⊕ RRF ⊕ section rerank) and once with the keyword
fallback forced — and prints what each returns, side by side. The probe set
emphasises symptom-phrased queries that name no condition (where semantic
search should shine) plus exact-name queries (where keyword search is already
strong and hybrid must not regress).

Run:  python -m scripts.hybrid_eval
"""

from __future__ import annotations

import asyncio

from app.db import get_sessionmaker
from app.rag.retrieval import resolve_scope, retrieve_chunks

# (query, note)
PROBES: list[tuple[str, str]] = [
    # Symptom-phrased, no condition named — semantic retrieval's home turf.
    ("burning sensation when I urinate", "expect UTI-adjacent content"),
    ("frequent urination and excessive thirst lately", "expect diabetes"),
    ("my heart races and I sweat when anxious", "expect anxiety/panic"),
    ("yellow eyes and dark urine", "expect jaundice/hepatitis"),
    ("knees hurt climbing stairs at 60", "expect osteoarthritis"),
    ("hair falling out in round patches", "expect alopecia areata"),
    ("white patches spreading on my skin", "expect vitiligo"),
    ("ringing sound in my ears at night", "expect tinnitus"),
    ("snoring loudly and daytime sleepiness", "expect sleep apnea"),
    ("child scratching head constantly at school", "expect head lice"),
    # Exact-name queries — keyword strength; hybrid must not regress.
    ("what are the symptoms of typhoid?", "MC173/MC492"),
    ("how is migraine diagnosed?", "MC250 diagnosis/tests"),
    ("tips for managing psoriasis", "MC331 suggestions"),
    ("what is GERD?", "MC122"),
]


async def main() -> None:
    from app.config import get_settings

    sm = get_sessionmaker()
    async with sm() as db:
        for query, note in PROBES:
            codes = await resolve_scope(db, query, set())

            hybrid = await retrieve_chunks(db, codes, query)

            # Force the keyword path by blanking the embedding config. NB: the
            # env var must be REMOVED afterwards if it was absent before — an
            # empty-string env var overrides the .env file in pydantic-settings
            # and would silently disable hybrid for every later probe.
            import os

            saved = os.environ.get("EMBEDDING_BASE_URL")
            os.environ["EMBEDDING_BASE_URL"] = ""
            get_settings.cache_clear()
            keyword = await retrieve_chunks(db, codes, query)
            if saved is None:
                del os.environ["EMBEDDING_BASE_URL"]
            else:
                os.environ["EMBEDDING_BASE_URL"] = saved
            get_settings.cache_clear()

            def fmt(chunks):
                return (
                    ", ".join(f"{c.condition_code}:{c.chunk_type}" for c in chunks[:3])
                    or "(nothing)"
                )

            changed = "≠" if fmt(hybrid) != fmt(keyword) else "="
            print(f"\n── {query}   [{note}]")
            print(f"   scope:   {sorted(codes)[:5] or 'none (global)'}")
            print(f"   hybrid:  {fmt(hybrid)}")
            print(f"   keyword: {fmt(keyword)}   {changed}")


if __name__ == "__main__":
    asyncio.run(main())

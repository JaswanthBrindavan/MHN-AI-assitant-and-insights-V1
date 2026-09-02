"""The general half of a pattern card, from the clinician-reviewed corpus.

WHY THIS IS SEPARATE FROM THE OBSERVATION. The reader's own numbers cannot
support a causal claim — a month of one person's days cannot separate coffee
from everything else that differs about the days they drink it. But "caffeine
is a stimulant that delays sleep onset" is not a claim about this reader at
all: it is general knowledge, it is in a profile a clinician signed off, and it
is the part that actually helps someone understand what they are looking at.

So the card says both, and marks which is which:

    <what your records did>  This might be the reason.  In general: <fact>

The fact is retrieved by CODE, from the same Master Condition Profiles the
chat quotes. It is never generated, never paraphrased, and never invented: if
the corpus has nothing for a pair, the card simply carries no "in general"
sentence. A missing fact costs a line; a wrong one costs the reader's trust.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import McpChunk

logger = logging.getLogger(__name__)

#: What to look for, per pair. The terms are matched against the corpus text;
#: the sentence returned is the corpus's own, trimmed to one line.
#:
#: Deliberately narrow. A broad search finds a sentence about something else
#: and staples it to the card, which is worse than no sentence at all.
FACT_TERMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("coffee", "sleep_duration"): ("caffeine", "sleep"),
    ("coffee", "heart_rate"): ("caffeine", "heart rate"),
    ("alcohol", "sleep_duration"): ("alcohol", "sleep"),
    ("alcohol", "heart_rate_resting"): ("alcohol", "heart rate"),
    ("alcohol", "blood_pressure"): ("alcohol", "blood pressure"),
    ("smoking", "heart_rate_variability_sdnn"): ("smoking", "heart rate"),
    ("water_low", "mood"): ("hydration", "fatigue"),
}

_MAX_CHARS = 220


def _first_sentence(text: str, terms: tuple[str, ...]) -> str | None:
    """The first sentence mentioning the first term, trimmed. Corpus words only."""
    for raw in text.replace("\n", " ").split(". "):
        line = raw.strip(" .•-\t")
        if len(line) < 40 or len(line) > _MAX_CHARS:
            continue
        low = line.lower()
        if all(t.split()[0] in low for t in terms[:1]) and terms[-1].split()[0] in low:
            return line if line.endswith(".") else line + "."
    return None


async def fact_for(
    db: AsyncSession, exposure: str, outcome: str
) -> str | None:
    """A reviewed general sentence for this pair, or None. Never raises."""
    terms = FACT_TERMS.get((exposure, outcome))
    if not terms:
        return None
    try:
        rows = (
            await db.execute(
                select(McpChunk.content)
                .where(*[McpChunk.content.ilike(f"%{t}%") for t in terms])
                # Deterministic: the same pair must return the same sentence
                # every night, or the card would churn and supersede itself.
                .order_by(McpChunk.condition_code, McpChunk.id)
                .limit(8)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — a fact is a nicety, never the card
        logger.warning("pattern fact lookup failed", exc_info=True)
        return None

    for content in rows:
        found = _first_sentence(content or "", terms)
        if found:
            return found
    return None

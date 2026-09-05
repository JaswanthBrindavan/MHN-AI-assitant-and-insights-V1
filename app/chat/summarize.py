"""LLM prose summary alongside the deterministic compaction.

The existing compactor is regex- and table-driven, which makes it reproducible
and means it cannot hallucinate a fact into a summary. It also means it
destroys everything it has no extractor for: "my mother died of this last year
and I'm frightened" compacts to a topic code.

So this ADDS a short prose summary next to the structured one. It does not
replace it, and it is never authoritative:

* The structured dict stays the source of truth for every safety-relevant
  field — flags, medications, boundaries, timeline. Those come from the same
  triage vocabulary as the floor, and a model paraphrase must never sit
  between the floor and what the pipeline believes.
* The prose is context only: situation, feelings, what has already been
  explained. It is framed to the model as recollection, not record.
* A summarizer failure loses the prose and keeps the structure. Never the
  other way round.
"""

from __future__ import annotations

import logging

from app.chat.memory import STICKY_KEYS

logger = logging.getLogger("davi.memory")

MAX_SUMMARY_CHARS = 600

_SYSTEM = (
    "You are summarising part of a health conversation so it can be recalled "
    "later. Write at most four sentences of plain prose covering: what the "
    "reader is dealing with, anything about their situation or feelings that "
    "would change how you speak to them, and what has already been explained "
    "so it is not repeated.\n"
    "Rules:\n"
    "- Do NOT diagnose, and do not state anything as medically established.\n"
    "- Do NOT invent details. If something is unclear, leave it out.\n"
    "- Do NOT include numbers, doses or lab values — those are recorded "
    "separately and exactly.\n"
    "- Write about the reader in the third person.\n"
    "Return only the summary."
)


def _render_transcript(messages: list[dict], limit: int = 40) -> str:
    lines = []
    for m in messages[-limit:]:
        who = "User" if m.get("role") == "user" else "Ink"
        text = (m.get("message") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{who}: {text[:400]}")
    return "\n".join(lines)


async def summarize_prose(provider, messages: list[dict]) -> str | None:
    """A short prose summary of the folded turns, or None.

    Never raises: the caller keeps the deterministic summary either way.
    """
    transcript = _render_transcript(messages)
    if not transcript.strip():
        return None
    try:
        turn = await provider.generate_turn(
            system=_SYSTEM,
            messages=[_user(transcript)],
            tools=(),
        )
    except Exception:  # noqa: BLE001 — prose is a bonus, never a requirement
        logger.warning("prose compaction failed; keeping structure only",
                       exc_info=True)
        return None

    text = (turn.text or "").strip()
    if not text:
        return None
    return text[:MAX_SUMMARY_CHARS]


def _user(content: str):
    from app.llm.tools import UserMessage

    return UserMessage(content)


def merge_prose(summary: dict, prose: str | None) -> dict:
    """Attach prose to a structured summary without disturbing it.

    Returns a NEW dict. The structured keys are copied through untouched — if
    this function ever changed one, a model paraphrase would have edited the
    record the safety floor relies on.
    """
    merged = dict(summary)
    if prose:
        merged["narrative"] = prose
    return merged


def authoritative_keys() -> tuple[str, ...]:
    """The keys the prose must never influence.

    Named here so the invariant is testable rather than merely intended.
    """
    return STICKY_KEYS

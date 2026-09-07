"""Deterministic structured compaction extractors (no LLM).

Flags are detected with the SAME triage vocabulary as the safety floor (one
vocabulary). Sticky keys (flags, medications, boundaries, timeline) survive
every compaction pass; topics/open_questions are capped at CAP. Every key is
bounded — see STICKY_CAPS.
"""

from __future__ import annotations

import re

from app.rag.retrieval import extract_condition_codes
from app.triage.red_flags import triage

# Sticky keys survive every pass; capped keys hold at most CAP items.
STICKY_KEYS: tuple[str, ...] = ("flags", "medications", "boundaries", "timeline")
CAPPED_KEYS: tuple[str, ...] = ("topics", "open_questions")
CAP = 12

# "Sticky" means a later compaction pass never drops what an earlier one kept —
# a red flag from message 1 still stands at message 200, where a capped key
# would have rolled it off. It never meant unbounded. This dict is
# re-serialised into the prompt on every turn and is personal health
# information in a JSON column, and until these caps existed the surviving
# summary of a long session grew without limit (retention purges only the
# SUPERSEDED versions). So each sticky key has a ceiling, and which end
# survives follows what the key is for:
#
# * flags / timeline keep the EARLIEST entries — first mention is the thing
#   they record, and the triage vocabulary runs to ~1,000 phrases so dedup
#   alone bounds nothing.
# * medications / boundaries keep the LATEST — a dose change or the most
#   recent refusal is worth more than the one before it.
#
# Twenty-four medications covers real polypharmacy (an older reader on
# fifteen drugs is not unusual, and a test pins fifteen). boundaries are
# 120-character verbatim assistant refusals, so their cap is the tightest:
# four is ~120 tokens, twenty-four would be more than the retrieved
# knowledge gets.
STICKY_CAPS: dict[str, int] = {
    "flags": 24, "medications": 24, "boundaries": 4, "timeline": 24,
}
_KEEP_LATEST = frozenset({"medications", "boundaries"})

# drug + dose, e.g. "metformin 500 mg", "amlodipine 5 mg".
_MED_RE = re.compile(
    r"\b([a-z][a-z\-]{3,})\s+(\d+(?:\.\d+)?)\s?(mg|mcg|g|ml|units?|iu)\b",
    re.IGNORECASE,
)
# Words that look like a drug slot but are not (avoids "take 500 mg").
_MED_STOPWORDS = {
    "take", "took", "taking", "about", "above", "below", "around", "only",
    "just", "with", "have", "need", "been", "that", "this", "your", "from",
    "after", "before", "every", "other", "some", "when", "then", "than",
    "dose", "daily", "times", "into", "over", "under", "roughly",
}

# Phrases that mark an assistant refusal / decline / scope boundary.
_BOUNDARY_PHRASES = (
    "i can only help with health",
    "i can't help with that",
    "i cannot help with that",
    "i'm not a doctor",
    "i am not a doctor",
    "i don't diagnose",
    "i do not diagnose",
    "this is not a diagnosis",
    "please call your local emergency",
)


def extract_flags(text: str) -> list[str]:
    """Red-flag terms via the shared triage vocabulary."""
    return triage(text).matched_terms


def extract_medications(text: str) -> list[str]:
    meds: list[str] = []
    for name, dose, unit in _MED_RE.findall(text):
        if name.lower() in _MED_STOPWORDS:
            continue
        normalized = f"{name.lower()} {dose} {unit.lower()}"
        if normalized not in meds:
            meds.append(normalized)
    return meds


def is_boundary(assistant_text: str) -> bool:
    low = assistant_text.lower()
    return any(p in low for p in _BOUNDARY_PHRASES)


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _bound(key: str, items: list[str]) -> list[str]:
    """Dedup (first mention wins) and cap. Which end survives depends on the key."""
    cap = STICKY_CAPS.get(key, CAP)
    kept = _dedup(items)
    return kept[-cap:] if key in _KEEP_LATEST else kept[:cap]


def empty_summary() -> dict:
    return {k: [] for k in (*STICKY_KEYS, *CAPPED_KEYS)}


def compact_messages(messages: list[dict]) -> dict:
    """Fold a list of {role, message} dicts into a structured summary."""
    summary = empty_summary()
    for m in messages:
        text = m["message"]
        role = m["role"]

        new_flags = extract_flags(text)
        new_meds = extract_medications(text)
        new_topics = sorted(extract_condition_codes(text))

        for f in new_flags:
            if f not in summary["flags"]:
                summary["flags"].append(f)
        for md in new_meds:
            if md not in summary["medications"]:
                summary["medications"].append(md)
        for tp in new_topics:
            if tp not in summary["topics"]:
                summary["topics"].append(tp)
        # timeline: first-mention order across flags, meds, topics.
        for item in [*new_flags, *new_meds, *new_topics]:
            if item not in summary["timeline"]:
                summary["timeline"].append(item)

        if role == "assistant" and is_boundary(text):
            snippet = text.strip()[:120]
            if snippet not in summary["boundaries"]:
                summary["boundaries"].append(snippet)
        if role == "user" and text.strip().endswith("?"):
            q = text.strip()[:120]
            if q not in summary["open_questions"]:
                summary["open_questions"].append(q)

    for k in (*STICKY_KEYS, *CAPPED_KEYS):
        summary[k] = _bound(k, summary[k])
    return summary


def merge_summaries(old: dict, new: dict) -> dict:
    """Union old and new per key, then bound every key (see STICKY_CAPS).

    Old comes first, so for keep-earliest keys the merge is monotone: what an
    earlier pass kept, a later pass keeps.
    """
    return {
        k: _bound(k, [*(old.get(k) or []), *(new.get(k) or [])])
        for k in (*STICKY_KEYS, *CAPPED_KEYS)
    }

"""Deterministic structured compaction extractors (no LLM).

Flags are detected with the SAME triage vocabulary as the safety floor (one
vocabulary). Sticky keys (flags, medications, boundaries, timeline) merge
without truncation and survive every pass; topics/open_questions are capped.
"""

from __future__ import annotations

import re

from app.rag.retrieval import extract_condition_codes
from app.triage.red_flags import triage

# Sticky keys never truncate; capped keys hold at most CAP items.
STICKY_KEYS: tuple[str, ...] = ("flags", "medications", "boundaries", "timeline")
CAPPED_KEYS: tuple[str, ...] = ("topics", "open_questions")
CAP = 12

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
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


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

    summary["topics"] = summary["topics"][:CAP]
    summary["open_questions"] = summary["open_questions"][:CAP]
    return summary


def merge_summaries(old: dict, new: dict) -> dict:
    """Sticky keys merge without truncation; capped keys hold at most CAP."""
    merged: dict = {}
    for k in STICKY_KEYS:
        merged[k] = _dedup([*(old.get(k) or []), *(new.get(k) or [])])
    for k in CAPPED_KEYS:
        merged[k] = _dedup([*(old.get(k) or []), *(new.get(k) or [])])[:CAP]
    return merged

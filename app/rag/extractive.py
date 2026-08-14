"""Deterministic extractive answers from retrieved corpus chunks.

Used when no live model is configured (LLM_PROVIDER=fake): instead of one
canned line for every question, the reply is composed verbatim from the
clinically-validated chunks that retrieval selected — different question,
different content. Pure formatting; no generation, no interpretation.
"""

from __future__ import annotations

import re

from app.rag.retrieval import RetrievedChunk

_MAX_CHUNKS = 3
_MAX_LINES_PER_CHUNK = 5
_MAX_LINE_CHARS = 260

_SECTION_TITLES = {
    "definition": "What it is",
    "prevalence": "How common it is",
    "diagnosis": "How it is diagnosed",
    "classification": "Types",
    "etiology": "What contributes to it",
    "risk_profiles": "Who tends to be at risk",
    "lifestyle_influence": "Lifestyle factors",
    "lifestyle_triggers": "Lifestyle thresholds",
    "tests_quantitative": "Common tests",
    "tests_qualitative": "Clinical examination",
    "symptoms": "Common symptoms",
    "signs": "Clinical signs",
    "complications": "Possible complications",
    "associated_conditions": "Often seen alongside",
    "suggestions": "What generally helps",
}


def _base_section(chunk_type: str) -> str:
    return chunk_type.rsplit("_", 1)[0] if chunk_type[-1:].isdigit() else chunk_type


def _clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[•\-\s]+", "", line)
    if len(line) > _MAX_LINE_CHARS:
        cut = line[:_MAX_LINE_CHARS]
        cut = cut.rsplit(" ", 1)[0]
        line = cut + "…"
    return line


def build_extractive_answer(chunks: list[RetrievedChunk]) -> str | None:
    """Compose a readable answer from the top retrieved chunks (verbatim)."""
    if not chunks:
        return None

    parts: list[str] = []
    seen_sections: set[tuple[str, str]] = set()
    display_names: list[str] = []

    for chunk in chunks[:_MAX_CHUNKS]:
        header, _, body = chunk.content.partition("\n")
        # Header shape: "<Display name> — <section words>:"
        display = header.split("—")[0].strip().rstrip(":")
        if display and display not in display_names:
            display_names.append(display)
        section = _base_section(chunk.chunk_type)
        key = (display, section)
        if key in seen_sections:
            continue
        seen_sections.add(key)

        title = _SECTION_TITLES.get(section, section.replace("_", " ").title())
        lines = [
            _clean_line(ln) for ln in body.split("\n") if len(ln.strip()) > 15
        ][:_MAX_LINES_PER_CHUNK]
        if not lines:
            continue
        parts.append(
            f"**{title} — {display}**\n" + "\n".join(f"• {ln}" for ln in lines)
        )

    if not parts:
        return None

    lead = (
        "From our clinically reviewed profile"
        + ("s" if len(display_names) > 1 else "")
        + f" ({', '.join(display_names[:3])}):"
    )
    tail = (
        "This is general, educational information — not a diagnosis or a "
        "personal recommendation. A doctor can interpret what it means for you."
    )
    return lead + "\n\n" + "\n\n".join(parts) + "\n\n" + tail

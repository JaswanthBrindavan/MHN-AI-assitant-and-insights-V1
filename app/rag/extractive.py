"""Deterministic extractive answers from retrieved corpus chunks.

Used when no live model is configured (LLM_PROVIDER=fake): instead of one
canned line for every question, the reply is composed verbatim from the
clinically-validated chunks that retrieval selected — different question,
different content. Pure formatting; no generation, no interpretation.
"""

from __future__ import annotations

import re

from app.rag.retrieval import RetrievedChunk, _base_section

# Question shapes whose answer IS a corpus section (definition, symptoms,
# diagnosis, causes/heredity, complications, prevalence, types, prevention).
# These are served extractively from the validated profiles — no LLM — when
# the message names a condition. Anything needing composition ("how does X
# affect daily life", "is X curable") deliberately stays with the model.
_DEFINITIONAL_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bwhat (?:is|are)\b|\bdefine\b|\bmeaning of\b|\btell me about\b"
    r"|\bexplain\b",
    r"\bsymptoms?\b|\bsigns?\b|\bhow do i know if\b"
    r"|\bhow to (?:identify|recogni[sz]e)\b",
    r"\bdiagnos|\btests? (?:for|to)\b|\bscreening\b|\bdetect",
    r"\brun in famil|\bhereditary\b|\bgenetic\b|\binherit"
    r"|\bfamily history of\b|\bcauses?\b|\bwhy do people get\b"
    r"|\brisk factors?\b",
    r"\bcomplications?\b|\bwhat happens if\b|\bleft untreated\b",
    r"\bhow common\b|\bprevalence\b|\bhow many people\b",
    r"\btypes? of\b|\bstages? of\b|\bkinds? of\b",
    r"\bprevent|\bavoid\b|\breduce (?:the )?risk\b",
))


def is_definitional_ask(message: str) -> bool:
    """True when the question's answer is a profile section verbatim."""
    return any(p.search(message) for p in _DEFINITIONAL_RES)

_MAX_CHUNKS = 3
_MAX_LINES_PER_CHUNK = 5
# 260 cut MC001's 370-char definition body mid-word ('...aka "Diabetes...').
# Raised for the FOCUSED path only: that answer carries one section instead of
# three, so a longer line still nets much shorter. The unfocused path keeps 260
# — raising it there made the very answers this change set out to shorten about
# 38% LONGER, since several question shapes `is_definitional_ask` serves map to
# no section and stay unfocused.
_MAX_LINE_CHARS = 260
_MAX_LINE_CHARS_FOCUSED = 400

# A focused answer renders ONE block per distinct section that was asked for,
# not one block total. Three sections at 1,432 chars for "what is diabetes" —
# with the definition landing third — was the reported defect, and a single
# section answers it. But a hard 1 also sliced away the second half of a
# compound question ("symptoms AND complications"), which retrieval had
# deliberately kept. The dedupe in `_selected` still collapses continuation
# parts, so this is a ceiling, not a quota.
_MAX_CHUNKS_FOCUSED = 1
_MAX_SECTIONS_FOCUSED = 3

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


def _clean_line(line: str, max_chars: int = _MAX_LINE_CHARS) -> str:
    line = line.strip()
    line = re.sub(r"^[•\-\s]+", "", line)
    if len(line) > max_chars:
        cut = line[:max_chars]
        cut = cut.rsplit(" ", 1)[0]
        line = cut + "…"
    return line


# What the menu may offer, in the order a reader tends to want it. Deliberately
# NOT derived from the retrieved chunks: retrieval now filters to the asked-for
# section, so the retrieved set contains only what was just rendered. Every
# Master Condition Profile is generated from one template carrying these
# sections, and a follow-up naming one the profile happens to lack still
# fail-opens to the unfiltered ranking rather than returning nothing.
#
# The PHRASE matters as much as the section. A menu that offers "what
# contributes to it" invites a follow-up the section router does not recognise,
# which fail-opens to the unfiltered three-chunk answer — precisely the reply
# this change exists to eliminate. Every phrase below is worded to contain a
# stem from `_SECTION_INTENT`, and `test_every_menu_phrase_routes_back` pins
# that: echo any menu item and you get that section.
_MENU_SECTIONS: tuple[tuple[str, str], ...] = (
    ("definition", "what it is"),
    ("symptoms", "the symptoms"),
    ("diagnosis", "how it is diagnosed"),
    ("etiology", "what causes it"),
    ("complications", "the complications"),
    ("suggestions", "what helps manage it"),
)


def is_focused(chunks: list[RetrievedChunk], sections: tuple[str, ...]) -> bool:
    """True when a section was asked for AND retrieval actually produced it.

    `_prefer_section` fails open — a profile lacking the asked-for section keeps
    the unfiltered ranking — so `bool(sections)` alone would render the top
    unrelated chunk as a confident single-section answer. Focus only when the
    filter really matched.
    """
    if not sections:
        return False
    return any(
        _base_section(c.chunk_type) in sections or c.chunk_type in sections
        for c in chunks
    )


def disclosure_menu(rendered: set[str]) -> str | None:
    """One line naming sections NOT shown, so a reader can ask for them.

    This is the whole of progressive disclosure. The follow-up it invites
    ("what are the symptoms") is an ordinary section-targeted question that
    retrieval now answers correctly, so it needs no new state, no new table and
    no turn-to-turn bookkeeping.
    """
    others = [phrase for section, phrase in _MENU_SECTIONS if section not in rendered]
    if not others:
        return None
    listed = others[:5]
    return (
        "I can also cover "
        + ", ".join(listed[:-1])
        + (" and " if len(listed) > 1 else "")
        + listed[-1]
        + " — just ask."
    )


def _focused_limit(chunks: list[RetrievedChunk]) -> int:
    """One slot per distinct section present, capped.

    A flat 1 answered "what is diabetes" correctly but truncated "symptoms and
    complications of diabetes" to symptoms alone, discarding what the section
    filter had deliberately kept.
    """
    distinct = len({_base_section(c.chunk_type) for c in chunks})
    return max(_MAX_CHUNKS_FOCUSED, min(distinct, _MAX_SECTIONS_FOCUSED))


def _selected(
    chunks: list[RetrievedChunk], focused: bool
) -> list[tuple[RetrievedChunk, str, str, list[str]]]:
    """The chunks that will actually be RENDERED, with their prepared lines.

    Selection used to happen inline in the renderer while the caller built its
    citation list from a separate `chunks[:3]` slice. The two drifted: the
    renderer drops a chunk whose base section is already shown, and drops one
    whose body yields no usable lines, both AFTER the slice — so a citation
    could point at a block the reader never saw. One pass, one answer.
    """
    limit = _focused_limit(chunks) if focused else _MAX_CHUNKS
    line_budget = _MAX_LINE_CHARS_FOCUSED if focused else _MAX_LINE_CHARS
    seen: set[tuple[str, str]] = set()
    out: list[tuple[RetrievedChunk, str, str, list[str]]] = []
    for chunk in chunks[:limit]:
        header, _, body = chunk.content.partition("\n")
        # Header shape: "<Display name> - <section words>:"
        display = header.split("\u2014")[0].strip().rstrip(":")
        section = _base_section(chunk.chunk_type)
        key = (display, section)
        if key in seen:
            continue
        lines = [
            _clean_line(ln, line_budget)
            for ln in body.split("\n") if len(ln.strip()) > 15
        ][:_MAX_LINES_PER_CHUNK]
        if not lines:
            continue
        seen.add(key)
        out.append((chunk, display, section, lines))
    return out


def rendered_chunks(
    chunks: list[RetrievedChunk], *, focused: bool = False
) -> list[RetrievedChunk]:
    """Public: exactly the chunks `build_extractive_answer` will render.

    Build the citation list from THIS, never from a parallel slice.
    """
    return [c for c, _d, _s, _l in _selected(chunks, focused)]


def build_extractive_answer(
    chunks: list[RetrievedChunk],
    *,
    focused: bool = False,
    with_menu: bool = False,
) -> str | None:
    """Compose a readable answer from the top retrieved chunks (verbatim).

    ``focused`` - the question named a section AND retrieval produced it, so
    render one block instead of three (see `is_focused`). ``with_menu`` -
    append the disclosure line. The caller passes ``with_menu`` only at
    ``risk == NONE``: the HIGH escalation banner is prepended to whatever this
    returns, and inviting a reader to browse the corpus underneath an
    urgent-care instruction would undercut it.
    """
    if not chunks:
        return None

    selected = _selected(chunks, focused)
    parts: list[str] = []
    rendered_sections: set[str] = set()
    display_names: list[str] = []

    for _chunk, display, section, lines in selected:
        if display and display not in display_names:
            display_names.append(display)
        rendered_sections.add(section)
        title = _SECTION_TITLES.get(section, section.replace("_", " ").title())
        parts.append(
            f"**{title} \u2014 {display}**\n" + "\n".join(f"\u2022 {ln}" for ln in lines)
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
    body = lead + "\n\n" + "\n\n".join(parts)
    if with_menu:
        menu = disclosure_menu(rendered_sections)
        if menu:
            body += "\n\n" + menu
    return body + "\n\n" + tail

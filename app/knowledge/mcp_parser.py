"""Parser for MHN Master Condition Profile (MCP) docx files.

The corpus is uniform: a title paragraph, an "AKA i.e., ..." alias line, then
20 section-marker paragraphs (plain style, matched by text) with content
paragraphs and data tables following each marker in document order.

Output is a structured dict plus flattened RAG chunks. The parser is
deterministic and offline; it never rewrites clinical content — it only
re-arranges the validated text into retrieval-sized chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

# Section markers (lowercased, matched by startswith on stripped text).
# Order matters only for readability; matching is by lookup.
SECTION_MARKERS: dict[str, str] = {
    "introduction to mcp": "_intro_header",
    "definition": "definition",
    "prevalence": "prevalence",
    "diagnosis": "diagnosis",
    "classification": "classification",
    "etiology": "etiology",
    "research draft of mcp": "_research_header",
    "profiles prone to": "risk_profiles",
    "lifestyle health parameters": "lifestyle_influence",
    "lhp metric triggers": "lifestyle_triggers",
    "traditional health parameters": "_thp_header",
    "quantitative thp": "tests_quantitative",
    "qualitative thp": "tests_qualitative",
    "clinical features": "_clinical_header",
    "symptoms": "symptoms",
    "signs": "signs",
    "concomitant conditions": "_concomitant_header",
    "complications": "complications",
    "associated medical conditions": "associated_conditions",
    "suggestions": "suggestions",
}

# Headers that only group other sections and hold no content of their own.
_STRUCTURAL = {v for v in SECTION_MARKERS.values() if v.startswith("_")}

# Maximum characters per emitted chunk; longer sections are split on row/
# paragraph boundaries.
MAX_CHUNK_CHARS = 1800

# No trailing \b: underscores are word chars, so "MC305_Alcohol..." would
# otherwise fail to match. A leading boundary + digit run is sufficient.
_MC_CODE_RE = re.compile(r"\b(MC\d{3,4})(?=\D|$)")


@dataclass
class ParsedMcp:
    code: str
    display_name: str
    aliases: list[str]
    sections: dict[str, list[str]] = field(default_factory=dict)
    source_file: str = ""


def extract_code_from_filename(filename: str) -> str | None:
    m = _MC_CODE_RE.search(filename)
    return m.group(1) if m else None


# Parenthetical inners that are commentary rather than usable abbreviations.
_PAREN_JUNK_RE = re.compile(
    r"[+/]|(\b(?:lay|terms?|clinical|ayurvedic|form|hindi|tamil|marathi|telugu|"
    r"kannada|bengali|urdu|colloquial|historical|regional|formerly|archaic|"
    r"informal|slang)\b)",
    re.IGNORECASE,
)

# Alias fragments that are enumeration/commentary debris, not condition names:
# articles, connectives, language/register labels, "formerly ..." notes.
_ALIAS_JUNK_PREFIX_RE = re.compile(
    r"^(?:a|an|the|or|and|also|when|with|without|formerly|previously|"
    r"historically?|colloquial(?:ly)?|informal(?:ly)?|lay(?:\s+terms?)?|"
    r"hindi|tamil|marathi|telugu|kannada|bengali|urdu|e\.?g|i\.?e|etc)\b",
    re.IGNORECASE,
)


def _is_junk_alias(fragment: str) -> bool:
    """Reject enumeration debris so it never becomes a scoping keyword."""
    if _ALIAS_JUNK_PREFIX_RE.match(fragment):
        return True
    # Unbalanced parentheses/quotes → a fragment split mid-parenthetical.
    if fragment.count("(") != fragment.count(")"):
        return True
    if fragment.count('"') % 2 or (fragment.count("“") != fragment.count("”")):
        return True
    return False


def parse_aka_line(line: str) -> list[str]:
    """Parse an AKA alias line into individual aliases.

    Real corpus lines separate aliases with commas AND semicolons, wrap lay
    terms in curly quotes, and carry abbreviations in parentheses
    ("Polycystic ovary syndrome (PCOS)"). Each parenthesised abbreviation is
    ALSO emitted as its own alias, along with the paren-free base.
    """
    text = line.strip()
    text = re.sub(r"^aka[\s:]*((i\.?e\.?)[.,\s]*)?", "", text, flags=re.IGNORECASE)

    aliases: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        candidate = candidate.strip().strip(".").strip("\"'“”‘’").strip()
        if candidate and candidate.lower() not in seen:
            seen.add(candidate.lower())
            aliases.append(candidate)

    for part in re.split(r"[,;]", text):
        part = part.strip()
        if not part or _is_junk_alias(part):
            continue
        _add(part)
        # "Name (ABBR)" → also "Name" and "ABBR" (skipping commentary parens).
        inners = re.findall(r"\(([^)]{2,40})\)", part)
        base = re.sub(r"\s*\([^)]*\)", "", part).strip()
        if base and base != part:
            _add(base)
        for inner in inners:
            inner = inner.strip()
            # Inners face BOTH junk filters: "(the eggs)" and "(Hindi)" are
            # commentary, not names.
            if not _PAREN_JUNK_RE.search(inner) and not _is_junk_alias(inner):
                _add(inner)
    return aliases


# Real section-marker paragraphs are short headings ("Lifestyle Health
# Parameters ( LHP )" = 35 chars). Prose that merely BEGINS with a marker word
# ("Diagnosis of acute pancreatitis follows the 2012 Revised Atlanta…",
# "Clinical features alone are unreliable…") must not be treated as a marker —
# that silently deleted diagnosis content in 8 corpus files.
MAX_MARKER_LEN = 45


def _match_marker(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) > MAX_MARKER_LEN:
        return None
    low = stripped.lower()
    for marker, section in SECTION_MARKERS.items():
        if low.startswith(marker):
            return section
    return None


def _table_to_lines(table: Table) -> list[str]:
    """Flatten a table into 'Header1: cell; Header2: cell' lines per data row."""
    rows = table.rows
    if not rows:
        return []
    headers = [c.text.strip() for c in rows[0].cells]
    lines: list[str] = []
    for row in rows[1:]:
        cells = [c.text.strip() for c in row.cells]
        # Merged cells repeat; collapse duplicates while preserving pairing.
        parts: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for header, cell in zip(headers, cells, strict=False):
            if not cell:
                continue
            key = (header, cell)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            cell_flat = re.sub(r"\s*\n\s*", " / ", cell)
            parts.append(f"{header}: {cell_flat}" if header else cell_flat)
        if parts:
            lines.append("; ".join(parts))
    return lines


def parse_mcp_docx(path: str | Path) -> ParsedMcp:
    """Parse one MCP docx into structured sections (document-order walk)."""
    path = Path(path)
    doc = Document(str(path))

    code = extract_code_from_filename(path.name) or "UNKNOWN"
    display_name = ""
    aliases: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    preamble_done = False

    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, doc).text.strip()
            if not text:
                continue
            if not display_name:
                display_name = text
                continue
            if not preamble_done and text.lower().startswith("aka"):
                aliases = parse_aka_line(text)
                continue
            section = _match_marker(text)
            if section is not None:
                preamble_done = True
                current = None if section in _STRUCTURAL else section
                if current is not None:
                    sections.setdefault(current, [])
                continue
            if current is not None:
                sections[current].append(text)
        elif child.tag.endswith("}tbl"):
            if current is not None:
                sections[current].extend(_table_to_lines(Table(child, doc)))

    return ParsedMcp(
        code=code,
        display_name=display_name,
        aliases=aliases,
        sections=sections,
        source_file=path.name,
    )


def _split_long_line(line: str, budget: int) -> list[str]:
    """Split an over-budget line at sentence boundaries (best effort)."""
    if len(line) <= budget:
        return [line]
    pieces: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?])\s+", line):
        if buf and len(buf) + len(sentence) + 1 > budget:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        pieces.append(buf)
    return pieces


def _split_to_chunks(header: str, lines: list[str]) -> list[str]:
    """Pack lines into chunks of at most MAX_CHUNK_CHARS (header included)."""
    budget = MAX_CHUNK_CHARS - len(header) - 1
    chunks: list[str] = []
    buf: list[str] = []
    size = len(header)
    for raw_line in lines:
        for line in _split_long_line(raw_line, budget):
            line_len = len(line) + 1
            if buf and size + line_len > MAX_CHUNK_CHARS:
                chunks.append(header + "\n" + "\n".join(buf))
                buf, size = [], len(header)
            buf.append(line)
            size += line_len
    if buf:
        chunks.append(header + "\n" + "\n".join(buf))
    return chunks


def build_chunks(parsed: ParsedMcp) -> list[dict]:
    """Flatten parsed sections into mcp_chunks rows (dicts)."""
    out: list[dict] = []
    for section, lines in parsed.sections.items():
        content_lines = [ln for ln in lines if ln.strip()]
        if not content_lines:
            continue
        header = f"{parsed.display_name} — {section.replace('_', ' ')}:"
        for i, content in enumerate(_split_to_chunks(header, content_lines)):
            out.append(
                {
                    "condition_code": parsed.code,
                    "chunk_type": section if i == 0 else f"{section}_{i + 1}",
                    "content": content,
                    "metadata": {
                        "source": "mcp_master_profile",
                        "source_file": parsed.source_file,
                        "display_name": parsed.display_name,
                        "section": section,
                        "part": i + 1,
                    },
                }
            )
    return out

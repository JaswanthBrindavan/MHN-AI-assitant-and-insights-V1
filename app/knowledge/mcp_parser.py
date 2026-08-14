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


def parse_aka_line(line: str) -> list[str]:
    """Parse 'AKA i.e., A, B, C' → ['A', 'B', 'C'] (order kept, deduped)."""
    text = line.strip()
    text = re.sub(r"^aka[\s:]*((i\.?e\.?)[.,\s]*)?", "", text, flags=re.IGNORECASE)
    aliases: list[str] = []
    for part in text.split(","):
        alias = part.strip().strip(".").strip()
        if alias and alias.lower() not in {a.lower() for a in aliases}:
            aliases.append(alias)
    return aliases


def _match_marker(text: str) -> str | None:
    low = text.strip().lower()
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


def _split_to_chunks(header: str, lines: list[str]) -> list[str]:
    """Pack lines into chunks of at most MAX_CHUNK_CHARS (header included)."""
    chunks: list[str] = []
    buf: list[str] = []
    size = len(header)
    for line in lines:
        line_len = len(line) + 1
        if buf and size + line_len > MAX_CHUNK_CHARS:
            chunks.append(header + "\n" + "\n".join(buf))
            buf, size = [], len(header)
        # A single line longer than the cap still becomes its own chunk.
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

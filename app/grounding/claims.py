"""Mechanical claim grounding — pure, standard-library only.

The system prompt requires every sentence containing a clinical value,
threshold, or dose to END with a citation marker:
  * ``[n]`` — retrieved chunk n
  * ``[P]`` — the patient-context block
  * ``[GK]`` — general knowledge, allowed ONLY when nothing was retrieved

This module parses markers, verifies cited ⊆ provided, and flags factual
sentences that carry no marker. It never calls an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARKER_RE = re.compile(r"\[(\d+|P|GK)\]")

# A sentence is "factual" if it states a clinical value/threshold/dose.
_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:mg/dl|mmhg|mmol/l|mcg|mg|g|ml|iu|%|bpm|kg|"
    r"hours?|hrs?|days?|weeks?|times a day|per day)\b",
    re.IGNORECASE,
)
_THRESHOLD_RE = re.compile(
    r"\b(?:above|below|over|under|less than|greater than|more than|at least|"
    r"no more than|higher than|lower than)\s+\d",
    re.IGNORECASE,
)


@dataclass
class GroundingReport:
    status: str  # "grounded" | "violations"
    violations: list[dict] = field(default_factory=list)
    factual_count: int = 0
    cited: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "violations": self.violations,
            "factual_count": self.factual_count,
            "cited": self.cited,
        }


# --------------------------------------------------------------------------- #
# Clinical assertions that carry no numbers
# --------------------------------------------------------------------------- #
# A sentence with no digits in it was never grounding-checked: is_factual only
# matched units and thresholds. So "you can stop taking it once you feel
# better" — the single most dangerous thing this product could say — passed
# unexamined, because the numeric guards have nothing to look at.
#
# Matched on grammatical SHAPE, not a phrase blocklist. A blocklist is the same
# treadmill this codebase is trying to leave behind: every new phrasing would
# be a new code change.

# Directives aimed at the reader about medication or care.
_DIRECTIVE_RE = re.compile(
    r"\byou (?:can|could|should|shouldn't|should not|need to|must|ought to|"
    r"may|might want to|don't need to|do not need to)\b"
    r"[^.?!]{0,60}?"
    r"\b(?:stop|start|skip|halve|double|increase|reduce|lower|raise|change|"
    r"switch|take|takes|taking|continue|discontinue|avoid|wait|delay)\b",
    re.IGNORECASE,
)

# Prognostic claims — what will happen, stated as fact.
_PROGNOSTIC_RE = re.compile(
    r"\b(?:usually|normally|typically|generally|always|will)\s+"
    r"(?:resolve|resolves|clear|clears|improve|improves|settle|settles|"
    r"go away|goes away|pass|passes|heal|heals)\b"
    r"|\bis (?:harmless|nothing to worry about|not serious|no cause for concern)\b"
    r"|\b(?:resolves|clears|goes away|settles) on (?:its|their) own\b",
    re.IGNORECASE,
)

# Routing the reader to care is ALWAYS safe and must never be flagged —
# blocking it would be the exact opposite of this product's purpose.
_SAFE_DIRECTION_RE = re.compile(
    r"\b(?:doctor|clinician|pharmacist|prescriber|physician|nurse|"
    r"emergency|hospital|urgent care|specialist|professional)\b",
    re.IGNORECASE,
)


def assertion_kind(sentence: str) -> str | None:
    """Classify a sentence as a clinical assertion needing a citation.

    Returns "directive", "prognostic", or None.
    """
    if _SAFE_DIRECTION_RE.search(sentence):
        return None
    if _DIRECTIVE_RE.search(sentence):
        return "directive"
    if _PROGNOSTIC_RE.search(sentence):
        return "prognostic"
    return None


def is_factual(sentence: str) -> bool:
    """True when a sentence makes a claim that requires a citation.

    Numeric claims (values, thresholds, doses) AND non-numeric clinical
    assertions (directives about medication, prognoses stated as fact).
    """
    return bool(
        _UNIT_RE.search(sentence)
        or _THRESHOLD_RE.search(sentence)
        or assertion_kind(sentence)
    )


def _normalize(answer: str) -> str:
    """Pull a marker that trails a sentence terminator back inside the sentence.

    Turns "... is high. [1]" into "... is high [1]." so per-sentence marker
    detection is robust to either placement.
    """
    return re.sub(r"([.!?])\s*(\[(?:\d+|P|GK)\])", r" \2\1", answer)


def _sentences(answer: str) -> list[str]:
    normalized = _normalize(answer)
    parts = re.split(r"(?<=[.!?])\s+", normalized.strip())
    return [p for p in parts if p.strip()]


def strip_markers(answer: str) -> str:
    """Remove all citation markers for display, tidying whitespace."""
    stripped = MARKER_RE.sub("", answer)
    stripped = re.sub(r"\s+([.,;:!?])", r"\1", stripped)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    return stripped.strip()


def analyze_grounding(
    answer: str,
    *,
    num_chunks: int,
    has_patient_context: bool,
    retrieval_happened: bool,
) -> GroundingReport:
    """Verify citations and flag ungrounded factual sentences."""
    provided = {str(i) for i in range(1, num_chunks + 1)}
    if has_patient_context:
        provided.add("P")

    violations: list[dict] = []
    cited_all: set[str] = set()
    factual_count = 0

    for sentence in _sentences(answer):
        markers = MARKER_RE.findall(sentence)
        cited_all.update(markers)
        factual = is_factual(sentence)
        if factual:
            factual_count += 1

        for marker in markers:
            if marker == "GK":
                # [GK] is only legitimate when nothing was retrieved.
                if retrieval_happened:
                    violations.append(
                        {"type": "gk_not_allowed", "sentence": sentence.strip()}
                    )
            elif marker not in provided:
                violations.append(
                    {
                        "type": "invalid_marker",
                        "marker": marker,
                        "sentence": sentence.strip(),
                    }
                )

        if factual and not markers:
            violations.append(
                {"type": "ungrounded_claim", "sentence": sentence.strip()}
            )

    status = "violations" if violations else "grounded"
    return GroundingReport(
        status=status,
        violations=violations,
        factual_count=factual_count,
        cited=sorted(cited_all),
    )

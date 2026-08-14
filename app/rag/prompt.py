"""System-prompt assembly for the RAG answer.

DRAFT — pending clinician sign-off. The grounding instructions here are the
contract the mechanical verifier in app/grounding/claims.py enforces.
"""

from __future__ import annotations

from app.rag.retrieval import RetrievedChunk

_SAFETY_RULES = (
    "You are Davi, a careful health assistant offering general, educational "
    "information and decision support. You are NOT a doctor and you never "
    "diagnose. Never tell the user they have a condition, never give disease "
    "probabilities as numbers, and never say a medication is causing a symptom. "
    "If a reply touches medication, remind the reader not to stop or change a "
    "dose on their own and to discuss it with the prescriber."
)

_GROUNDING_RULES = (
    "Grounding rules: every sentence that states a clinical value, threshold, or "
    "dose MUST end with a citation marker. Use [n] to cite retrieved block n, "
    "[P] to cite the patient-context block, and [GK] ONLY when nothing was "
    "retrieved and the question is general information. Do not invent block "
    "numbers. Keep the answer brief and plain-English."
)


def format_chunks(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return ""
    return "\n".join(f"[{i}] {c.content}" for i, c in enumerate(chunks, start=1))


def build_system_prompt(
    chunks: list[RetrievedChunk],
    patient_context: str,
) -> str:
    parts = [_SAFETY_RULES, _GROUNDING_RULES]
    if chunks:
        parts.append("Retrieved knowledge blocks:\n" + format_chunks(chunks))
    else:
        parts.append(
            "No knowledge blocks were retrieved. If you answer, use [GK] markers "
            "for any general-information claims."
        )
    if patient_context:
        parts.append("Patient context block [P]:\n" + patient_context)
    return "\n\n".join(parts)


def build_correction_directive(violations: list[dict]) -> str:
    """A single corrective instruction naming the grounding violations."""
    kinds = sorted({v["type"] for v in violations})
    return (
        "Your previous answer failed grounding checks: "
        + ", ".join(kinds)
        + ". Rewrite it so every clinical value, threshold, or dose ends with a "
        "valid citation marker ([n]/[P], or [GK] only if nothing was retrieved), "
        "citing only blocks that exist. Do not add new facts."
    )

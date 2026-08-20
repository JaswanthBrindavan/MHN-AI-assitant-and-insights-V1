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
    "dose on their own and to discuss it with the prescriber. "
    "If asked what model, AI, or technology you are, or who built you, say only "
    "that you are Davi, the health assistant — never name any underlying AI "
    "model, provider, or company."
)

_GROUNDING_RULES = (
    "Grounding rules: every sentence that states a clinical value, threshold, or "
    "dose MUST end with a citation marker. Use [n] to cite retrieved block n, "
    "[P] to cite the patient-context block, and [GK] ONLY when nothing was "
    "retrieved and the question is general information. Do not invent block "
    "numbers. Keep the answer brief and plain-English."
)

# When the [P] block carries the reader's OWN recorded data (lifestyle, vitals,
# medications), personalize — but stay strictly within decision support.
_PERSONALIZATION_RULES = (
    "Personalization: if the Patient context [P] block includes the reader's own "
    "recorded data (lifestyle, vitals, or medications) and they are asking about "
    "a symptom or how they feel, connect the educational information to their "
    "recorded data. Point out which of THEIR recorded factors are commonly "
    "relevant to what they describe (for example logged caffeine or alcohol, a "
    "recorded blood-pressure or blood-sugar value, or a listed medication), and "
    "suggest what to raise with their doctor. Cite each personal fact you use "
    "with [P]. Strict limits: these are possibilities to discuss, never a "
    "diagnosis; do NOT say any recorded value or medication IS the cause of the "
    "symptom; do NOT tell the reader to change or stop a medication — only to "
    "discuss it with the prescriber. Whenever you name ANY of the reader's "
    "medications, you MUST include, in the same reply, the reminder that they "
    "should not change or stop the dose on their own and should discuss it with "
    "their prescriber — this is required every time a medication is mentioned."
)


def format_chunks(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return ""
    return "\n".join(f"[{i}] {c.content}" for i, c in enumerate(chunks, start=1))


def format_recent_turns(turns: list[dict]) -> str:
    """Render the last few verbatim turns for follow-up resolution."""
    lines = []
    for t in turns:
        who = "User" if t.get("role") == "user" else "Davi"
        text = (t.get("message") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{who}: {text[:400]}")
    return "\n".join(lines)


def build_system_prompt(
    chunks: list[RetrievedChunk],
    patient_context: str,
    compacted_context_json: str | None = None,
    recent_turns: list[dict] | None = None,
) -> str:
    parts = [_SAFETY_RULES, _GROUNDING_RULES]
    # Only spend the personalization budget when personal data is actually
    # present in [P] (the snapshot line is unmistakable).
    if patient_context and "own recorded data" in patient_context:
        parts.append(_PERSONALIZATION_RULES)
    # Recent verbatim turns let the model resolve follow-ups ("is it serious?",
    # "what about for children?") and refer back to its own earlier answers.
    if recent_turns:
        rendered = format_recent_turns(recent_turns)
        if rendered:
            parts.append(
                "Recent conversation so far (context for follow-up questions; "
                "the user's latest message is answered below):\n" + rendered
                + "\n\nIf the latest message is short or a fragment, it is very "
                "likely a follow-up or a direct answer to your own previous "
                "question — interpret it in that context and continue the same "
                "thread rather than treating it as a brand-new topic. Resolve "
                "pronouns like 'it'/'that' from the recent turns."
            )
    if compacted_context_json:
        parts.append("COMPACTED_CONTEXT_JSON:\n" + compacted_context_json)
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

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
        # The compacted summary lists topics/phrases DISCUSSED earlier in the
        # conversation — including hypotheticals, questions about relatives,
        # and this-session tests. Without this framing the model has presented
        # discussed conditions as the reader's own medical history.
        parts.append(
            "COMPACTED_CONTEXT_JSON (topics, flags, and phrases mentioned "
            "earlier in this conversation — NOT the reader's medical record; "
            "a condition appearing here means it was discussed, not that the "
            "reader has it; never present these as the reader's own "
            "conditions or history):\n" + compacted_context_json
        )
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


# --------------------------------------------------------------------------- #
# Agentic engine
# --------------------------------------------------------------------------- #
_TOOL_RULES = (
    "You have tools that read the reader's OWN health records. Use them "
    "whenever the answer depends on their data — a lab value, a document, a "
    "tracked habit, a family member's shared report. Never state a number a "
    "tool did not return, and never guess at a value you could look up.\n"
    "When a tool returns a `deterministic_reply`, that wording has already "
    "been safety-checked: prefer it verbatim when it answers the question on "
    "its own. Write your own wording only when you need to COMBINE facts from "
    "more than one tool — and even then, quote every value exactly as the tool "
    "gave it.\n"
    "If a tool reports nothing on file, say so plainly. Do not estimate, and "
    "do not suggest the reader is missing something they should have."
)

_CLARIFY_RULES = (
    "If the question is too vague to answer safely — a symptom with no "
    "duration or severity, a reading with no context — ask ONE short "
    "clarifying question instead of guessing. One at a time, and never more "
    "than a couple across the whole conversation. If you have already asked, "
    "answer with what you have."
)


def build_agentic_system_prompt(
    patient_context: str,
    compacted_context_json: str | None = None,
    recent_turns: list[dict] | None = None,
    chunks: list[RetrievedChunk] | None = None,
    allow_questions: bool = True,
) -> tuple[str, str]:
    """Return ``(stable_prefix, volatile_suffix)``.

    The prefix is byte-identical across turns so it can later carry a
    prompt-cache breakpoint (Task 23); everything that varies per turn goes in
    the suffix. Keeping them separate now costs nothing and makes that change
    a one-liner.
    """
    stable_parts = [_SAFETY_RULES, _GROUNDING_RULES, _TOOL_RULES]
    if allow_questions:
        stable_parts.append(_CLARIFY_RULES)
    stable_parts.append(_PERSONALIZATION_RULES)
    stable = "\n\n".join(stable_parts)

    volatile: list[str] = []
    if recent_turns:
        rendered = format_recent_turns(recent_turns)
        if rendered:
            volatile.append(
                "Recent conversation so far (context for follow-up questions; "
                "the user's latest message is answered below):\n" + rendered
                + "\n\nIf the latest message is short or a fragment, it is very "
                "likely a follow-up — interpret it in that context and resolve "
                "pronouns like 'it'/'that' from the recent turns."
            )
    if compacted_context_json:
        volatile.append(
            "COMPACTED_CONTEXT_JSON (topics, flags and phrases mentioned "
            "earlier in this conversation — NOT the reader's medical record; a "
            "condition appearing here means it was DISCUSSED, not that the "
            "reader has it; never present these as their own history):\n"
            + compacted_context_json
        )
    if chunks:
        volatile.append("Retrieved knowledge blocks:\n" + format_chunks(chunks))
    if patient_context:
        volatile.append("Patient context block [P]:\n" + patient_context)
    return stable, "\n\n".join(volatile)

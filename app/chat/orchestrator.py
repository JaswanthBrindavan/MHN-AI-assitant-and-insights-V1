"""Chat orchestration.

Order is the safety design: deterministic triage floor → scope guard → intent
route → handler. The symptom/educational path does scoped RAG, an LLM answer,
mechanical grounding, then output validation. Safety layers FAIL OPEN: any crash
in grounding/validation/receipts degrades to the deterministic safe reply and
logs a WARNING — a guardrail must never be a new way to break a reply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.context import build_patient_context
from app.chat.conversation import (
    add_message,
    assemble_context,
    ensure_session,
    maybe_compact,
)
from app.chat.replies import (
    GREETING_REPLY,
    HIGH_ESCALATION,
    IDENTITY_REPLY,
    SCOPE_DECLINE,
    safe_reply,
)
from app.chat.router import (
    CONVERSATIONAL,
    DATA_QUERY,
    is_identity_question,
    route,
)
from app.chat.scope import is_off_topic
from app.chat.validation import validate_reply
from app.config import get_settings
from app.grounding.claims import GroundingReport, analyze_grounding, strip_markers
from app.llm.base import LLMProvider
from app.models.chat import RagTurnReceipt
from app.rag.prompt import build_correction_directive, build_system_prompt
from app.rag.retrieval import RetrievedChunk, retrieve_chunks, scope_codes
from app.triage.red_flags import EMERGENCY, EMERGENCY_DIRECTIVE, HIGH, NONE, triage

logger = logging.getLogger("davi.chat")


@dataclass
class ChatResult:
    response_message: str
    risk_level: str
    recommended_action: str
    provenance: dict = field(default_factory=dict)
    grounding: dict | None = None
    session_id: uuid.UUID | None = None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _write_receipt(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    message: str,
    model_name: str,
    retrieved: list[dict] | None,
    grounding: dict | None,
    grounding_status: str,
    used_rag: bool,
) -> None:
    """Write an auditable receipt (hashes only, never raw text). Fail-open."""
    settings = get_settings()
    try:
        db.add(
            RagTurnReceipt(
                user_id=user_id,
                session_id=session_id,
                query_hash=_hash(message),
                model_name=model_name,
                prompt_version=settings.llm_prompt_version,
                retrieved=retrieved,
                grounding=grounding,
                grounding_mode=settings.grounding_mode,
                grounding_status=grounding_status,
                used_rag=used_rag,
            )
        )
        await db.flush()
    except Exception:  # noqa: BLE001 — receipts must never break a reply
        logger.warning("receipt write failed", exc_info=True)


async def _apply_grounding(
    provider: LLMProvider,
    system: str,
    message: str,
    answer: str,
    chunks: list[RetrievedChunk],
    patient_text: str,
) -> tuple[GroundingReport | None, str | None]:
    """Return (report, answer). answer is None → caller must use a safe reply."""
    mode = get_settings().grounding_mode
    if mode == "off":
        return None, answer

    report = analyze_grounding(
        answer,
        num_chunks=len(chunks),
        has_patient_context=bool(patient_text),
        retrieval_happened=bool(chunks),
    )
    if mode == "log" or report.status == "grounded":
        if report.status == "violations":
            logger.warning("grounding violations (log mode): %s", report.violations)
        return report, answer

    # enforce: ONE corrective retry against the SAME retrieved context.
    directive = build_correction_directive(report.violations)
    retry = await provider.generate(system=system + "\n\n" + directive, user=message)
    retry_report = analyze_grounding(
        retry,
        num_chunks=len(chunks),
        has_patient_context=bool(patient_text),
        retrieval_happened=bool(chunks),
    )
    if retry_report.status == "grounded":
        return retry_report, retry
    logger.warning("grounding still failing after retry; degrading to safe reply")
    return retry_report, None


async def _data_query_reply(db: AsyncSession, user_id: uuid.UUID) -> str:
    text, _codes = await build_patient_context(db, user_id)
    if not text:
        return (
            "I don't have any family-history insights on record for you yet. You "
            "can add family history and I'll prepare decision-support notes."
        )
    return (
        "Here's what I have on record for you. "
        + text
        + " These are decision-support notes, not a diagnosis — worth discussing "
        "with your doctor."
    )


async def _dispatch(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID,
) -> ChatResult:
    tr = triage(message)
    risk = tr.level

    # 1) Off-topic decline — only when the triage floor did not match.
    if not tr.matched and is_off_topic(message):
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name, retrieved=None, grounding=None,
            grounding_status="n/a", used_rag=False,
        )
        return ChatResult(
            response_message=SCOPE_DECLINE,
            risk_level=NONE,
            recommended_action="out_of_scope",
            provenance={"path": "scope_declined"},
        )

    intent = route(message, tr.matched)

    # 2) Emergency — deterministic directive, never an LLM.
    if risk == EMERGENCY:
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name, retrieved=None, grounding=None,
            grounding_status="n/a", used_rag=False,
        )
        return ChatResult(
            response_message=EMERGENCY_DIRECTIVE,
            risk_level=EMERGENCY,
            recommended_action="call_emergency_services",
            provenance={"path": "triage_emergency", "matched": tr.matched_terms},
        )

    # 3) Conversational (greeting / identity).
    if intent == CONVERSATIONAL:
        reply = IDENTITY_REPLY if is_identity_question(message) else GREETING_REPLY
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name, retrieved=None, grounding=None,
            grounding_status="n/a", used_rag=False,
        )
        return ChatResult(
            response_message=reply,
            risk_level=risk,
            recommended_action="none",
            provenance={"path": "conversational"},
        )

    # 4) Data query — serve stored insights/pedigree; never compute.
    if intent == DATA_QUERY:
        reply = await _data_query_reply(db, user_id)
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name, retrieved=None, grounding=None,
            grounding_status="n/a", used_rag=False,
        )
        return ChatResult(
            response_message=reply,
            risk_level=risk,
            recommended_action="review_with_clinician",
            provenance={"path": "data_query"},
        )

    # 5) Symptom / educational RAG path (risk is none or high here).
    patient_text, user_codes = await build_patient_context(db, user_id)
    codes = scope_codes(message, user_codes)
    chunks = await retrieve_chunks(db, codes, message)
    used_rag = bool(chunks)

    # Prepend COMPACTED_CONTEXT_JSON when a summary exists for this session.
    compacted_summary, _recent = await assemble_context(db, session_id)
    compacted_json = json.dumps(compacted_summary) if compacted_summary else None
    system = build_system_prompt(chunks, patient_text, compacted_json)

    answer = await provider.generate(system=system, user=message)

    report: GroundingReport | None = None
    grounding_status = "n/a"
    try:
        report, grounded_answer = await _apply_grounding(
            provider, system, message, answer, chunks, patient_text
        )
        grounding_status = report.status if report else "off"
        if grounded_answer is None:
            display = safe_reply(risk)
        else:
            display = strip_markers(grounded_answer)
            if risk == HIGH:
                display = f"{HIGH_ESCALATION} {display}"
            if not validate_reply(display, risk).ok:
                display = safe_reply(risk)
    except Exception:  # noqa: BLE001 — safety layers fail open
        logger.warning("grounding/validation failed; safe reply", exc_info=True)
        display = safe_reply(risk)
        grounding_status = "error"

    await _write_receipt(
        db, user_id=user_id, session_id=session_id, message=message,
        model_name=provider.model_name,
        retrieved=[c.to_dict() for c in chunks] if chunks else None,
        grounding=report.to_dict() if report else None,
        grounding_status=grounding_status, used_rag=used_rag,
    )

    action = "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
    return ChatResult(
        response_message=display,
        risk_level=risk,
        recommended_action=action,
        provenance={
            "path": "symptom_rag",
            "used_rag": used_rag,
            "conditions": sorted(codes),
            "chunks": [c.id for c in chunks],
        },
        grounding=report.to_dict() if report else None,
    )


async def handle_chat(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None = None,
) -> ChatResult:
    """Persist the turn, dispatch, then run deterministic compaction.

    Compaction fires after the assistant message and never raises.
    """
    session_id = await ensure_session(db, user_id, session_id)
    await add_message(
        db, session_id, "user", message,
        extracted_intent={"risk": triage(message).level},
    )
    result = await _dispatch(db, user_id, message, provider, session_id)
    await add_message(db, session_id, "assistant", result.response_message)
    await maybe_compact(db, session_id)
    result.session_id = session_id
    return result

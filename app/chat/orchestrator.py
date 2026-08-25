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
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.context import (
    build_health_snapshot,
    build_patient_context,
    is_personal_health_query,
)
from app.chat.conversation import (
    add_message,
    assemble_context,
    ensure_session,
    maybe_compact,
)
from app.chat.data_handlers import (
    handle_ai_result_query,
    handle_doctor_consult_query,
    handle_document_query,
    handle_family_list_query,
    handle_metric_query,
    handle_report_param_ask,
    handle_section_detail_query,
    handle_suggestion_query,
    handle_summary_query,
    handle_tracker_add,
    handle_value_check,
)
from app.chat.long_term import recall, record_topics
from app.chat.replies import (
    GREETING_REPLY,
    HIGH_ESCALATION,
    IDENTITY_REPLY,
    SCOPE_DECLINE,
    SELF_HARM_REPLY,
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
from app.drugs.service import (
    NON_DRUG_TERMS,
    build_drug_reply,
    build_interaction_reply,
    extract_drug_query_term,
    extract_interaction_query,
    find_drug,
    find_substitutes,
)
from app.grounding.claims import GroundingReport, analyze_grounding, strip_markers
from app.i18n.language import (
    LANGUAGE_NAMES,
    detect_language,
    language_directive,
)
from app.knowledge.registry import load_condition_index
from app.llm.base import LLMProvider
from app.models.chat import RagTurnReceipt
from app.rag.extractive import build_extractive_answer, is_definitional_ask
from app.rag.prompt import build_correction_directive, build_system_prompt
from app.rag.retrieval import RetrievedChunk, resolve_scope, retrieve_chunks
from app.translate.service import (
    InboundPivot,
    SidecarTranslator,
    get_translator,
    pivot_inbound,
    pivot_outbound,
)
from app.triage.red_flags import (
    EMERGENCY,
    EMERGENCY_DIRECTIVE,
    HIGH,
    NONE,
    triage,
)

logger = logging.getLogger("davi.chat")


@dataclass
class ChatResult:
    response_message: str
    risk_level: str
    recommended_action: str
    provenance: dict = field(default_factory=dict)
    grounding: dict | None = None
    session_id: uuid.UUID | None = None
    citations: list[dict] | None = None
    visual: dict | None = None
    language: str = "en"
    # Truthful decision trace (the pipeline's actual steps, not simulated
    # reasoning) — rendered as the "thinking" chain in clients.
    trace: list[dict] = field(default_factory=list)
    # Document cards ([{kind, resource_type, id, title, date, owner}]) for
    # replies referencing stored files — clients open them via the existing
    # app flow (Spring presigned URL / health-wallet routes).
    documents: list[dict] | None = None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _write_receipt(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    message: str,
    model_name: str,
    retrieved: list[dict] | None = None,
    grounding: dict | None = None,
    grounding_status: str = "n/a",
    used_rag: bool = False,
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
    pivot: InboundPivot | None = None,
) -> ChatResult:
    tr = triage(message)
    risk = tr.level
    # Every reply composes in English; when the pivot is active the sidecar
    # translates the final text into the user's language and script. lang is
    # only reported to the client and drives the no-sidecar LLM directive.
    lang = pivot.display_language if pivot is not None else detect_language(message)

    trace: list[dict] = []

    def t(step: str, detail: str) -> None:
        trace.append({"step": step, "detail": detail})

    if tr.matched:
        t("Safety triage",
          f"{risk.upper()} — matched: {', '.join(repr(m) for m in tr.matched_terms[:4])}")
    else:
        t("Safety triage", "no red flags detected")
    t("Language", LANGUAGE_NAMES.get(lang, lang))

    # 1) Off-topic decline — only when the triage floor did not match.
    if not tr.matched and is_off_topic(message):
        t("Scope guard", "not a health question — declining politely")
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
        )
        return ChatResult(
            response_message=SCOPE_DECLINE,
            risk_level=NONE,
            recommended_action="out_of_scope",
            provenance={"path": "scope_declined"},
            language=lang,
            trace=trace,
        )

    intent = route(message, tr.matched)

    # 2) Emergency — deterministic directive, never an LLM. Self-harm risk
    #    gets a dedicated supportive directive with the Tele-MANAS helpline.
    if risk == EMERGENCY:
        if tr.self_harm:
            t("Emergency response",
              "self-harm risk — supportive helpline directive (deterministic)")
        else:
            t("Emergency response",
              "deterministic directive — the LLM is never the arbiter of emergencies")
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
        )
        return ChatResult(
            response_message=(
                SELF_HARM_REPLY if tr.self_harm else EMERGENCY_DIRECTIVE
            ),
            risk_level=EMERGENCY,
            recommended_action="call_emergency_services",
            provenance={"path": "triage_emergency", "matched": tr.matched_terms},
            language=lang,
            trace=trace,
        )

    # 3) Conversational (greeting / identity).
    if intent == CONVERSATIONAL:
        t("Intent", "greeting / identity — canned reply")
        reply = IDENTITY_REPLY if is_identity_question(message) else GREETING_REPLY
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
        )
        return ChatResult(
            response_message=reply,
            risk_level=risk,
            recommended_action="none",
            provenance={"path": "conversational"},
            language=lang,
            trace=trace,
        )

    # 4) Deterministic data abilities — documents, tracker adds, metric
    #      pulls, health summaries, MCP suggestions. NONE risk only (the
    #      triage floor always wins) and fail-open: any crash falls through
    #      to the RAG path.
    if risk == NONE:
        try:
            # SAVEPOINT: an ability failure (e.g. a core table missing in a
            # standalone deployment) must roll back only its own writes and
            # leave the session usable for the RAG fallback.
            async with db.begin_nested():
                # A stated reading ("my sugar is 117") is specific — check it
                # against reference ranges before the other parsers. Passing the
                # session lets a bare "fasting"/"after a meal" clarification
                # recall the earlier value and re-evaluate it deterministically.
                ability = await handle_value_check(db, user_id, message, session_id)
                if ability is None:
                    ability = await handle_tracker_add(db, user_id, message)
                if ability is None:
                    # AI-result requests outrank the document LISTING — "get
                    # insights for this report" must fetch the pipeline's
                    # result, not list files.
                    ability = await handle_ai_result_query(
                        db, user_id, message, session_id
                    )
                if ability is None:
                    # Detail questions about a section ("policy number",
                    # "bill amount") outrank the LISTING of that section.
                    ability = await handle_section_detail_query(
                        db, user_id, message
                    )
                if ability is None:
                    ability = await handle_document_query(db, user_id, message)
                if ability is None:
                    ability = await handle_family_list_query(db, user_id, message)
                if ability is None:
                    ability = await handle_doctor_consult_query(
                        db, user_id, message
                    )
                if ability is None:
                    ability = await handle_metric_query(db, user_id, message)
                if ability is None:
                    # Anything else a lab report carries (basophils, RDW, …)
                    # — answered only when the test exists on file.
                    ability = await handle_report_param_ask(
                        db, user_id, message
                    )
                if ability is None:
                    ability = await handle_summary_query(db, user_id, message)
                if ability is None:
                    _ptext, user_codes = await build_patient_context(db, user_id)
                    ability = await handle_suggestion_query(
                        db, user_id, message, user_codes
                    )
            if ability is not None:
                path = ability["provenance"].get("path", "ability")
                t("Data ability",
                  f"handled deterministically by the {path.replace('_', ' ')} "
                  "handler (no LLM)")
                verdict = validate_reply(ability["reply"], risk)
                if not verdict.ok:
                    t("Output validation",
                      f"blocked ({verdict.reason}) — replaced with safe reply")
                    ability = {
                        "reply": safe_reply(risk),
                        "action": ability["action"],
                        "provenance": {**ability["provenance"],
                                       "degraded": "validation"},
                    }
                else:
                    t("Output validation", "passed all safety checks")
                await _write_receipt(
                    db, user_id=user_id, session_id=session_id,
                    message=message, model_name=provider.model_name,
                )
                return ChatResult(
                    response_message=ability["reply"],
                    risk_level=risk,
                    recommended_action=ability["action"],
                    provenance=ability["provenance"],
                    citations=ability.get("citations"),
                    visual=ability.get("visual"),
                    documents=ability.get("documents"),
                    language=lang,
                    trace=trace,
                )
        except Exception:  # noqa: BLE001 — abilities must never break a reply
            logger.warning("data ability failed; continuing", exc_info=True)

    # 4.5) Data query — serve stored insights/pedigree; never compute. Runs
    #      AFTER the abilities so a precise parse ("show me my last BP
    #      reading" → metric pull) beats the generic data-path phrasing
    #      ("show me my …") instead of being shadowed by it.
    if intent == DATA_QUERY:
        t("Intent", "question about the user's own records — serving stored data")
        reply = await _data_query_reply(db, user_id)
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
        )
        return ChatResult(
            response_message=reply,
            risk_level=risk,
            recommended_action="review_with_clinician",
            provenance={"path": "data_query"},
            language=lang,
            trace=trace,
        )

    # 5) Drug-information question — deterministic reply from the validated
    #    drug database (never the LLM). Only at NONE risk: any red-flag match
    #    stays on the symptom path so escalation is preserved. Fail-open.
    if risk == NONE:
        # 5a) Combination questions ("can I take X and Y together") — the drug
        # dataset has no interaction data, so instead of an ungrounded LLM
        # answer or the generic fallback, name both items deterministically
        # and route to a pharmacist. Fires only when at least one term is a
        # verified medicine (so "honey and lemon" still reaches the LLM).
        try:
            pair = extract_interaction_query(message)
            if pair and not all(t.lower() in NON_DRUG_TERMS for t in pair):
                # Reply with the USER'S OWN terms, not the canonical product
                # names — a composition match can resolve "ibuprofen" to an
                # obscure brand, and answering about that brand reads wrong.
                # The lookup is only the is-this-a-medicine gate.
                names: list[str] = list(pair)
                matched_any = False
                for raw in pair:
                    hit = None
                    if raw.lower() not in NON_DRUG_TERMS:
                        async with db.begin_nested():
                            hit = await find_drug(db, raw)
                    matched_any = matched_any or hit is not None
                if matched_any:
                    t("Drug interaction question",
                      f"'{names[0]}' + '{names[1]}' — deterministic "
                      "check-with-pharmacist reply (no interaction data, "
                      "no LLM)")
                    await _write_receipt(
                        db, user_id=user_id, session_id=session_id,
                        message=message, model_name=provider.model_name,
                    )
                    return ChatResult(
                        response_message=build_interaction_reply(*names),
                        risk_level=risk,
                        recommended_action="discuss_with_prescriber",
                        provenance={
                            "path": "drug_interaction_query",
                            "drugs": names,
                            "source": "medicine_master",
                        },
                        language=lang,
                        trace=trace,
                    )
        except Exception:  # noqa: BLE001 — must never break a reply
            logger.warning(
                "drug interaction check failed; continuing", exc_info=True
            )
        try:
            term = extract_drug_query_term(message)
            drug = None
            substitutes: list[str] = []
            if term:
                # SAVEPOINT: a lookup failure must leave the session usable.
                async with db.begin_nested():
                    drug = await find_drug(db, term)
                    if drug is not None:
                        substitutes = await find_substitutes(db, drug)
            if term:
                if drug is not None:
                    t("Drug lookup",
                      f"'{term}' matched {drug.name} in the validated medicines "
                      "database — deterministic reply (no LLM)")
                    reply = build_drug_reply(drug, substitutes)
                    await _write_receipt(
                        db, user_id=user_id, session_id=session_id,
                        message=message, model_name=provider.model_name,
                    )
                    return ChatResult(
                        response_message=reply,
                        risk_level=risk,
                        recommended_action="discuss_with_prescriber",
                        provenance={
                            "path": "drug_query",
                            "drug": drug.name,
                            "source": "medicine_master",
                        },
                        language=lang,
                        trace=trace,
                    )
        except Exception:  # noqa: BLE001 — drug lookup must never break a reply
            logger.warning("drug lookup failed; continuing to RAG", exc_info=True)

    # 6) Symptom / educational RAG path (risk is none or high here).
    patient_text, user_codes = await build_patient_context(db, user_id)
    # For PERSONAL-symptom questions ("why am I so tired?"), enrich the [P]
    # block with the reader's own recorded data so the answer can be correlated
    # with their lifestyle/vitals/medications — as things to discuss with a
    # clinician, never as a diagnosis or a stated cause (prompt + validator
    # enforce that). General education questions stay lean (no private data).
    if is_personal_health_query(message):
        try:
            snapshot = await build_health_snapshot(db, user_id)
            if snapshot:
                patient_text = (
                    f"{patient_text}\n\n{snapshot}" if patient_text else snapshot
                )
        except Exception:  # noqa: BLE001 — enrichment must never break a reply
            logger.warning("health snapshot failed; continuing", exc_info=True)

    # Short-term memory: recent verbatim turns drive follow-up resolution. The
    # last entry is the current message (already persisted) — the PRIOR turns
    # are the conversational context.
    compacted_summary, recent = await assemble_context(db, session_id)
    prior_turns = recent[:-1] if recent else []

    codes = await resolve_scope(db, message, user_codes)
    # Scope carry-forward: a follow-up like "is it serious?" names no condition
    # of its own, so inherit the topic from the reader's OWN recent questions.
    # Keying on "did THIS message name a condition" (message-only scope) rather
    # than on `codes` — which is never empty for users with a pedigree — so the
    # topic carries for everyone. Union with `codes` keeps pedigree context.
    message_named_condition = bool(await resolve_scope(db, message, set()))
    if not message_named_condition and prior_turns:
        recent_user_text = " ".join(
            m["message"] for m in prior_turns[-6:] if m.get("role") == "user"
        )
        carried = await resolve_scope(db, recent_user_text, set())
        if carried:
            codes = codes | carried
            t("Follow-up", f"carried topic scope from recent turns: "
              f"{sorted(carried)[:4]}")
    chunks = await retrieve_chunks(db, codes, message)
    used_rag = bool(chunks)
    try:
        _idx = await load_condition_index(db)
        _names = sorted(
            _idx.by_code[c].display_name for c in codes if _idx and c in _idx.by_code
        ) if _idx else []
    except Exception:  # noqa: BLE001
        _names = []
        _idx = None

    # Long-term memory: record the topics discussed (code → display) and any
    # red-flag terms, and recall past topics into the [P] context for this and
    # future sessions. Fail-open — never breaks a reply.
    if codes:
        topics = {
            c: (_idx.by_code[c].display_name if _idx and c in _idx.by_code else c)
            for c in codes
        }
        await record_topics(db, user_id, topics, flags=tr.matched_terms)
    recalled = await recall(db, user_id)
    if recalled:
        patient_text = f"{patient_text}\n\n{recalled}" if patient_text else recalled
    if _names:
        t("Knowledge scope", "matched conditions: " + ", ".join(_names[:4]))
    elif codes:
        t("Knowledge scope", "conditions: " + ", ".join(sorted(codes)[:4]))
    else:
        t("Knowledge scope",
          "no condition named — broad search over the validated corpus")
    if chunks:
        _sections = sorted({c.chunk_type.rsplit("_", 1)[0] for c in chunks})
        t("Retrieval",
          f"{len(chunks)} chunks from clinically reviewed profiles "
          f"({', '.join(_sections[:4])})")
    else:
        t("Retrieval", "nothing retrieved — answering with general guidance only")

    # Extractive answers — no LLM — in two cases:
    #   * no live model configured (LLM_PROVIDER=fake): serve retrieved
    #     validated content for everything rather than one canned line;
    #   * a definitional ask that names a condition ("what is X", "symptoms
    #     of X", "does X run in families"): the clinician-reviewed profile
    #     section IS the answer, so serving it verbatim is both cheaper and
    #     better-grounded than generation. Personal framings and follow-ups
    #     (no condition named in THIS message) stay with the model.
    serve_extractive = bool(chunks) and (
        get_settings().llm_provider == "fake"
        or (
            risk == NONE
            and message_named_condition
            and is_definitional_ask(message)
            and not is_personal_health_query(message)
        )
    )
    if serve_extractive:
        extractive = build_extractive_answer(chunks)
        if extractive is not None:
            t("Generate",
              "answered directly from the clinically validated profile "
              "content (no model call)")
            display = extractive
            if risk == HIGH:
                display = f"{HIGH_ESCALATION} {display}"
            verdict = validate_reply(display, risk)
            if not verdict.ok:
                t("Output validation",
                  f"blocked ({verdict.reason}) — replaced with the safe reply")
                display = safe_reply(risk)
            else:
                t("Output validation", "passed all safety checks")
            try:
                _index = await load_condition_index(db)
            except Exception:  # noqa: BLE001
                _index = None
            extractive_citations = [
                {
                    "marker": str(i + 1),
                    "source": "mcp_master_profile",
                    "condition_code": c.condition_code,
                    "section": c.chunk_type,
                    "display_name": (
                        _index.by_code[c.condition_code].display_name
                        if _index and c.condition_code in _index.by_code
                        else c.condition_code
                    ),
                }
                for i, c in enumerate(chunks[:3])
            ]
            await _write_receipt(
                db, user_id=user_id, session_id=session_id, message=message,
                model_name="extractive",
                retrieved=[c.to_dict() for c in chunks],
                grounding=None, grounding_status="extractive", used_rag=True,
            )
            action = (
                "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
            )
            return ChatResult(
                response_message=display,
                risk_level=risk,
                recommended_action=action,
                provenance={
                    "path": "symptom_rag",
                    "mode": "extractive",
                    "used_rag": True,
                    "conditions": sorted(codes),
                    "chunks": [c.id for c in chunks],
                },
                citations=extractive_citations or None,
                language=lang,
                trace=trace,
            )

    # COMPACTED_CONTEXT_JSON (summary) + recent verbatim turns (both fetched
    # above) give the model long-window and short-window conversational memory.
    compacted_json = json.dumps(compacted_summary) if compacted_summary else None
    system = build_system_prompt(
        chunks, patient_text, compacted_json, recent_turns=prior_turns[-6:]
    )
    # The reply language always follows the LATEST message. With an active
    # pivot the model must answer in English (the sidecar translates it
    # back); otherwise the directive names the detected language — and an
    # explicit "reply in English" matters just as much, so a Telugu history
    # in the recent-turns context never drags an English question's answer
    # back into Telugu.
    directive = language_directive(
        "en" if (pivot is not None and pivot.active) else lang
    )
    system = system + "\n\n" + directive

    # A provider outage must degrade to the deterministic safe reply, never
    # crash a patient-facing endpoint.
    # The provider/model identity is never disclosed — not in replies (the
    # validator enforces that) and not here in the user-visible trace either.
    t("Generate", "asking the assistant")
    try:
        answer = await provider.generate(system=system, user=message)
    except Exception:  # noqa: BLE001 — fail open
        logger.warning("LLM provider failed; safe reply", exc_info=True)
        t("Generate", "provider failed — degrading to the deterministic safe reply")
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
            retrieved=[c.to_dict() for c in chunks] if chunks else None,
            grounding=None, grounding_status="provider_error", used_rag=used_rag,
        )
        action = "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
        return ChatResult(
            response_message=safe_reply(risk),
            risk_level=risk,
            recommended_action=action,
            provenance={"path": "symptom_rag", "degraded": "provider_error"},
            language=lang,
            trace=trace,
        )

    report: GroundingReport | None = None
    grounding_status = "n/a"
    try:
        report, grounded_answer = await _apply_grounding(
            provider, system, message, answer, chunks, patient_text
        )
        grounding_status = report.status if report else "off"
        if report is not None:
            if report.status == "grounded":
                t("Claim grounding",
                  f"every clinical claim is cited ({len(report.cited)} sources)")
            else:
                t("Claim grounding",
                  f"{len(report.violations)} violation(s) found "
                  f"({get_settings().grounding_mode} mode)")
        if grounded_answer is None:
            t("Safety net", "grounding could not be repaired — safe reply instead")
            display = safe_reply(risk)
        else:
            display = strip_markers(grounded_answer)
            if risk == HIGH:
                display = f"{HIGH_ESCALATION} {display}"
            # Extend the diagnostic-assertion lexicon with the clinically-
            # validated registry names + aliases (paren-cleaned), fail-open.
            extra: tuple[str, ...] | None = None
            try:
                index = await load_condition_index(db)
                if index is not None:
                    extra = index.diagnostic_terms()
            except Exception:  # noqa: BLE001
                extra = None
            verdict = validate_reply(display, risk, extra)
            if not verdict.ok:
                t("Output validation",
                  f"blocked ({verdict.reason}) — replaced with the safe reply")
                display = safe_reply(risk)
            else:
                t("Output validation", "passed all safety checks")
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
    citations = await _build_citations(db, report, chunks, bool(patient_text))
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
        citations=citations,
        language=lang,
        trace=trace,
    )


async def _build_citations(
    db: AsyncSession,
    report: GroundingReport | None,
    chunks: list[RetrievedChunk],
    has_patient_context: bool,
) -> list[dict] | None:
    """Structured citations for the markers the answer actually cited."""
    if report is None or not report.cited:
        return None
    try:
        index = await load_condition_index(db)
    except Exception:  # noqa: BLE001
        index = None
    citations: list[dict] = []
    for marker in report.cited:
        if marker == "P":
            if has_patient_context:
                citations.append(
                    {"marker": "P", "source": "patient_context",
                     "display_name": "Your health record"}
                )
            continue
        if marker == "GK":
            citations.append(
                {"marker": "GK", "source": "general_knowledge",
                 "display_name": "General knowledge (nothing retrieved)"}
            )
            continue
        i = int(marker) - 1
        if 0 <= i < len(chunks):
            chunk = chunks[i]
            display = chunk.condition_code
            if index is not None and chunk.condition_code in index.by_code:
                display = index.by_code[chunk.condition_code].display_name
            citations.append(
                {
                    "marker": marker,
                    "source": "mcp_master_profile",
                    "condition_code": chunk.condition_code,
                    "section": chunk.chunk_type,
                    "display_name": display,
                }
            )
    return citations or None


# C0/C1 control characters carry no linguistic meaning and NUL (0x00) is
# illegal in PostgreSQL text — an unsanitized NUL in a message raises
# asyncpg CharacterNotInRepertoireError when the turn is persisted, 500-ing
# the request. Strip all control chars except tab/newline/carriage-return.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize_message(message: str) -> str:
    return _CONTROL_CHARS.sub("", message)


async def handle_chat(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None = None,
    translator: SidecarTranslator | None = None,
) -> ChatResult:
    """Persist the turn, dispatch, then run deterministic compaction.

    Compaction fires after the assistant message and never raises. When the
    translation sidecar is configured, non-English messages are pivoted
    through English (history keeps the user's original words; triage and the
    whole pipeline see English) and the reply is translated back.
    """
    message = _sanitize_message(message)
    if translator is None:
        translator = get_translator()
    pivot = await pivot_inbound(message, translator)
    work = pivot.english_text if pivot.active else message
    session_id = await ensure_session(db, user_id, session_id)
    await add_message(
        db, session_id, "user", message,
        extracted_intent={"risk": triage(work).level},
    )
    result = await _dispatch(db, user_id, work, provider, session_id, pivot=pivot)
    if pivot.active and result.response_message:
        translated = await pivot_outbound(
            result.response_message, pivot, translator
        )
        if translated is not None:
            result.response_message = translated
            result.provenance["translation"] = {
                "language": pivot.display_language, "status": "translated",
            }
        else:
            # Fail open: the English reply is always safe to show.
            result.provenance["translation"] = {
                "language": pivot.display_language, "status": "fallback_english",
            }
    # Persist the reply's structured extras alongside the text so a restored
    # conversation keeps its document cards (and action line) after a reload —
    # the extracted_intent JSON column already exists for exactly this kind of
    # per-message metadata.
    assistant_meta: dict | None = None
    if result.documents or result.recommended_action:
        assistant_meta = {}
        if result.documents:
            assistant_meta["documents"] = result.documents
        if result.recommended_action:
            assistant_meta["action"] = result.recommended_action
    await add_message(
        db, session_id, "assistant", result.response_message,
        extracted_intent=assistant_meta,
    )
    await maybe_compact(db, session_id)
    result.session_id = session_id
    return result

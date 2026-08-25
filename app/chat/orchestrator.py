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
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import memory_assembly
from app.chat.agent import append_directive, recover, run_agent
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
    questions_asked,
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
from app.chat.db_release import ReleasingProvider
from app.chat.replies import (
    GREETING_REPLIES,
    HIGH_ESCALATION,
    IDENTITY_REPLIES,
    SCOPE_DECLINES,
    SELF_HARM_REPLY,
    pick,
    safe_reply,
)
from app.chat.router import (
    CONVERSATIONAL,
    DATA_QUERY,
    is_identity_question,
    route,
)
from app.chat.scope import is_off_topic
from app.chat.tools.definitions import TOOL_SPECS
from app.chat.tools.registry import execute_tool
from app.chat.validation import redact_reason, validate_reply
from app.config import get_settings
from app.coredata.service import allergy_warning, medication_allergies
from app.drugs.service import (
    NON_DRUG_TERMS,
    build_drug_reply,
    build_interaction_reply,
    extract_drug_query_term,
    extract_interaction_query,
    find_drug,
)
from app.grounding.claims import GroundingReport, analyze_grounding, strip_markers
from app.grounding.fidelity import unit_values, values_traceable
from app.i18n.language import (
    LANGUAGE_NAMES,
    detect_language,
    language_directive,
)
from app.knowledge.registry import load_condition_index
from app.llm.base import LLMProvider
from app.llm.tools import UserMessage
from app.models.chat import RagTurnReceipt
from app.rag.extractive import build_extractive_answer, is_definitional_ask
from app.rag.prompt import (
    build_agentic_system_prompt,
    build_correction_directive,
    build_system_prompt,
)
from app.rag.retrieval import RetrievedChunk, resolve_scope, retrieve_chunks
from app.telemetry import (
    chat_latency,
    chat_turns,
    degradations,
    llm_tokens,
    record_fail_open,
    timed,
    tool_calls,
)
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
        record_fail_open("receipts")


async def _apply_grounding(
    provider: LLMProvider,
    system: str | Sequence[str],
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
    # append_directive, not `system + x`: on a split prompt the naive form
    # writes into the cached prefix, which is the one string that must not
    # change.
    retry = await provider.generate(
        system=append_directive(system, "\n\n" + directive), user=message
    )
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


async def _interaction_refusal(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None,
    risk: str,
    lang: str,
    trace: list[dict],
    t,
) -> ChatResult | None:
    """The deterministic reply to "can I take X with Y". None if not asked.

    Lives OUTSIDE the engine branch on purpose. It is provider-independent and
    it is the one drug question where an ungrounded answer can hurt someone,
    so both engines must pass through it — see project_docs/
    task-25-drug-interactions.md.
    """
    if risk != NONE:
        # The triage floor always wins; a red flag stays on the escalation
        # path rather than being answered as a medication question.
        return None
    # Combination questions ("can I take X and Y together"). There is
    # NO interaction dataset, so the only honest answer is a deterministic
    # one that names both items and routes to a pharmacist — never an
    # ungrounded LLM answer, and never the generic safe fallback.
    #
    # This fires on the PHRASING, not on whether drug_reference recognised
    # the terms. Requiring a database hit (as this did originally) meant an
    # unrecognised medicine name — a foreign brand, a misspelling, a
    # supplement, anything outside the Indian dataset — fell through to the
    # LLM, on the one question class where an ungrounded answer can do the
    # most harm. The asymmetry settles it: a false refusal costs a mildly
    # unhelpful "ask a pharmacist"; a false ANSWER about a real interaction
    # can hurt someone.
    #
    # Ordinary food pairings still reach the LLM through NON_DRUG_TERMS,
    # which carries everyday foods for exactly this reason.
    # NARROW on purpose. The pattern match touches no I/O and cannot fail for
    # a database reason; only the lookup can. A wider try -- one that also
    # wrapped the receipt write below -- would mean a transient DB error
    # DELETED the refusal and handed "can I take warfarin and aspirin
    # together?" to the LLM. That is the exact outcome this function exists to
    # prevent, and it would have been reachable by a database hiccup.
    try:
        pair = extract_interaction_query(message)
    except Exception:  # noqa: BLE001 — a parser must never break a reply
        logger.warning("interaction extraction failed; continuing", exc_info=True)
        record_fail_open("drug_interaction")
        return None

    if not pair or all(term.lower() in NON_DRUG_TERMS for term in pair):
        return None

    # Reply with the USER'S OWN terms, not the canonical product names — a
    # composition match can resolve "ibuprofen" to an obscure brand, and
    # answering about that brand reads wrong.
    names: list[str] = list(pair)

    # Recorded, not gated on: it says how often the refusal fires for terms the
    # dataset has never heard of, which is the number that would justify buying
    # a better one. A lookup failure costs us that statistic and NOTHING else —
    # the refusal proceeds either way.
    recognised = False
    try:
        for raw in pair:
            if raw.lower() in NON_DRUG_TERMS:
                continue
            async with db.begin_nested():
                if await find_drug(db, raw) is not None:
                    recognised = True
    except Exception:  # noqa: BLE001 — the refusal does not depend on this
        logger.warning("drug recognition lookup failed; continuing", exc_info=True)
        record_fail_open("drug_interaction")

    t("Drug interaction question",
      f"'{names[0]}' + '{names[1]}' — deterministic "
      "check-with-pharmacist reply (no interaction data, "
      "no LLM)")
    # _write_receipt is already fail-open internally, so a receipt failure
    # cannot take the refusal down with it.
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
            "source": "drug_reference",
            "recognised": recognised,
        },
        language=lang,
        trace=trace,
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
            response_message=pick(SCOPE_DECLINES, session_id),
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
        # RECORD BEFORE EXITING. An emergency is the single event most worth
        # remembering, and until now it was the only severity that opened no
        # episode: this path returns before either engine reaches the normal
        # recording step, so the top of the range the triage floor decides was
        # never persisted.
        #
        # This runs in the SHARED prologue, so one call covers both engines.
        # It records the event only — no topics, because retrieval has not run
        # and must not: emergency handling does NOT continue through the normal
        # symptom-assessment flow. Fail-open, so remembering can never delay or
        # displace the directive the reader needs right now.
        await memory_assembly.record(
            db, user_id, codes=(), flags=tr.matched_terms, risk=risk
        )
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
        reply = (
            pick(IDENTITY_REPLIES, session_id)
            if is_identity_question(message)
            else pick(GREETING_REPLIES, session_id)
        )
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

    # 3.4) Drug-combination questions. SHARED across both engines, and it has
    #      to be: there is no interaction dataset, so a model asked "can I take
    #      X with Y" would answer from its own weights. This sat inside the
    #      legacy chain below until two safety evals caught the agentic engine
    #      answering interaction questions itself — a gap that retiring the
    #      legacy chain (Task 12) would have made permanent and invisible.
    combination = await _interaction_refusal(
        db, user_id, message, provider, session_id, risk, lang, trace, t
    )
    if combination is not None:
        return combination

    # 3.5) Engine selection. Everything above — the triage floor, the scope
    #      guard, the emergency directive, the canned conversational replies
    #      and the drug-combination refusal — is SHARED and has already run,
    #      so the agentic engine can never see an emergency and the model is
    #      never the arbiter of one.
    if get_settings().chat_engine == "agentic":
        t("Engine", "agentic — the assistant can look things up for itself")
        return await _dispatch_agentic(
            db, user_id, message, provider, session_id, tr, risk, lang, trace, t
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
                      f"blocked ({redact_reason(verdict.reason)}) — replaced with safe reply")
                    ability = {
                        "reply": safe_reply(risk, session_id),
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
            record_fail_open("abilities")

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
        try:
            term = extract_drug_query_term(message)
            drug = None
            if term:
                # SAVEPOINT: a lookup failure must leave the session usable.
                async with db.begin_nested():
                    drug = await find_drug(db, term)
            if term:
                if drug is not None:
                    t("Drug lookup",
                      f"'{term}' matched {drug.name} in the validated medicines "
                      "database — deterministic reply (no LLM)")
                    # The reader's OWN medication allergies. This path
                    # returns before the [P] block is built and lives inside
                    # the legacy branch, so nothing else would carry them.
                    # Fail-open: a lookup failure must not cost the answer.
                    warning = ""
                    try:
                        async with db.begin_nested():
                            warning = allergy_warning(
                                await medication_allergies(db, user_id)
                            )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "allergy lookup failed; continuing", exc_info=True
                        )
                        record_fail_open("allergy_lookup")
                    reply = build_drug_reply(drug, allergy_warning=warning)
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
                            "source": "drug_reference",
                        },
                        language=lang,
                        trace=trace,
                    )
        except Exception:  # noqa: BLE001 — drug lookup must never break a reply
            logger.warning("drug lookup failed; continuing to RAG", exc_info=True)
            record_fail_open("drug_lookup")

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
            record_fail_open("health_snapshot")

    # Per-user memory (profile, open episodes, past topics), read once through
    # the SHARED assembly so both engines see all of it.
    _memory = await memory_assembly.assemble(db, user_id)

    # Short-term memory: recent verbatim turns drive follow-up resolution. The
    # last entry is the current message (already persisted) — the PRIOR turns
    # are the conversational context.
    compacted_summary, recent = await assemble_context(db, session_id)
    prior_turns = recent[:-1] if recent else []

    # Message-only scope FIRST, then union the pedigree codes in. The old
    # shape called resolve_scope twice with the same message — a duplicate
    # registry match and an extra round trip — because it needed both answers.
    # Deriving one from the other gives both for one call.
    message_codes = await resolve_scope(db, message, set())
    codes = message_codes | await resolve_scope(db, "", user_codes)
    # Scope carry-forward: a follow-up like "is it serious?" names no condition
    # of its own, so inherit the topic from the reader's OWN recent questions.
    # Keying on "did THIS message name a condition" rather than on `codes` —
    # which is never empty for users with a pedigree — so the topic carries
    # for everyone. Union with `codes` keeps pedigree context.
    message_named_condition = bool(message_codes)
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
    # Why this turn fell back, if it did. The agentic path has always recorded
    # this; the legacy path did not, which left the degradation metric blind on
    # the engine that currently answers real users. Declared here because both
    # the extractive branch and the main RAG branch set it.
    legacy_degraded: str | None = None
    try:
        _idx = await load_condition_index(db)
        _names = sorted(
            _idx.by_code[c].display_name for c in codes if _idx and c in _idx.by_code
        ) if _idx else []
    except Exception:  # noqa: BLE001
        _names = []
        _idx = None

    # Per-user memory: profile, open episodes, and past topics. Read through
    # the SHARED assembly so both engines see all of it — legacy used to read
    # only the topic recall, so a reader's consent-gated profile never reached
    # the prompt on the default engine. See app/chat/memory_assembly.py.
    await memory_assembly.record(
        db, user_id, codes=codes, flags=tr.matched_terms, risk=risk
    )
    patient_text = _memory.append_to(patient_text)
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
                  f"blocked ({redact_reason(verdict.reason)}) — replaced with the safe reply")
                display = safe_reply(risk, session_id)
                legacy_degraded = "validation"
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
                    **({"degraded": legacy_degraded} if legacy_degraded else {}),
                },
                citations=extractive_citations or None,
                language=lang,
                trace=trace,
            )

    # COMPACTED_CONTEXT_JSON (summary) + recent verbatim turns (both fetched
    # above) give the model long-window and short-window conversational memory.
    compacted_json = json.dumps(compacted_summary) if compacted_summary else None
    stable_rules, volatile_context = build_system_prompt(
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
    # Split, not joined: element 0 is byte-identical across turns and carries
    # the prompt-cache breakpoint in the Anthropic adapter. Every other
    # provider joins it straight back, so the model is told exactly the same
    # thing either way. The language directive belongs in the VOLATILE half --
    # it changes with the reader's language, and a per-reader prefix caches
    # for nobody.
    system: list[str] = [
        stable_rules,
        "\n\n".join(p for p in (volatile_context, directive) if p),
    ]

    # A provider outage must degrade to the deterministic safe reply, never
    # crash a patient-facing endpoint.
    # The provider/model identity is never disclosed — not in replies (the
    # validator enforces that) and not here in the user-visible trace either.
    t("Generate", "asking the assistant")
    try:
        answer = await provider.generate(system=system, user=message)
    except Exception:  # noqa: BLE001 — fail open
        logger.warning("LLM provider failed; safe reply", exc_info=True)
        record_fail_open("provider")
        t("Generate", "provider failed — degrading to the deterministic safe reply")
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name,
            retrieved=[c.to_dict() for c in chunks] if chunks else None,
            grounding=None, grounding_status="provider_error", used_rag=used_rag,
        )
        action = "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
        return ChatResult(
            response_message=safe_reply(risk, session_id),
            risk_level=risk,
            recommended_action=action,
            provenance={"path": "symptom_rag", "degraded": "provider_error"},
            language=lang,
            trace=trace,
        )

    report: GroundingReport | None = None
    grounding_status = "n/a"
    # (legacy_degraded is declared above, before the extractive branch that
    # also sets it — the agentic path has always recorded this; the legacy
    # path did not, leaving the degradation metric blind on the engine that
    # currently answers real users.)
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
            display = safe_reply(risk, session_id)
            legacy_degraded = "grounding"
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
                  f"blocked ({redact_reason(verdict.reason)}) — replaced with the safe reply")
                display = safe_reply(risk, session_id)
                legacy_degraded = "validation"
            else:
                t("Output validation", "passed all safety checks")
    except Exception:  # noqa: BLE001 — safety layers fail open
        logger.warning("grounding/validation failed; safe reply", exc_info=True)
        record_fail_open("grounding")
        display = safe_reply(risk, session_id)
        grounding_status = "error"
        legacy_degraded = "guard_error"

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
            **({"degraded": legacy_degraded} if legacy_degraded else {}),
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
    # Commit before every model call, so a pooled connection is not pinned for
    # the seconds the provider spends on the network. See app/chat/db_release.py
    # -- this is the binding constraint on concurrency, and it is invisible
    # from reading the endpoint.
    provider = ReleasingProvider(provider, db)
    if translator is None:
        translator = get_translator()
    pivot = await pivot_inbound(message, translator)
    work = pivot.english_text if pivot.active else message
    session_id = await ensure_session(db, user_id, session_id)
    await add_message(
        db, session_id, "user", message,
        extracted_intent={"risk": triage(work).level},
    )
    # Every path converges here, so this is the one place that sees the whole
    # turn: how long it took, which engine ran it, and whether the reader got
    # a real answer or a fallback. Label values come from bounded sets only —
    # never the message, the user, or a condition name.
    engine = get_settings().chat_engine
    with timed(chat_latency, engine=engine):
        result = await _dispatch(
            db, user_id, work, provider, session_id, pivot=pivot
        )

    chat_turns.inc(engine=engine, risk=result.risk_level)
    degraded = result.provenance.get("degraded")
    if degraded:
        # THE number that says whether the system is quietly answering badly.
        degradations.inc(engine=engine, reason=str(degraded))
    for name in result.provenance.get("tools", []) or []:
        tool_calls.inc(tool=str(name))
    usage = result.provenance.get("usage") or {}
    for direction in ("input_tokens", "output_tokens"):
        if usage.get(direction):
            llm_tokens.inc(usage[direction], direction=direction)
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
    await maybe_compact(db, session_id, provider)
    result.session_id = session_id
    return result


async def _dispatch_agentic(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider,
    session_id: uuid.UUID,
    tr,
    risk: str,
    lang: str,
    trace: list[dict],
    t,
) -> ChatResult:
    """The tool-driven path.

    Callers guarantee the triage floor, the scope guard, the emergency path and
    the conversational path have ALREADY run — this is never reached for an
    emergency, and the model is never the arbiter of one.
    """
    settings = get_settings()

    patient_text, user_codes = await build_patient_context(db, user_id)
    if is_personal_health_query(message):
        try:
            snapshot = await build_health_snapshot(db, user_id)
            if snapshot:
                patient_text = (
                    f"{patient_text}\n\n{snapshot}" if patient_text else snapshot
                )
        except Exception:  # noqa: BLE001 — enrichment must never break a reply
            logger.warning("health snapshot failed; continuing", exc_info=True)
            record_fail_open("health_snapshot")

    # Per-user memory, through the SHARED assembly. Agentic used to read the
    # profile and episodes but never the long-term topic recall, and never
    # recorded topics at all. See app/chat/memory_assembly.py.
    _memory = await memory_assembly.assemble(db, user_id)
    patient_text = _memory.append_to(patient_text)

    compacted, recent = await assemble_context(db, session_id)
    prior_turns = recent[:-1] if recent else []

    codes = await resolve_scope(db, message, user_codes)
    chunks = await retrieve_chunks(db, codes, message)

    # Remember what was raised, at the severity the FLOOR decided — never a
    # severity the model inferred. Placed HERE, before generation, to match
    # legacy: what the reader raised does not depend on whether the reply
    # succeeded. Recording it after the guards (as this used to) meant an
    # agent-loop failure silently forgot the symptom they just described.
    await memory_assembly.record(
        db, user_id, codes=codes, flags=tr.matched_terms, risk=risk
    )

    asked = await questions_asked(db, session_id)
    allow_questions = asked < settings.chat_max_clarifying_questions

    stable, volatile = build_agentic_system_prompt(
        patient_text,
        json.dumps(compacted) if compacted else None,
        recent_turns=prior_turns[-6:],
        chunks=chunks,
        allow_questions=allow_questions,
    )
    directive = language_directive("en" if lang != "en" else lang)
    # Split, not joined: element 0 is byte-identical across every turn and
    # carries the prompt-cache breakpoint in the Anthropic adapter. Every
    # other provider joins it straight back, so the model is told exactly the
    # same thing either way — only the billing differs.
    #
    # The language directive belongs in the VOLATILE half: it changes with the
    # reader's language, and a per-reader prefix caches for nobody.
    system: list[str] = [stable, "\n\n".join(p for p in (volatile, directive) if p)]

    async def _executor(call):
        return await execute_tool(db, user_id, call, session_id)

    # Tools are offered only at NONE risk. A red flag stays on the safe path so
    # nothing can delay or dilute an escalation.
    offered = TOOL_SPECS if risk == NONE else ()

    t("Generate", "asking the assistant, with access to your records")
    try:
        outcome = await run_agent(
            provider, system, [UserMessage(message)], offered, _executor,
            max_rounds=settings.llm_max_tool_rounds,
        )
    except Exception:  # noqa: BLE001 — fail open, never crash the endpoint
        logger.warning("agent loop failed; safe reply", exc_info=True)
        record_fail_open("agent")
        t("Generate", "provider failed — degrading to the deterministic safe reply")
        await _write_receipt(
            db, user_id=user_id, session_id=session_id, message=message,
            model_name=provider.model_name, grounding_status="provider_error",
        )
        return ChatResult(
            response_message=safe_reply(risk, session_id),
            risk_level=risk,
            recommended_action=(
                "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
            ),
            provenance={"path": "agentic", "degraded": "provider_error"},
            language=lang, trace=trace,
        )

    if outcome.tool_names:
        t("Records", "looked up: " + ", ".join(
            sorted({n.replace("_", " ") for n in outcome.tool_names})))

    display = strip_markers(outcome.text)
    if risk == HIGH:
        display = f"{HIGH_ESCALATION} {display}"

    degraded: str | None = None

    try:
        index = await load_condition_index(db)
        extra_terms = index.diagnostic_terms() if index is not None else None
    except Exception:  # noqa: BLE001
        extra_terms = None

    async def _try_recover(reason: str, detail: str = "") -> bool:
        """One corrective retry before falling back. True if it worked.

        Without this, a guard rejection throws the whole answer away and
        substitutes one fixed sentence — the reader gets a non-answer with no
        explanation and no path forward, and two in a row look like a broken
        bot. The floor is unchanged; it is just reached less often.
        """
        nonlocal display
        rewritten = await recover(
            provider, system, outcome.messages, reason, detail
        )
        if not rewritten:
            return False
        candidate = strip_markers(rewritten)
        if risk == HIGH:
            candidate = f"{HIGH_ESCALATION} {candidate}"
        retry_ok, _ = values_traceable(candidate, sources)
        if not retry_ok:
            return False
        if not sources and unit_values(candidate):
            return False
        if not validate_reply(candidate, risk, extra_terms).ok:
            return False
        display = candidate
        return True

    # Fidelity FIRST: a drifted lab value is worse than a blocked reply, and it
    # is the failure mode the validator cannot see.
    sources = [*outcome.source_texts, *(c.content for c in chunks)]
    if patient_text:
        sources.append(patient_text)
    ok, stray = values_traceable(display, sources)
    if not ok:
        logger.warning("numeric fidelity failure: %s", stray)
        if await _try_recover("fidelity", ", ".join(stray)):
            t("Value check", "a stated value was corrected on a second pass")
        else:
            t("Value check",
              "a stated value did not match your records — replaced with the "
              "safe reply")
            display, degraded = safe_reply(risk, session_id), "fidelity"
    elif not sources and unit_values(display):
        # Nothing was retrieved and no tool ran, yet the reply states a
        # clinical value or a dose. There is nothing behind it — the model
        # made it up. This is the case values_traceable deliberately cannot
        # judge (no sources to compare against), so the policy lives here.
        stated = unit_values(display)
        logger.warning("ungrounded clinical value with no sources: %s", stated)
        if await _try_recover("ungrounded_value", ", ".join(stated)):
            t("Value check", "an unsupported figure was removed on a second pass")
        else:
            t("Value check",
              "a dose or measurement was stated with nothing to support it — "
              "replaced with the safe reply")
            display, degraded = safe_reply(risk, session_id), "ungrounded_value"
    elif sources:
        t("Value check", "every value matches your records")

    if degraded is None:
        verdict = validate_reply(display, risk, extra_terms)
        if not verdict.ok:
            if await _try_recover(verdict.reason):
                t("Output validation",
                  f"first attempt blocked ({redact_reason(verdict.reason)}); the rewrite "
                  "passed")
            else:
                t("Output validation",
                  f"blocked ({redact_reason(verdict.reason)}) — replaced with the safe reply")
                display, degraded = safe_reply(risk, session_id), "validation"
        else:
            t("Output validation", "passed all safety checks")

    await _write_receipt(
        db, user_id=user_id, session_id=session_id, message=message,
        model_name=provider.model_name,
        retrieved=[c.to_dict() for c in chunks] if chunks else None,
        grounding_status="agentic", used_rag=bool(chunks),
    )

    provenance: dict = {
        "path": "agentic",
        "tools": outcome.tool_names,
        "rounds": outcome.rounds,
        "conditions": sorted(codes),
        "usage": outcome.usage,
    }
    if outcome.forced:
        provenance["forced_answer"] = True
    if degraded:
        provenance["degraded"] = degraded

    return ChatResult(
        response_message=display,
        risk_level=risk,
        recommended_action=(
            "seek_care_promptly" if risk == HIGH else "discuss_with_clinician"
        ),
        provenance=provenance,
        language=lang,
        trace=trace,
    )

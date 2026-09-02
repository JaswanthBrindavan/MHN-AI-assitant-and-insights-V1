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
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import memory_assembly
from app.chat.abilities import (
    parse_correlation_query,
    parse_document_query_fuzzy,
    parse_metric_query,
    parse_report_param_ask,
    parse_section_detail_query,
    parse_stated_value,
    parse_summary_query,
    parse_tracker_add,
    parse_tracker_query,
)
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
    last_pending_med,
    maybe_compact,
    questions_asked,
)
from app.chat.correlation import READ_FAILED as CORRELATION_READ_FAILED
from app.chat.data_handlers import (
    handle_ai_result_query,
    handle_correlation_query,
    handle_doctor_consult_query,
    handle_document_query,
    handle_family_list_query,
    handle_metric_query,
    handle_report_param_ask,
    handle_section_detail_query,
    handle_suggestion_query,
    handle_summary_query,
    handle_tracker_add,
    handle_tracker_query,
    handle_value_check,
)
from app.chat.db_release import ReleasingProvider
from app.chat.episodes import is_recovery_message, open_episodes
from app.chat.episodes import worst_level as episodes_worst_level
from app.chat.medication_flow import handle_medication_turn
from app.chat.replies import (
    CARRIED_ESCALATION,
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
    build_dose_refusal,
    build_drug_reply,
    build_interaction_reply,
    extract_dose_query,
    extract_drug_query_term,
    extract_interaction_query,
    find_drug,
    find_substitutes,
)
from app.grounding.claims import (
    MARKER_RE,
    GroundingReport,
    analyze_grounding,
    strip_markers,
)
from app.grounding.fidelity import unit_values, values_traceable
from app.i18n.language import (
    LANGUAGE_NAMES,
    detect_language,
    language_directive,
)
from app.i18n.notices import english_fallback_notice
from app.knowledge.registry import load_condition_index
from app.llm.base import LLMProvider
from app.llm.tools import UserMessage
from app.models.chat import RagTurnReceipt
from app.rag.extractive import (
    build_extractive_answer,
    disclosure_menu,
    is_definitional_ask,
    is_focused,
    rendered_chunks,
)
from app.rag.prompt import (
    build_agentic_system_prompt,
    build_correction_directive,
    build_system_prompt,
)
from app.rag.retrieval import (
    RetrievedChunk,
    _base_section,
    resolve_scope,
    retrieve_chunks,
    target_sections,
)
from app.telemetry import (
    chat_latency,
    chat_turns,
    degradations,
    llm_tokens,
    record_fail_open,
    timed,
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
    LEVEL_ORDER,
    NONE,
    TriageResult,
    max_level,
    triage,
)

logger = logging.getLogger("davi.chat")


def lang_hint(pivot, message: str) -> str:
    """The reader's language for notice selection — pivot detection first,
    script-range detection as the fallback."""
    if pivot is not None and pivot.language and pivot.language != "en":
        return pivot.language
    return detect_language(message)



@dataclass(frozen=True)
class Used:
    """What the ANSWER used — carried out of a path WITH the answer.

    NOT "what retrieval returned". Retrieval runs ahead of the engine branch,
    so a tracker total, a drug lookup and a corpus answer all leave the same
    function with the same `chunks` variable in scope; deriving citations from
    that variable is what made "how much water did I drink" cite four
    unrelated condition profiles.

    `chunks` is the numbered block list markers index into (prompt order);
    `markers` names the blocks the answer actually used. The default is
    "nothing from the corpus", so a path that declares nothing cites nothing —
    the safe direction, and the only one that survives a sixth call site.
    """

    chunks: tuple[RetrievedChunk, ...] = ()
    markers: tuple[str, ...] = ()
    patient: bool = False


def used_rendered(chunks: Sequence[RetrievedChunk]) -> Used:
    """Blocks the answer rendered VERBATIM — every one of them was used."""
    return Used(tuple(chunks), tuple(str(i) for i in range(1, len(chunks) + 1)))


def used_cited(
    text: str, chunks: Sequence[RetrievedChunk], patient_text: str = ""
) -> Used:
    """A GENERATED answer used exactly the blocks it CITED.

    Both engines put the same numbered blocks and the same grounding rules in
    front of the model, so both have marker evidence; the agentic engine threw
    it away with `strip_markers` and cited the retrieved set instead.
    `dict.fromkeys` dedupes in first-cited order, so identical text always
    yields an identical citation list.
    """
    return Used(
        tuple(chunks),
        tuple(dict.fromkeys(MARKER_RE.findall(text))),
        bool(patient_text),
    )


def used_plus(used: Used, extra: Sequence[RetrievedChunk]) -> Used:
    """`used`, plus corpus blocks a TOOL retrieved and rendered itself.

    A tool answer carries no `[n]` — its blocks were never numbered into the
    prompt — but the reply quotes them verbatim, so they are what it used.
    """
    if not extra:
        return used
    off = len(used.chunks)
    return Used(
        used.chunks + tuple(extra),
        used.markers + tuple(str(off + i) for i in range(1, len(extra) + 1)),
        used.patient,
    )


@dataclass
class ChatResult:
    response_message: str
    risk_level: str
    recommended_action: str
    provenance: dict = field(default_factory=dict)
    grounding: dict | None = None
    session_id: uuid.UUID | None = None
    # NEVER set at a construction site: `handle_chat` derives it from
    # `used`, which is the only thing a path declares. Forgetting to
    # declare `used` yields NO citations — the one failure direction
    # that cannot mislead a reader.
    citations: list[dict] | None = None
    used: Used | None = None
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


def _lead(escalation: str, risk: str, reply: str) -> str:
    """The escalation banner AND the answer, at HIGH risk.

    Two rules in one line, both learned the hard way:

    * the answer is ADDED to the banner, never replaced by it. A reader who
      asked "is my coffee the reason I sleep badly" and got only "you
      mentioned something earlier ..." was not answered at all.
    * an EMPTY reply stays empty. Prefixing a banner to "" produced a
      banner-only reply that PASSED ``validate_reply`` (non-empty, carries an
      escalation), so the `empty` rule never fired and the safe reply never
      took over.
    """
    return f"{escalation} {reply}" if risk == HIGH and reply else reply


def _led_action(risk: str, reply: str, action: str) -> str:
    """The action that matches the reply the reader actually got.

    When `_lead` prefixes the escalation, the reply TELLS them to seek care
    promptly while `recommended_action` still carried the handler's own verdict
    — `self_care` for a correlation readout, `discuss_with_clinician` for a
    summary. So the prose and the machine-readable field said different things
    in one payload, and the field is the half the mobile clients render.
    """
    return "seek_care_promptly" if risk == HIGH and reply else action


async def _stage(name: str, coro):
    """Time one pre-retrieval stage and log it. NOT in the user-facing trace.

    Staging measured 43s on a turn that made NO model call at all (a
    definitional ask served extractively) and 113s on one that did. In both the
    whole cost sat between routing and retrieval, which the trace records as a
    single opaque jump. These lines name the stage; they carry a duration and
    nothing else, so no PHI reaches the log.
    """
    started = time.perf_counter()
    try:
        return await coro
    finally:
        logger.info(
            "chat stage %s %.0fms", name, (time.perf_counter() - started) * 1000
        )


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
        # Content-verified citations: a cited sentence's values must appear
        # in the cited chunk, not merely cite an existing number.
        chunk_texts=[c.content for c in chunks],
        patient_text=patient_text,
    )
    if mode == "log" or report.status == "grounded":
        if report.status == "violations":
            kinds = sorted({v.get("type", "?") for v in report.violations})
            logger.warning(
                "grounding violations (log mode): %d violation(s) [%s]",
                len(report.violations), ", ".join(kinds),
            )
        return report, answer

    # enforce: ONE corrective retry against the SAME retrieved context.
    directive = build_correction_directive(report.violations, answer)
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
    escalation: str = HIGH_ESCALATION,
) -> ChatResult | None:
    """The deterministic reply to "can I take X with Y". None if not asked.

    Lives OUTSIDE the engine branch on purpose. It is provider-independent and
    it is the one drug question where an ungrounded answer can hurt someone,
    so both engines must pass through it — see project_docs/
    task-25-drug-interactions.md.
    """
    if risk == EMERGENCY:
        return None  # emergencies stay on the deterministic directive
    # At HIGH the refusal STILL applies — these are the question classes an
    # ungrounded answer harms most, and "HIGH" used to hand them to the LLM.
    # The escalation banner is prepended below so the floor stays visible.
    # Combination questions ("can I take X and Y together"). There is
    # NO interaction dataset, so the only honest answer is a deterministic
    # one that names both items and routes to a pharmacist — never an
    # ungrounded LLM answer, and never the generic safe fallback.
    #
    # This fires on the PHRASING, not on whether medicine_master recognised
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
    interaction_reply = build_interaction_reply(*names)
    if risk == HIGH:
        interaction_reply = f"{escalation} {interaction_reply}"
    return ChatResult(
        response_message=interaction_reply,
        risk_level=risk,
        recommended_action="discuss_with_prescriber",
        provenance={
            "path": "drug_interaction_query",
            "drugs": names,
            "source": "medicine_master",
            "recognised": recognised,
        },
        language=lang,
        trace=trace,
    )


async def _dosing_refusal(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None,
    risk: str,
    lang: str,
    trace: list[dict],
    t,
    escalation: str = HIGH_ESCALATION,
) -> ChatResult | None:
    """The deterministic reply to a dose/dosage question. None if not asked.

    SHARED across both engines, like the interaction refusal and for the same
    reason: there is no dosing dataset, so the only outputs possible are a
    deterministic pharmacist/label routing or a model-invented mg figure —
    and a hallucinated pediatric dose is the most dangerous sentence this
    product could emit. Fires on phrasing; NON_DRUG_TERMS keeps "how much
    water should I drink" on its normal path.
    """
    if risk == EMERGENCY:
        return None  # the emergency directive always wins
    try:
        term = extract_dose_query(message)
    except Exception:  # noqa: BLE001 — a parser must never break a reply
        logger.warning("dose extraction failed; continuing", exc_info=True)
        record_fail_open("drug_dosing")
        return None
    if term is None or (term and term.lower() in NON_DRUG_TERMS):
        return None

    t("Dose question",
      "deterministic check-the-label / ask-a-pharmacist reply "
      "(no dosing data, no LLM)")
    await _write_receipt(
        db, user_id=user_id, session_id=session_id,
        message=message, model_name=provider.model_name,
    )
    dose_reply = build_dose_refusal(term)
    if risk == HIGH:
        dose_reply = f"{escalation} {dose_reply}"
    return ChatResult(
        response_message=dose_reply,
        risk_level=risk,
        recommended_action="discuss_with_prescriber",
        provenance={"path": "drug_dose_query", "term": term or None},
        language=lang,
        trace=trace,
    )


async def _drug_info_reply(
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
    """Deterministic drug information from medicine_master. None if not asked.

    SHARED across both engines (it lived inside the legacy chain, and the
    agentic engine answered drug questions from its own weights — the same
    bypass class as the interaction refusal). Only at NONE risk (a HIGH turn
    needs the symptom path, not a product monograph); fail-open.
    """
    if risk != NONE:
        return None
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
        if term and drug is not None:
            t("Drug lookup",
              f"'{term}' matched {drug.name} in the validated medicines "
              "database — deterministic reply (no LLM)")
            # The reader's OWN medication allergies — nothing else on this
            # early-return path would carry them. Fail-open.
            warning = ""
            try:
                async with db.begin_nested():
                    warning = allergy_warning(
                        await medication_allergies(db, user_id)
                    )
            except Exception:  # noqa: BLE001
                logger.warning("allergy lookup failed; continuing", exc_info=True)
                record_fail_open("allergy_lookup")
            reply = build_drug_reply(drug, substitutes, allergy_warning=warning)
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
        logger.warning("drug lookup failed; continuing", exc_info=True)
        record_fail_open("drug_lookup")
    return None


async def _correlation_reply(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None,
    risk: str,
    message_risk: str,
    escalation: str,
    lang: str,
    trace: list[dict],
    t,
) -> ChatResult | None:
    """A deterministic co-occurrence readout for a two-metric question.

    SHARED across both engines, and it has to be. "does coffee affect my
    sleep" is a question a model will happily answer from its own weights with
    a causal sentence, and there is no dataset behind that sentence -- the same
    shape as the interaction and dosing refusals. Answering it deterministically
    is the only way both engines say the same non-causal thing.

    Placed AFTER the drug slots so a medication question never reaches here,
    and BEFORE the engine branch so the legacy tracker slot -- which used to
    swallow these and answer with a coffee total -- never sees them.

    Gated on THIS MESSAGE's triage, not on the turn's risk. "A reassuring
    number beside chest pain is a reason to delay care" is right for a red flag
    in the message being answered; it is wrong for a HIGH level merely CARRIED
    from an unresolved earlier episode, where it silently disabled the handler
    and handed "is my coffee the reason I sleep badly" to the model, which
    answered with the escalation banner and nothing else. Same call as "an
    unrelated data question mid-flow is released, not re-asked" (11e17be).
    The carried banner still leads the reply -- see `_lead`.

    Fail-open, in a SAVEPOINT: a rollup that is missing in a standalone
    deployment must leave the session usable for the RAG fallback.
    """
    if message_risk != NONE:
        return None
    try:
        async with db.begin_nested():
            ability = await handle_correlation_query(db, user_id, message)
    except Exception:  # noqa: BLE001 — a pattern read must never break a reply
        logger.warning("correlation read failed; continuing", exc_info=True)
        record_fail_open("correlation")
        # Fail open, but NOT back to the model. Returning None here would hand
        # the question to the RAG path, and a model asked "does coffee affect
        # my sleep" answers it causally from its own weights -- which is the
        # only reason this slot exists. Say the read failed instead.
        if parse_correlation_query(message) is None:
            return None
        ability = {
            "reply": CORRELATION_READ_FAILED,
            "action": "self_care",
            "provenance": {"path": "correlation_query", "degraded": "read_failed"},
        }
    if ability is None:
        return None
    prov = ability["provenance"]
    if prov.get("degraded"):
        t("Pattern check", "the lookup failed — said so, rather than guessing")
    elif prov.get("declined") == "medication":
        t("Pattern check",
          "the question named a medicine — routed to the prescriber rather "
          "than answered about something else")
    elif prov.get("declined"):
        t("Pattern check",
          f"{prov['input']} against a reading I have no daily series for — "
          "said which pairs I can do instead")
    else:
        t("Pattern check",
          f"{prov['input']} beside {prov['outcome']} over "
          f"{prov['window_days']} days — "
          + ("co-occurrence only, no cause claimed"
             if prov["enough"] else "not enough days; declined to compare"))
    await _write_receipt(
        db, user_id=user_id, session_id=session_id, message=message,
        model_name=provider.model_name,
    )
    return ChatResult(
        response_message=_lead(escalation, risk, ability["reply"]),
        risk_level=risk,
        recommended_action=_led_action(risk, ability["reply"], ability["action"]),
        provenance=prov,
        language=lang,
        trace=trace,
    )


async def _summary_reply(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID | None,
    risk: str,
    message_risk: str,
    escalation: str,
    lang: str,
    trace: list[dict],
    t,
) -> ChatResult | None:
    """The deterministic health summary. SHARED across both engines.

    It used to sit in the legacy ability chain only. On the agentic engine the
    model called `get_health_summary` and RECOMPOSED the answer from the tool
    payload -- and a summary is a dozen exact figures, so it is a dozen chances
    to lose the whole reply. Measured: a course recorded as "Dolo 650 Tablet"
    came back as "Dolo 650 mg", the model having invented a unit on a
    medication dose, which is precisely what the numeric-fidelity guard exists
    to catch. The guard fired, correctly, and threw away a summary that was
    otherwise entirely right.

    A handler whose whole value is being exact should not be recomposed at all.
    This is the same call `serve_extractive` makes for the corpus: the
    validated text IS the answer, so serve it verbatim and make no model call.

    Gated on THIS MESSAGE's triage, like `_correlation_reply` -- a carried
    episode floor leads the reply with its banner rather than suppressing it.

    PRECEDENCE. Hoisting it out of the legacy chain moved it ABOVE every
    handler that used to run first, and `_SUMMARY_RE` matches a bare
    "summary" -- so "summary of my blood pressure" stopped reaching
    `handle_metric_query`, "summary of my last blood test" stopped reaching
    the document handler, and "log 2 glasses of water for my health summary"
    stopped WRITING. Worst of all it outranked `handle_value_check`, the
    deterministic reference-range check that is deliberately first in the
    legacy chain, so a stated reading could be answered with a week of
    averages instead of being graded.

    A more specific parser therefore wins, exactly as it did before the
    hoist. The whole-health summary is the fallback, not the front door.
    """
    if message_risk != NONE or parse_summary_query(message) is None:
        return None
    # `parse_ai_result_query` is deliberately NOT in this list. It claims the
    # bare word "summary", so it matches "summarise my health" too, and its
    # HANDLER is what actually gates -- it answers only when the message
    # references a document. Consulting the parser here would hand every
    # whole-health ask to a handler that then declines, which is how the
    # summary stopped answering at all mid-fix.
    if (
        parse_stated_value(message) is not None
        or parse_tracker_add(message) is not None
        or parse_tracker_query(message) is not None
        or parse_metric_query(message) is not None
        or parse_document_query_fuzzy(message) is not None
        or parse_report_param_ask(message) is not None
        or parse_section_detail_query(message) is not None
    ):
        return None
    try:
        # SAVEPOINT: one missing core table must leave the session usable.
        async with db.begin_nested():
            ability = await handle_summary_query(db, user_id, message)
    except Exception:  # noqa: BLE001 — a summary must never break a reply
        logger.warning("health summary failed; continuing", exc_info=True)
        record_fail_open("health_summary")
        return None
    if ability is None:
        return None
    t("Health summary",
      "your own records, read and rendered deterministically (no LLM)")
    reply = _lead(escalation, risk, ability["reply"])
    verdict = validate_reply(reply, risk)
    provenance = dict(ability["provenance"])
    if not verdict.ok:
        t("Output validation",
          f"blocked ({redact_reason(verdict.reason)}) — replaced with the safe reply")
        reply = safe_reply(risk, session_id)
        provenance["degraded"] = "validation"
    else:
        t("Output validation", "passed all safety checks")
    await _write_receipt(
        db, user_id=user_id, session_id=session_id, message=message,
        model_name=provider.model_name,
    )
    return ChatResult(
        response_message=reply,
        risk_level=risk,
        recommended_action=_led_action(risk, ability["reply"], ability["action"]),
        provenance=provenance,
        visual=None if provenance.get("degraded") else ability.get("visual"),
        documents=ability.get("documents"),
        language=lang,
        trace=trace,
    )


async def _scope_with_carry_forward(
    db: AsyncSession,
    message: str,
    user_codes: set[str],
    prior_turns: list[dict],
) -> tuple[set[str], bool, set[str]]:
    """Condition scope for this turn, inheriting the topic on a follow-up.

    Returns ``(codes, message_named_condition, carried)``.

    Shared by BOTH dispatchers. It used to live inline in the legacy branch
    only, so on the agentic engine a bare follow-up resolved against the
    message alone — and `resolve_scope(db, "tell me more", set())` is empty,
    because the global fallback needs tokens of 5+ characters and "tell"/"more"
    are 4. The agentic engine therefore retrieved NOTHING for exactly the
    follow-ups this carry-forward exists to serve.

    Message-only scope first, then union the pedigree codes: the old shape
    called resolve_scope twice with the same message because it needed both
    answers, and deriving one from the other gives both for one call.
    """
    message_codes = await resolve_scope(db, message, set())
    # Key on "did THIS message name a condition" rather than on `codes`, which
    # is never empty for a user with a pedigree, so the topic carries for
    # everyone.
    message_named_condition = bool(message_codes)
    if message_named_condition:
        # The reader named their topic. Their pedigree is NOT also the topic.
        #
        # Unioning it in meant a reader whose father has hypertension asked
        # "what is type 2 diabetes" and got a scope of {T2DM, MC001, HTN,
        # MC051} — and `spread_across_conditions` then GUARANTEES the
        # off-topic code a slot out of k=4, because it exists to stop one
        # condition taking every slot. So the hypertension profile was
        # retrieved, put in the prompt, and cited, on a question that named
        # diabetes explicitly. Measured against the real corpus.
        #
        # Pedigree still reaches the answer — through the [P] block, which is
        # where the reader's own context belongs. It does not belong in the
        # retrieval scope of a question that named something else.
        codes = message_codes
    else:
        codes = await resolve_scope(db, "", user_codes)
    carried: set[str] = set()
    if not message_named_condition and prior_turns:
        recent_user_text = " ".join(
            m["message"] for m in prior_turns[-6:] if m.get("role") == "user"
        )
        carried = await resolve_scope(db, recent_user_text, set())
        if carried:
            codes = codes | carried
    return codes, message_named_condition, carried


async def _dispatch(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    provider: LLMProvider,
    session_id: uuid.UUID,
    pivot: InboundPivot | None = None,
    pending_med: dict | None = None,
    original_message: str | None = None,
) -> ChatResult:
    tr = triage(message)
    # With an ACTIVE pivot, `message` is the MT English and the floor would
    # otherwise be a function of MT phrasing — a paraphrase could lower it
    # (audit high). Run triage over the reader's ORIGINAL text too and take
    # the maximum: downstream may raise the floor, never lower it, and that
    # must hold across the translation boundary.
    if original_message is not None and original_message != message:
        tr_orig = triage(original_message)
        if LEVEL_ORDER[tr_orig.level] > LEVEL_ORDER[tr.level] or (
            tr_orig.self_harm and not tr.self_harm
        ):
            merged_terms = sorted(set(tr.matched_terms + tr_orig.matched_terms))
            tr = TriageResult(
                level=max_level(tr.level, tr_orig.level),
                matched_terms=merged_terms,
                self_harm=tr.self_harm or tr_orig.self_harm,
            )
    risk = tr.level
    # An UNRESOLVED red-flag episode raises this turn's floor.
    #
    # Live case: a reader had described chest pain with left-arm discomfort —
    # which `red_flags.py:178`'s ACS co-occurrence rule classes EMERGENCY — and
    # never said it settled. Days later they asked an educational question about
    # diabetes. Triage sees only the current message, so the turn was NONE, the
    # reply volunteered "how's the chest pain and left arm discomfort doing now
    # — fully settled, or still lingering?", and the recommended action was
    # `discuss_with_clinician`. `episodes.worst_level` existed to prevent exactly
    # this and was never called from anywhere.
    #
    # Capped at HIGH deliberately. Restoring the episode's own EMERGENCY would
    # fire the deterministic emergency directive on every later turn until the
    # reader said they were better — including "what is diabetes" — which trains
    # people to ignore it. HIGH gives the escalation banner and
    # `seek_care_promptly` without hijacking the turn. Raising only: an episode
    # can never LOWER a floor the current message set.
    #
    # And it does NOT fire on every turn. Reported by a reader: the escalation
    # sentence led every single reply, including answers to "what is
    # prediabetes", long after they had first been told. Repeating an urgent
    # instruction on an encyclopedia question is how it stops being read — the
    # same argument that capped this at HIGH in the first place.
    #
    # So the floor is skipped when THIS message is a pure corpus lookup: a
    # reader asking what a word means is not in a moment where re-escalating
    # helps. It still fires on anything personal, any symptom report, any
    # follow-up — the turns where it can actually change what they do. The
    # episode stays open either way and stays in the [P] block, so the model
    # can still raise it in its own words when it is relevant.
    #
    # Note the ordering: `validate_reply` REQUIRES an escalation directive at
    # HIGH risk, so "keep the risk but drop the banner" is not available. The
    # only way not to repeat the sentence is not to raise the floor.
    episode_floor = NONE
    _open: list | None = None
    _corpus_lookup = (
        is_definitional_ask(message) and not is_personal_health_query(message)
    )
    # Nor on the turn where they tell us they are better. The episodes are
    # closed later in THIS turn, by `memory_assembly.record` — but the floor
    # is read here, before that, so a recovery report used to be answered
    # with "you mentioned something earlier ... seek medical care promptly".
    # Observed in staging: the reader said "i am feeling better now" and was
    # escalated at in the same breath.
    _recovery = is_recovery_message(message, has_red_flag=bool(tr.matched_terms))
    # Nor on a question that is not about their health at all.
    #
    # Reported twice by the owner: "its not necessary to keep on repeating the
    # same thing again and again... for every message its not good". An open
    # episode was leading EVERY reply with "you mentioned something earlier
    # that can be serious" — including "how much water this week" and "show my
    # latest lab reports", which have nothing to do with it. Repeated on every
    # turn the sentence stops being a warning and becomes noise, which is the
    # opposite of what a safety banner is for.
    #
    # It still leads whenever the turn IS about their health: a symptom named
    # in this message (`tr.matched_terms`), or a question about their own
    # wellbeing. So symptoms reported one after another still accumulate and
    # still escalate — that is the case the banner exists for — while a
    # tracker lookup in between is answered as the ordinary question it is.
    #
    # The episode itself is untouched: still open, still recorded, still
    # raising the floor the moment the conversation returns to it.
    _unrelated_ask = (
        not tr.matched_terms and not is_personal_health_query(message)
    )
    try:
        _open = await open_episodes(db, user_id)
        if (
            not _corpus_lookup
            and not _recovery
            and not _unrelated_ask
            and LEVEL_ORDER[episodes_worst_level(_open)] >= LEVEL_ORDER[HIGH]
        ):
            episode_floor = HIGH
    except Exception:  # noqa: BLE001 — memory must never break a reply
        logger.warning("open-episode floor failed; using triage only", exc_info=True)
        record_fail_open("episode_floor")
    # Which banner the reply leads with. When the floor comes from an
    # unresolved EARLIER episode and this message raised nothing itself,
    # "some of what you describe" is false — see CARRIED_ESCALATION.
    escalation = (
        CARRIED_ESCALATION
        if episode_floor == HIGH and LEVEL_ORDER[risk] < LEVEL_ORDER[HIGH]
        else HIGH_ESCALATION
    )
    risk = max_level(risk, episode_floor)
    # Every reply composes in English; when the pivot is active the sidecar
    # translates the final text into the user's language and script. lang is
    # only reported to the client and drives the no-sidecar LLM directive.
    lang = pivot.display_language if pivot is not None else detect_language(message)

    trace: list[dict] = []
    # Wall-clock since the turn began, stamped on every trace step. Without
    # this the trace says WHAT happened and never WHERE the time went, so a
    # 30-second turn and a 300ms one are indistinguishable after the fact.
    # Staging measured 19-33s on LLM paths against 0.1-5s on deterministic
    # ones, and nothing in the response said which stage was responsible.
    _t0 = time.perf_counter()

    def t(step: str, detail: str) -> None:
        trace.append({
            "step": step,
            "detail": detail,
            "ms": round((time.perf_counter() - _t0) * 1000),
        })

    if tr.matched:
        t("Safety triage",
          f"{risk.upper()} — matched: {', '.join(repr(m) for m in tr.matched_terms[:4])}")
    elif episode_floor != NONE:
        # Saying "no red flags detected" beside a seek-care banner reads as a
        # contradiction and hides WHY the turn escalated. This message raised
        # nothing; an earlier unresolved one did.
        t("Safety triage",
          f"{risk.upper()} — nothing in this message, carried from an earlier "
          "symptom you have not said has settled")
    else:
        t("Safety triage", "no red flags detected")
    t("Language", LANGUAGE_NAMES.get(lang, lang))

    # 0.9) Deterministic medication add/stop/remove/list. SHARED across both
    #      engines and placed HERE — after the triage floor (so an emergency
    #      typed mid-flow still wins) but BEFORE the scope guard (so a bare
    #      answer like "yes, morning and night" is not declined as off-topic).
    #      The whole transaction is deterministic so it completes every time,
    #      instead of the model sometimes deflecting a medication write.
    if risk == NONE:
        med = await handle_medication_turn(
            db, user_id, message, pending_med, provider)
        if med is not None:
            t("Medication flow", f"deterministic — {med.get('action')}")
            await _write_receipt(
                db, user_id=user_id, session_id=session_id, message=message,
                model_name=provider.model_name,
            )
            return ChatResult(
                response_message=med["reply"],
                risk_level=NONE,
                recommended_action=med.get("action", "medication_flow"),
                provenance={**med.get("provenance", {}),
                            "pending_med": med.get("pending_med")},
                language=lang,
                trace=trace,
            )

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
            db, user_id, codes=(), flags=tr.matched_terms, risk=risk,
            message=message,
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
        db, user_id, message, provider, session_id, risk, lang, trace, t,
        escalation,
    )
    if combination is not None:
        return combination

    # 3.45) Dose/dosage questions. SHARED for the same reason as 3.4: no
    #       dosing dataset exists, so a model answer would be invented — and a
    #       hallucinated pediatric dose is the worst sentence this product
    #       could produce.
    dosing = await _dosing_refusal(
        db, user_id, message, provider, session_id, risk, lang, trace, t,
        escalation,
    )
    if dosing is not None:
        return dosing

    # 3.46) Drug-information questions — deterministic from medicine_master,
    #       SHARED so neither engine ever answers them from model weights.
    drug_info = await _drug_info_reply(
        db, user_id, message, provider, session_id, risk, lang, trace, t
    )
    if drug_info is not None:
        return drug_info

    # 3.47) Two-metric "does X affect my Y" questions — a deterministic
    #       co-occurrence readout, SHARED for the same reason as 3.4 and 3.46.
    #       It sits here rather than in the legacy chain because
    #       `parse_tracker_query` matched these first and answered them as a
    #       coffee total: a handler at or after the tracker slot would be dead
    #       on arrival for the exact phrasings it exists to serve.
    correlation = await _correlation_reply(
        db, user_id, message, provider, session_id, risk, tr.level,
        escalation, lang, trace, t
    )
    if correlation is not None:
        return correlation

    # 3.48) The health summary — deterministic, SHARED, and served VERBATIM.
    #       It lived in the legacy ability chain, so the agentic engine let the
    #       model recompose it and one reformatted dose discarded the whole
    #       answer. Hoisted rather than duplicated: no parser ahead of it in
    #       that chain claims a summary phrasing, so legacy routing is
    #       unchanged and both engines now emit identical text.
    summary = await _summary_reply(
        db, user_id, message, provider, session_id, risk, tr.level,
        escalation, lang, trace, t
    )
    if summary is not None:
        return summary

    # 3.5) Engine selection. Everything above — the triage floor, the scope
    #      guard, the emergency directive, the canned conversational replies
    #      and the drug-combination refusal — is SHARED and has already run,
    #      so the agentic engine can never see an emergency and the model is
    #      never the arbiter of one.
    if get_settings().chat_engine == "agentic":
        t("Engine", "agentic — the assistant can look things up for itself")
        return await _dispatch_agentic(
            db, user_id, message, provider, session_id, tr, risk, lang,
            trace, t, pivot=pivot, episodes=_open, escalation=escalation,
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
                # Medication add/stop/remove is NOT handled here any more: the
                # deterministic flow in the SHARED prologue (medication_flow)
                # owns it on both engines, with guards this chain's regex
                # parser never had — question framing, third-party, dose
                # changes, conditionals, self-harm. A message that the flow
                # RELEASED must not be re-parsed by a weaker parser: that is
                # how "Can I stop taking my metformin?" became a real write.
                if ability is None:
                    # AFTER tracker_add: "log 2 glasses of water" and "how much
                    # water did I drink" share every noun and differ only in
                    # framing, so the WRITE must get first refusal.
                    ability = await handle_tracker_query(db, user_id, message)
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
                # (handle_summary_query is NOT called here any more: the
                # SHARED prologue owns it on both engines — see 3.48.)
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
                    # A deterministic handler answered from the reader's
                    # OWN rows. Only the suggestions handler renders
                    # corpus text, and it hands back exactly the chunks
                    # it rendered — everything else declares nothing.
                    used=used_rendered(ability.get("used_chunks") or ()),
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

    # (Step 5, the deterministic drug-information reply, moved to the SHARED
    # prologue — the agentic engine used to bypass it entirely and answer
    # drug questions from model weights, the audit's engine-parity bug class.)

    # 6) Symptom / educational RAG path (risk is none or high here).
    patient_text, user_codes = await _stage(
        "patient_context", build_patient_context(db, user_id)
    )
    # For PERSONAL-symptom questions ("why am I so tired?"), enrich the [P]
    # block with the reader's own recorded data so the answer can be correlated
    # with their lifestyle/vitals/medications — as things to discuss with a
    # clinician, never as a diagnosis or a stated cause (prompt + validator
    # enforce that). General education questions stay lean (no private data).
    if is_personal_health_query(message):
        try:
            snapshot = await _stage(
                "health_snapshot", build_health_snapshot(db, user_id)
            )
            if snapshot:
                patient_text = (
                    f"{patient_text}\n\n{snapshot}" if patient_text else snapshot
                )
        except Exception:  # noqa: BLE001 — enrichment must never break a reply
            logger.warning("health snapshot failed; continuing", exc_info=True)
            record_fail_open("health_snapshot")

    # Per-user memory (profile, open episodes, past topics), read once through
    # the SHARED assembly so both engines see all of it.
    _memory = await _stage(
        "memory_assembly",
        memory_assembly.assemble(db, user_id, episodes_hint=_open),
    )

    # Short-term memory: recent verbatim turns drive follow-up resolution. The
    # last entry is the current message (already persisted) — the PRIOR turns
    # are the conversational context.
    compacted_summary, recent = await _stage(
        "session_context", assemble_context(db, session_id)
    )
    prior_turns = recent[:-1] if recent else []

    # Message-only scope FIRST, then union the pedigree codes in. The old
    # shape called resolve_scope twice with the same message — a duplicate
    # registry match and an extra round trip — because it needed both answers.
    # Deriving one from the other gives both for one call.
    codes, message_named_condition, carried = await _stage(
        "resolve_scope",
        _scope_with_carry_forward(db, message, user_codes, prior_turns),
    )
    if carried:
        t("Follow-up", f"carried topic scope from recent turns: "
          f"{sorted(carried)[:4]}")
    chunks = await _stage("retrieval", retrieve_chunks(db, codes, message))
    used_rag = bool(chunks)
    # Why this turn fell back, if it did. The agentic path has always recorded
    # this; the legacy path did not, which left the degradation metric blind on
    # the engine that currently answers real users. Declared here because both
    # the extractive branch and the main RAG branch set it.
    legacy_degraded: str | None = None
    # What the ANSWER used — set where the answer is, in both branches
    # below. Left empty, this turn cites nothing.
    legacy_used = Used()
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
        db, user_id, codes=codes, flags=tr.matched_terms, risk=risk,
        message=message,
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
        # A question that names a section gets that section only. The menu is
        # gated on NONE: HIGH_ESCALATION is prepended to whatever comes back,
        # and inviting the reader to browse the corpus underneath an
        # urgent-care instruction would undercut it.
        _focused = is_focused(chunks, target_sections(message))
        extractive = build_extractive_answer(
            chunks, focused=_focused, with_menu=(risk == NONE)
        )
        if extractive is not None:
            t("Generate",
              "answered directly from the clinically validated profile "
              "content (no model call)")
            display = extractive
            if risk == HIGH:
                display = f"{escalation} {display}"
            verdict = validate_reply(display, risk)
            if not verdict.ok:
                t("Output validation",
                  f"blocked ({redact_reason(verdict.reason)}) — replaced with the safe reply")
                display = safe_reply(risk, session_id)
                legacy_degraded = "validation"
            else:
                t("Output validation", "passed all safety checks")
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
                # Exactly what the renderer emitted, never the retrieved
                # set. (A replaced reply is dropped in handle_chat.)
                used=used_rendered(rendered_chunks(chunks, focused=_focused)),
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
    # ALWAYS English (audit high / policy): the validator's guarantees — no
    # diagnosis, no provider leak, no reassurance-at-high — only work on
    # English text. With an active pivot the sidecar translates the VALIDATED
    # English; without one, handle_chat appends a fixed native-language
    # notice (app/i18n/notices.py) after validation.
    directive = language_directive("en")
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
            legacy_used = used_cited(grounded_answer, chunks, patient_text)
            display = strip_markers(grounded_answer)
            if risk == HIGH:
                display = f"{escalation} {display}"
            # Numeric fidelity — the SAME ladder the agentic engine has always
            # run, on the engine that answers real users (audit high: drifted
            # lab values, misquoted readings and invented doses passed here
            # unchecked while the non-default engine would have caught them).
            fid_sources = [c.content for c in chunks]
            if patient_text:
                fid_sources.append(patient_text)
            fid_ok, stray = values_traceable(display, fid_sources)
            if not fid_ok:
                logger.warning(
                    "numeric fidelity failure (legacy): %d stray value(s)",
                    len(stray),
                )
                t("Value check",
                  "a stated value did not match your records — replaced with "
                  "the safe reply")
                display = safe_reply(risk, session_id)
                legacy_degraded = "fidelity"
            elif not fid_sources and unit_values(display):
                # Nothing retrieved, no patient context, yet the reply states
                # a clinical value or dose — nothing stands behind it.
                logger.warning(
                    "ungrounded clinical value with no sources (legacy): "
                    "%d value(s)", len(unit_values(display)),
                )
                t("Value check",
                  "a dose or measurement was stated with nothing to support "
                  "it — replaced with the safe reply")
                display = safe_reply(risk, session_id)
                legacy_degraded = "ungrounded_value"
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
        used=legacy_used,
        language=lang,
        trace=trace,
    )


async def _cite(
    db: AsyncSession, used: Used | None
) -> tuple[list[dict] | None, list[str]]:
    """The ONE place a citation is produced — plus the chunk ids behind it.

    Both halves come out of the same `Used`, so provenance and citations
    cannot disagree: there is no second slice for them to drift apart on.
    """
    if used is None or not used.markers:
        return None, []
    try:
        index = await load_condition_index(db)
    except Exception:  # noqa: BLE001 — citations must never break a reply
        index = None
    out: list[dict] = []
    ids: list[str] = []
    for marker in used.markers:
        if marker == "P":
            if used.patient:
                out.append(
                    {"marker": "P", "source": "patient_context",
                     "display_name": "Your health record"}
                )
            continue
        if marker == "GK":
            # Only when nothing WAS retrieved. The entry asserts a fact about
            # retrieval, and emitting it unconditionally put "General
            # knowledge (nothing retrieved)" in the same payload as
            # `provenance.chunks = [3 ids]` -- a direct contradiction, and the
            # counterexample to this function's own docstring. `log` mode is
            # the default and ships the answer, so it reached the client.
            if not used.chunks:
                out.append(
                    {"marker": "GK", "source": "general_knowledge",
                     "display_name": "General knowledge (nothing retrieved)"}
                )
            continue
        i = int(marker) - 1
        if not (0 <= i < len(used.chunks)):
            continue  # the model invented a block number
        chunk = used.chunks[i]
        if chunk.id in ids:
            continue
        display = chunk.condition_code
        if index is not None and chunk.condition_code in index.by_code:
            display = index.by_code[chunk.condition_code].display_name
        section = _base_section(chunk.chunk_type)
        ids.append(chunk.id)
        out.append(
            {
                "marker": marker,
                "source": "mcp_master_profile",
                "chunk_id": chunk.id,
                "condition_code": chunk.condition_code,
                "section": chunk.chunk_type,
                "display_name": display,
                # A client that renders one field showed "MC051" four times.
                # Give it something a reader can actually read.
                "label": f"{display} — {section.replace('_', ' ')}",
            }
        )
    return (out or None), ids


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
    # The in-flight medication draft (if any) from the last assistant turn, so
    # the deterministic flow resumes across turns with no new table.
    pending_med = await last_pending_med(db, session_id)
    await add_message(
        db, session_id, "user", message,
        extracted_intent={
            "risk": triage(work).level,
            # The English pivot, when active — compaction extracts from it.
            **({"english": work} if pivot.active else {}),
        },
    )
    # Every path converges here, so this is the one place that sees the whole
    # turn: how long it took, which engine ran it, and whether the reader got
    # a real answer or a fallback. Label values come from bounded sets only —
    # never the message, the user, or a condition name.
    engine = get_settings().chat_engine
    with timed(chat_latency, engine=engine):
        result = await _dispatch(
            db, user_id, work, provider, session_id, pivot=pivot,
            pending_med=pending_med,
            original_message=message if pivot.active else None,
        )

    chat_turns.inc(engine=engine, risk=result.risk_level)
    degraded = result.provenance.get("degraded")
    # Citations are built HERE and nowhere else, from what the path
    # DECLARED it used. A reply that was replaced (any degrade reason,
    # either engine) shows none of that content, so it cites none of it —
    # one rule in one place, instead of five call sites each remembering
    # to pass None.
    result.citations, _used_ids = await _cite(
        db, None if degraded else result.used
    )
    # Every reply states what it used; [] is "nothing from the corpus".
    result.provenance["used_chunks"] = _used_ids
    if degraded:
        # THE number that says whether the system is quietly answering badly.
        degradations.inc(engine=engine, reason=str(degraded))
    # (tool_calls is incremented once per execution in the tool registry,
    # with an outcome label — a second label-less increment here doubled
    # every count.)
    usage = result.provenance.get("usage") or {}
    for direction in ("input_tokens", "output_tokens"):
        if usage.get(direction):
            llm_tokens.inc(usage[direction], direction=direction)
    if (not pivot.active and result.response_message
            and (notice := english_fallback_notice(lang_hint(pivot, message)))):
        # Reader wrote in a language we cannot serve right now: the reply is
        # validated English plus ONE fixed sentence in their language saying
        # so. Appended after validation — a constant cannot be corrupted.
        result.response_message = f"{result.response_message}\n\n{notice}"
        result.provenance["translation"] = {
            "language": lang_hint(pivot, message), "status": "english_notice",
        }
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
    pending_next = result.provenance.get("pending_med")
    if result.documents or result.recommended_action or pending_next:
        assistant_meta = {}
        if result.documents:
            assistant_meta["documents"] = result.documents
        if result.recommended_action:
            assistant_meta["action"] = result.recommended_action
        # Carry the in-flight medication draft to the next turn (deterministic
        # flow state — see app/chat/medication_flow.py). Absent once the flow
        # completes or is abandoned, so it self-clears.
        if pending_next:
            assistant_meta["pending_med"] = pending_next
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
    pivot: InboundPivot | None = None,
    episodes: list | None = None,
    escalation: str = HIGH_ESCALATION,
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
    _memory = await memory_assembly.assemble(db, user_id, episodes_hint=episodes)
    patient_text = _memory.append_to(patient_text)

    compacted, recent = await assemble_context(db, session_id)
    prior_turns = recent[:-1] if recent else []

    codes, _named, _carried = await _scope_with_carry_forward(
        db, message, user_codes, prior_turns
    )
    chunks = await retrieve_chunks(db, codes, message)

    # A question the validated profile answers VERBATIM does not need a model.
    #
    # `serve_extractive` has existed in the legacy branch since Phase 4 and the
    # agentic engine never reached it: `_dispatch` returns into this function
    # ~230 lines ABOVE it, so this engine re-implemented the expensive half of
    # that path (retrieval, memory, prompt assembly) and skipped the free half.
    # Measured: "what is diabetes" costs 4,291 prompt tokens and TWO sequential
    # model calls here, of which 3,601 tokens cannot contribute to the answer —
    # while the extractive renderer produces a clinician-reviewed answer from
    # the same chunks in 21 ms with no model call at all.
    #
    # The `is_focused` conjunct is load-bearing and is NOT in the legacy gate:
    # without it a definitional-SHAPED question whose answer is not a profile
    # section ("is diabetes curable") would be served a mismatched section with
    # no model left to notice. `_prefer_section` fails open, so `bool(chunks)`
    # alone does not mean the corpus actually holds the asked-for section.
    #
    # Deliberately NOT including legacy's `llm_provider == "fake"` arm: that
    # would route every fake-provider test through here and leave the agentic
    # loop untested.
    if (
        risk == NONE
        and chunks
        and _named
        and is_definitional_ask(message)
        and not is_personal_health_query(message)
        and is_focused(chunks, target_sections(message))
    ):
        extractive = build_extractive_answer(
            chunks, focused=True, with_menu=True
        )
        if extractive is not None:
            t("Generate",
              "answered directly from the clinically validated profile "
              "content (no model call)")
            verdict = validate_reply(extractive, risk)
            if verdict.ok:
                await memory_assembly.record(
                    db, user_id, codes=codes, flags=tr.matched_terms,
                    risk=risk, message=message,
                )
                await _write_receipt(
                    db, user_id=user_id, session_id=session_id,
                    message=message, model_name="extractive",
                    retrieved=[c.to_dict() for c in chunks],
                    grounding=None, grounding_status="extractive",
                    used_rag=True,
                )
                t("Output validation", "passed all safety checks")
                return ChatResult(
                    response_message=extractive,
                    risk_level=risk,
                    recommended_action="discuss_with_clinician",
                    provenance={
                        "path": "agentic",
                        "mode": "extractive",
                        "used_rag": True,
                        "conditions": sorted(codes),
                        "chunks": [c.id for c in chunks],
                    },
                    # Exactly what the renderer emitted. The legacy twin
                    # has used `rendered_chunks` since Phase 4; this
                    # branch cited the whole retrieved set instead.
                    used=used_rendered(
                        rendered_chunks(chunks, focused=True)
                    ),
                    language=lang,
                    trace=trace,
                )
            # Validation failed on clinician-reviewed text: fall through to the
            # model rather than serving a safe reply, since the model may yet
            # compose something that passes.

    # Remember what was raised, at the severity the FLOOR decided — never a
    # severity the model inferred. Placed HERE, before generation, to match
    # legacy: what the reader raised does not depend on whether the reply
    # succeeded. Recording it after the guards (as this used to) meant an
    # agent-loop failure silently forgot the symptom they just described.
    await memory_assembly.record(
        db, user_id, codes=codes, flags=tr.matched_terms, risk=risk,
        message=message,
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
    # Same rule as the legacy engine: ALWAYS English — see the legacy site.
    # (An earlier fix had the model reply in the reader's language when no
    # sidecar was up; that produced replies no validator could check, which
    # the audit rated a high. The validated-English-plus-native-notice policy
    # replaces it, identically on both engines.)
    directive = language_directive("en")
    # Split, not joined: element 0 is byte-identical across every turn and
    # carries the prompt-cache breakpoint in the Anthropic adapter. Every
    # other provider joins it straight back, so the model is told exactly the
    # same thing either way — only the billing differs.
    #
    # The language directive belongs in the VOLATILE half: it changes with the
    # reader's language, and a per-reader prefix caches for nobody.
    system: list[str] = [stable, "\n\n".join(p for p in (volatile, directive) if p)]

    # Charts the tools rendered. They travel OUT OF BAND -- an SVG is prompt
    # the model cannot read, but the client still needs it, and until now the
    # agentic engine built the chart, paid for it in tokens and then dropped
    # it, so the same question returned a chart on legacy and none here.
    tool_visuals: list[dict] = []
    # Corpus blocks a TOOL retrieved and rendered itself
    # (`get_condition_guidance` runs its own scoped retrieval). They
    # never reach the numbered blocks, so the model cannot cite them
    # with [n] — but the reply quotes them, so they are what it used.
    tool_sources: list[RetrievedChunk] = []
    # Document cards a tool built. Out of band for the SAME reason as the SVG:
    # the card is client plumbing (`resource_type`, row id) the model cannot
    # use, and the handler's own `deterministic_reply` already names every
    # title and date. They used to be a `_PASSTHROUGH` — i.e. straight into the
    # prompt — and the agentic terminal then attached NONE of them, so the
    # reader got a list of reports with no button to open any of them.
    tool_documents: list[dict] = []

    async def _executor(call):
        return await execute_tool(
            db, user_id, call, session_id,
            visuals=tool_visuals, sources=tool_sources,
            documents=tool_documents,
        )

    # Tools are offered when THIS MESSAGE raised no red flag. A red flag in the
    # message being answered stays on the safe path so nothing can delay or
    # dilute an escalation — but a HIGH merely CARRIED from an unresolved
    # earlier episode left the model with no records access on an ordinary data
    # question, and the reader got the banner and no answer. The banner still
    # leads the reply (`_lead`).
    offered = TOOL_SPECS if tr.level == NONE else ()

    # Say which of the two it actually is. The old line claimed records access
    # unconditionally, including on the red-flag turns where `offered` is empty
    # — the trace is what a reader (and an on-call engineer) diagnoses from.
    t("Generate",
      "asking the assistant, with access to your records" if offered
      else "asking the assistant — no records access on a red-flag turn")
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
    used = used_plus(
        used_cited(outcome.text, chunks, patient_text), tool_sources
    )
    # Progressive disclosure, on BOTH engines. The legacy renderer appends this
    # inside build_extractive_answer; the agentic engine has no renderer of its
    # own, so it is appended here — before the fidelity, validation and HIGH
    # branches, so the menu is subject to exactly the same guards as the rest
    # of the reply rather than being bolted on after them.
    #
    # Four gates, each earning its place:
    #   `display`  — an empty model turn (a refusal, or text that was only
    #                citation markers) must stay empty so validate_reply's
    #                `empty` rule fires and safe_reply takes over. Appending
    #                first turned "" into a menu-only reply that PASSED
    #                validation, so the reader got a browse list and no answer
    #                while `degradations` never incremented.
    #   corpus-ask — mirrors the legacy `serve_extractive` gate, so the menu
    #                does not land on a tracker write or a personal-records
    #                answer that merely happened to retrieve chunks.
    #   is_focused — the section filter actually matched something.
    #   risk NONE  — never under the HIGH banner.
    _sections = target_sections(message)
    if (
        display
        and risk == NONE
        and chunks
        and is_definitional_ask(message)
        and not is_personal_health_query(message)
        and is_focused(chunks, _sections)
    ):
        # Exclude the sections that were actually RETRIEVED, not the ones the
        # question asked for. Legacy excludes what it rendered; the agentic
        # engine cannot know what the model wrote, but the retrieved set is a
        # far better proxy than the target set — a compound ask whose second
        # half never reached the prompt would otherwise be hidden from the menu
        # despite never being shown.
        _shown = {
            _base_section(c.chunk_type)
            for c in chunks
            if _base_section(c.chunk_type) in _sections
            or c.chunk_type in _sections
        }
        _menu = disclosure_menu(_shown)
        if _menu:
            display = display + "\n\n" + _menu
    display = _lead(escalation, risk, display)

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
        nonlocal display, used
        rewritten = await recover(
            provider, system, outcome.messages, reason, detail
        )
        if not rewritten:
            return False
        candidate = _lead(escalation, risk, strip_markers(rewritten))
        retry_ok, _ = values_traceable(candidate, sources)
        if not retry_ok:
            return False
        if not sources and unit_values(candidate):
            return False
        if not validate_reply(candidate, risk, extra_terms).ok:
            return False
        display = candidate
        used = used_plus(
            used_cited(rewritten, chunks, patient_text), tool_sources
        )
        return True

    # Fidelity FIRST: a drifted lab value is worse than a blocked reply, and it
    # is the failure mode the validator cannot see.
    sources = [*outcome.source_texts, *(c.content for c in chunks)]
    if patient_text:
        sources.append(patient_text)
    ok, stray = values_traceable(display, sources)
    if not ok:
        logger.warning("numeric fidelity failure: %d stray value(s)", len(stray))
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
        logger.warning(
            "ungrounded clinical value with no sources: %d value(s)", len(stated)
        )
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
        # What retrieval PUT IN FRONT of the model, matching legacy. Citations
        # are marker-derived, and `_GROUNDING_RULES` only requires a marker on
        # a sentence stating a clinical value — so an ordinary educational
        # answer is legitimately marker-free, and without this key a
        # corpus-derived reply left NO client-visible trace that the corpus
        # was consulted at all. That is the reader-reported bug the deleted
        # `_agentic_citations` was written to fix.
        "chunks": [c.id for c in chunks],
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
        used=used,
        visual=None if degraded else (tool_visuals[0] if tool_visuals else None),
        documents=None if degraded else (tool_documents or None),
        language=lang,
        trace=trace,
    )

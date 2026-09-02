"""Executors wrapping the existing handlers, returning structured data.

Each returns a JSON-serialisable dict (or ``None`` for "nothing found")
carrying BOTH the structured facts and the handler's own validator-safe
wording under ``deterministic_reply``.

That second field is the point. The handler's phrasing has been through the
output validator and, for the clinical paths, clinician review; the model is
told to prefer it verbatim when it answers the question on its own, and to
compose its own wording only when it needs to COMBINE facts from more than one
tool. It also gives the numeric-fidelity guard something to check against —
every value in the reply must appear in a tool result.

No exception handling here. app/chat/tools/registry.py owns the SAVEPOINT and
the fail-closed contract, so a handler that raises is reported to the model as
a failed call rather than killing the turn.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.abilities import (
    DocumentQuery,
    StatedValue,
    SummaryQuery,
    find_relation,
    normalize_document_kinds,
    tracker_query_for,
)
from app.chat.context import build_patient_context
from app.chat.data_handlers import (
    _NO_WEARABLE_RANGE,
    _WEARABLE_GRADE_RE,
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
    handle_tracker_query,
    handle_value_check,
    perform_medication_write,
)
from app.chat.tools.definitions import VALUE_CHECK_METRICS
from app.coredata.service import document_owner
from app.drugs.service import build_drug_reply, find_drug, find_substitutes
from app.rag.extractive import (
    build_extractive_answer,
    is_focused,
    rendered_chunks,
)
from app.rag.retrieval import (
    resolve_scope,
    retrieve_chunks,
    target_sections,
)

# Keys a handler may return that the model can use directly.
#
# "visual" is NOT one of them. A rendered chart is ~3.3 KB of SVG — roughly
# 900 prompt tokens on a high-traffic tool — that the model cannot read and
# has no use for, and its numbers are a fidelity trap besides: the SVG stores
# a bar as `58</text>` while the values list holds `58.0`, so a model quoting
# its own chart trips the guard and has its whole reply replaced. It travels
# OUT OF BAND instead, under `_visual`, which execute_tool lifts off the
# payload before serialising and hands to the caller for the ChatResult.
_PASSTHROUGH = ("documents",)

#: Payload keys carrying data for the caller, never for the model.
OUT_OF_BAND_VISUAL = "_visual"
#: Corpus chunks the handler RENDERED — the caller cites these. They used
#: to travel as a "citations" passthrough, i.e. straight into the prompt,
#: where the model spent tokens on them and the caller never saw them.
OUT_OF_BAND_SOURCES = "_sources"


def _unwrap(ability: dict | None, **extra) -> dict | None:
    """Handler result -> tool payload."""
    if ability is None:
        return None
    payload: dict = {
        "deterministic_reply": ability["reply"],
        "provenance": ability.get("provenance", {}),
        **extra,
    }
    for key in _PASSTHROUGH:
        if ability.get(key):
            payload[key] = ability[key]
    if ability.get("visual"):
        payload[OUT_OF_BAND_VISUAL] = ability["visual"]
    if ability.get("used_chunks"):
        payload[OUT_OF_BAND_SOURCES] = ability["used_chunks"]
    return payload


async def get_latest_metric(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    metric = str(args.get("metric", "")).strip()
    if not metric:
        return None
    # The tool description tells the model to send underscore keys
    # ("blood_pressure"), and this parser reads English. Without the swap a
    # reader WITH a reading on file was told there was none -- the same bug
    # already fixed in check_value_against_range, left standing in its sibling.
    spoken = metric.replace("_", " ")
    ability = await handle_metric_query(db, user_id, f"what is my latest {spoken}")
    if ability is None:
        return None
    prov = ability.get("provenance", {})
    return _unwrap(
        ability,
        metric=prov.get("metric", metric),
        value=prov.get("value_text"),
        recorded=prov.get("recorded"),
        source=prov.get("source"),
        found=prov.get("found", True),
    )


async def get_report_parameter(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    param = str(args.get("parameter", "")).strip()
    if not param:
        return None
    ability = await handle_report_param_ask(db, user_id, f"what is my {param}")
    return _unwrap(ability, parameter=param)


async def get_documents(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    # Pass the tool's own structured arguments straight through. This used to
    # rebuild an English sentence ("show me report") and re-parse it, but the
    # parser demands an ownership marker, so EVERY call parsed to None and the
    # tool returned nothing -- on the agentic engine that is every document
    # request the reader makes.
    who = str(args.get("relation") or "").strip().lower()
    named = str(args.get("owner_name") or "").strip()
    relation = find_relation(f"my {who}") if who else None
    query = DocumentQuery(
        kinds=normalize_document_kinds(args.get("kinds")),
        relation=relation,
        # A relation word we do not recognise is treated as a name, so the
        # family lookup can answer "no such connected member" instead of
        # quietly showing the reader their own documents.
        owner_name=named or (who if who and relation is None else None),
    )
    ability = await handle_document_query(db, user_id, "", query=query)
    return _unwrap(ability, asked_about=who or named or "you")


async def check_value_against_range(
    db: AsyncSession, user_id: uuid.UUID, args: dict, session_id
) -> dict | None:
    metric = str(args.get("metric", "")).strip().lower().replace(" ", "_")
    value = args.get("value")
    if not metric or value is None:
        return None
    # Off-enum, whatever the schema says. A wearable term here is the refusal;
    # anything else is a metric with no reference range, which is the same
    # answer for a different reason.
    if metric not in VALUE_CHECK_METRICS or _WEARABLE_GRADE_RE.search(metric):
        # Davi has no reference ranges for wearable metrics and the client
        # contract forbids putting a band or grade on one. A sentence in the
        # tool description is not a guard -- this repo's own recurring lesson.
        return {
            "graded": False,
            "metric": metric,
            "deterministic_reply": _NO_WEARABLE_RANGE["reply"],
            "note": (
                "There is no reference range for wearable readings. Report "
                "the figure; do not call it high, low, normal or reassuring."
            ),
        }
    secondary = args.get("secondary")
    # STRUCTURED, not a synthesised English sentence for `parse_stated_value`
    # to re-read: "my blood_sugar is 117" resolved to nothing, and "my random
    # glucose is 130" resolved to the FASTING band. Same reason
    # `handle_tracker_query` and `handle_summary_query` take a parsed query.
    ability = await handle_value_check(
        db, user_id, "", session_id,
        stated=StatedValue(
            metric=metric,
            value=float(value),
            secondary=float(secondary) if secondary is not None else None,
        ),
    )
    return _unwrap(ability, metric=metric, value=value, secondary=secondary)


async def log_lifestyle_entry(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    kind = str(args.get("kind", "")).strip()
    quantity = args.get("quantity")
    if not kind or quantity is None:
        return None
    days = int(args.get("days_ago") or 0)
    when = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
    ability = await handle_tracker_add(
        db, user_id, f"I had {quantity} {kind} {when}"
    )
    return _unwrap(ability, kind=kind, quantity=quantity, days_ago=days)


async def get_health_summary(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    # Structured argument straight through, as get_documents and
    # get_tracker_total do. Synthesising an English sentence for the free-text
    # parser to re-read is the bug that made every document tool call return
    # nothing, and it would silently drop an unrecognised period to "week".
    period = str(args.get("period") or "week")
    if period not in ("week", "month", "year"):
        period = "week"
    ability = await handle_summary_query(
        db, user_id, "", query=SummaryQuery(period=period)
    )
    return _unwrap(ability, period=period)


async def get_tracker_total(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    # Structured arguments straight through, as get_documents does. The
    # enum value is resolved against the SAME _TRACKER_TERMS table the free-text
    # parser uses, so the two engines cannot answer the same question with
    # different numbers.
    query = tracker_query_for(
        str(args.get("metric") or ""), str(args.get("period") or "week")
    )
    if query is None:
        # NOT None: registry turns a None payload into "Nothing on file for
        # that", and a metric this tool does not cover is not the reader
        # having no data. "heart rate", "blood pressure" and "weight" all
        # land here and are all data this app holds.
        return {
            "found": False,
            "note": (
                "This tool does not track that. It is NOT a statement that "
                "the reader has no such data — use get_latest_metric for "
                "vitals and body measurements before saying anything is "
                "missing."
            ),
            "metric": str(args.get("metric") or ""),
        }
    ability = await handle_tracker_query(db, user_id, "", query=query)
    # Report the metric and period the READ actually used, not the ones asked
    # for: the handler resolves an HRV sibling, falls back from the wearable to
    # a manual log, and clamps a month/year ask to the weekly rollup it has.
    # A payload naming a window the number does not cover is what lets the
    # model paraphrase a week's total as a month's.
    provenance = (ability or {}).get("provenance", {})
    return _unwrap(
        ability,
        metric=provenance.get("metric", query.key),
        period=provenance.get("period", query.period),
    )


async def get_family_members(
    db: AsyncSession, user_id: uuid.UUID, _args: dict, _session_id
) -> dict | None:
    ability = await handle_family_list_query(db, user_id, "who is in my family")
    return _unwrap(ability)


# A natural phrasing per section, so the ONE section-intent table in
# `app/rag/retrieval.py` stays the single source of truth for what a section
# means. Anything not listed falls back to suggestions, which is what this tool
# used to return unconditionally.
_SECTION_QUERY: dict[str, str] = {
    "definition": "what is {c}",
    "symptoms": "what are the symptoms of {c}",
    "signs": "what are the signs of {c}",
    "diagnosis": "how is {c} diagnosed",
    "tests": "what tests are used for {c}",
    "etiology": "what causes {c}",
    "risk_factors": "what are the risk factors for {c}",
    "complications": "what are the complications of {c}",
    "prevalence": "how common is {c}",
    "classification": "what types of {c} are there",
    "suggestions": "tips for {c}",
}


async def get_condition_guidance(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    condition = str(args.get("condition", "")).strip()
    if not condition:
        return None
    section = str(args.get("section", "") or "").strip().lower()
    _text, codes = await build_patient_context(db, user_id)

    # Without a section argument this executor hardcoded "tips for {condition}",
    # and `handle_suggestion_query` filters `chunk_type LIKE 'suggestions%'` —
    # so EVERY condition question the model routed here came back as the
    # suggestions section, whatever was actually asked.
    if section in ("", "suggestions"):
        ability = await handle_suggestion_query(
            db, user_id, f"tips for {condition}", codes
        )
        if ability is not None:
            return _unwrap(ability, condition=condition, section="suggestions")
        # An OMITTED section means "general advice on managing this". Falling
        # through to a definition would answer a management question with a
        # definition the model did not ask for and cannot tell apart from one
        # it did. Nothing found means nothing found.
        return None

    query = _SECTION_QUERY.get(section, _SECTION_QUERY["definition"]).format(
        c=condition
    )
    # Scope on the CONDITION NAME alone, not on the reader's background codes.
    # Passing `codes` in meant an unresolvable condition still produced a scope
    # — the reader's own pedigree conditions — and the profile for one of those
    # came back labelled as the answer about the condition they actually asked
    # about. Better to return nothing and let the model say it has no
    # validated content for that condition.
    scope = await resolve_scope(db, condition, set())
    if not scope:
        return None
    chunks = await retrieve_chunks(db, scope, query, k=4)
    focused = is_focused(chunks, target_sections(query))
    reply = build_extractive_answer(chunks, focused=focused)
    if reply is None:
        return None
    return {
        "deterministic_reply": reply,
        "provenance": {
            "source": "mcp_master_profile",
            "conditions": sorted(scope),
            "chunks": [c.id for c in chunks],
        },
        "condition": condition,
        "section": section or "definition",
        # This tool runs its OWN scoped retrieval, so the turn-level
        # retrieval the caller holds is a different set entirely. Cite
        # what the reply actually quotes.
        OUT_OF_BAND_SOURCES: rendered_chunks(chunks, focused=focused),
    }


async def lookup_medicine(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    name = str(args.get("name", "")).strip()
    if not name:
        return None
    drug = await find_drug(db, name)
    if drug is None:
        return None
    # The reader's recorded medication allergies — the one deterministic
    # safety line the legacy drug path always includes and this executor
    # silently dropped (audit high: engine asymmetry, the drug-interaction
    # bug class). Fail-open like the legacy path.
    warning = ""
    try:
        from app.coredata.service import allergy_warning, medication_allergies
        async with db.begin_nested():
            warning = allergy_warning(await medication_allergies(db, user_id))
    except Exception:  # noqa: BLE001 — enrichment must never break the lookup
        pass
    # medicine_master (Flyway V19) reshaped these: `uses` became `used_for`,
    # and side_effects became a ", "-joined TEXT column. Slicing that string
    # like the old list would hand the model a truncated WORD as a fact
    # ("nausea, vomiting"[:5] == "nause") — valid Python, so nothing but a
    # test catches it.
    substitutes = await find_substitutes(db, drug)
    return {
        "deterministic_reply": build_drug_reply(
            drug, substitutes, allergy_warning=warning),
        # Structured too, so the model cannot summarise the warning away.
        "allergy_warning": warning or None,
        "name": drug.name,
        "composition": [c for c in (drug.composition1, drug.composition2) if c],
        "uses": list(drug.used_for or [])[:5],
        "side_effects": [
            s for s in (drug.side_effects or "").split(", ") if s
        ][:5],
        "habit_forming": drug.habit_forming,
        # Stated explicitly so the model cannot infer that silence means safe.
        "has_interaction_data": False,
    }


async def analyze_image(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    """Read a stored image, if the reader is entitled to it and vision is on.

    Consent is enforced by ``fetch_document_bytes`` BEFORE any network call, so
    there is no path from a chat message to an image the family-consent gate
    would deny. The description that comes back is UNTRUSTED generated text —
    it lands in a tool result, which the orchestrator's validator, fidelity
    guard and grounding verifier all see.
    """
    from app.documents.fetch import fetch_document_bytes
    from app.vision.service import (
        describe_image,
        get_vision_provider,
        vision_enabled,
    )

    if not vision_enabled():
        return None

    kind = str(args.get("kind", "")).strip()
    raw_id = args.get("document_id")
    if raw_id is None:
        return None
    try:
        doc_id = int(raw_id)
    except (TypeError, ValueError):
        return None

    owner = await document_owner(db, kind, doc_id)
    if owner is None:
        return None

    fetched = await fetch_document_bytes(
        db,
        viewer_id=user_id,
        owner_id=owner.owner_id,
        kind=kind,
        resource_id=doc_id,
        is_private=owner.is_private,
    )
    if fetched is None:
        return None

    result = await describe_image(
        get_vision_provider(),
        fetched,
        kind=str(args.get("subject", "document")),
        question=str(args.get("question", "")),
    )
    if result is None or not result.usable:
        return None

    return {
        "description": result.text,
        "subject": result.kind,
        "document_id": doc_id,
        "kind": kind,
        # Said plainly so the model does not treat a photograph as proof.
        "note": (
            "This is what a model could SEE in the image, not an established "
            "fact. Do not name a condition from it, and say clearly that a "
            "clinician needs to look properly."
        ),
    }


# Doses/day -> the slot letters Spring's schedulePattern expects (M/A/E/N).
_SLOTS_BY_COUNT = {1: "M", 2: "ME", 3: "MAE", 4: "MAEN"}


async def get_document_ai_result(
    db: AsyncSession, user_id: uuid.UUID, args: dict, session_id
) -> dict | None:
    phrase = str(args.get("request") or "insights for my report")
    ability = await handle_ai_result_query(db, user_id, phrase, session_id)
    return _unwrap(ability)


async def get_section_details(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    kind = str(args.get("kind", "")).strip().lower()
    if not kind:
        return None
    ability = await handle_section_detail_query(
        db, user_id, f"details of my {kind}s"
    )
    return _unwrap(ability, kind=kind)


async def get_doctor_consults(
    db: AsyncSession, user_id: uuid.UUID, _args: dict, _session_id
) -> dict | None:
    ability = await handle_doctor_consult_query(
        db, user_id, "my recent doctor consultations"
    )
    return _unwrap(ability)


async def list_medications(
    db: AsyncSession, user_id: uuid.UUID, _args: dict, _session_id
) -> dict | None:
    from app.medicines.service import list_courses
    listed = await list_courses(user_id)
    if not listed.ok:
        return {
            "available": False,
            "note": "The medication list could not be read right now — say "
            "so; do not guess at what the reader takes.",
        }
    return {
        "medications": [c.name for c in listed.courses],
        "note": "Active courses only; private entries are excluded. If you "
        "name any of these, include the do-not-change-the-dose reminder.",
    }


async def get_medication_adherence(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    name = str(args.get("name", "")).strip()
    if not name:
        return None
    from app.chat.medication_flow import _handle_adherence
    ability = await _handle_adherence(db, user_id, name)
    return _unwrap(ability, name=name)


async def _medication(action: str, db, user_id, args) -> dict | None:
    """Shared executor for the three medication tools. Routes structured args
    straight to the write, so a scheduled add carries its frequency (the model
    gathers and confirms it before calling)."""
    name = str(args.get("name", "")).strip()
    if not name:
        return None
    strength: str | None = None
    is_prn = False
    schedule_pattern: str | None = None
    if action == "add":
        strength = str(args.get("strength") or "").strip() or None
        tpd = args.get("times_per_day")
        if args.get("as_needed"):
            is_prn = True
        elif isinstance(tpd, int) and not isinstance(tpd, bool) and 1 <= tpd <= 4:
            schedule_pattern = _SLOTS_BY_COUNT[tpd]
        else:
            # Frequency unknown: a valid as-needed course, never a 500.
            is_prn = True
    ability = await perform_medication_write(
        db, user_id, action, name,
        strength=strength, is_prn=is_prn, schedule_pattern=schedule_pattern,
    )
    return _unwrap(ability, action=action, name=name)


async def add_medication(db, user_id, args, _session_id) -> dict | None:
    return await _medication("add", db, user_id, args)


async def stop_medication(db, user_id, args, _session_id) -> dict | None:
    return await _medication("stop", db, user_id, args)


async def remove_medication(db, user_id, args, _session_id) -> dict | None:
    return await _medication("remove", db, user_id, args)

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

from app.chat.context import build_patient_context
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
    perform_medication_write,
)
from app.coredata.service import document_owner
from app.drugs.service import build_drug_reply, find_drug, find_substitutes

# Keys a handler may return that the model can use directly.
_PASSTHROUGH = ("documents", "visual", "citations")


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
    return payload


async def get_latest_metric(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    metric = str(args.get("metric", "")).strip()
    if not metric:
        return None
    ability = await handle_metric_query(db, user_id, f"what is my latest {metric}")
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
    kinds = args.get("kinds") or ["document"]
    if not isinstance(kinds, list):
        kinds = [str(kinds)]
    who = str(args.get("relation") or args.get("owner_name") or "").strip()
    phrase = f"show me {who + ' ' if who else ''}{' '.join(str(k) for k in kinds)}"
    ability = await handle_document_query(db, user_id, phrase)
    return _unwrap(ability, asked_about=who or "you")


async def check_value_against_range(
    db: AsyncSession, user_id: uuid.UUID, args: dict, session_id
) -> dict | None:
    metric = str(args.get("metric", "")).strip()
    value = args.get("value")
    if not metric or value is None:
        return None
    secondary = args.get("secondary")
    reading = f"{value}/{secondary}" if secondary is not None else f"{value}"
    ability = await handle_value_check(
        db, user_id, f"my {metric} is {reading}", session_id
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
    period = str(args.get("period", "week"))
    ability = await handle_summary_query(
        db, user_id, f"health summary for the {period}"
    )
    return _unwrap(ability, period=period)


async def get_family_members(
    db: AsyncSession, user_id: uuid.UUID, _args: dict, _session_id
) -> dict | None:
    ability = await handle_family_list_query(db, user_id, "who is in my family")
    return _unwrap(ability)


async def get_condition_guidance(
    db: AsyncSession, user_id: uuid.UUID, args: dict, _session_id
) -> dict | None:
    condition = str(args.get("condition", "")).strip()
    if not condition:
        return None
    _text, codes = await build_patient_context(db, user_id)
    ability = await handle_suggestion_query(
        db, user_id, f"tips for {condition}", codes
    )
    return _unwrap(ability, condition=condition)


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

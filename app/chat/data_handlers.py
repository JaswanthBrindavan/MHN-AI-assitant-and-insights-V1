"""Handlers for the deterministic chat data-abilities.

Each returns a plain dict {reply, action, provenance, visual?} or None (not
applicable / nothing found → caller falls through). All replies are
validator-safe and never diagnostic; tracker writes always confirm what was
recorded. Everything here is deterministic — no LLM.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.charts.svg import chart_payload
from app.chat.abilities import (
    METRIC_REGISTRY,
    DocumentQuery,
    MedicationCommand,
    MetricQuery,
    StatedValue,
    SummaryQuery,
    TrackerAdd,
    param_tokens,
    parse_ai_result_query,
    parse_doctor_consult_query,
    parse_document_query_fuzzy,
    parse_family_list_query,
    parse_medication_command,
    parse_metric_query,
    parse_report_param_ask,
    parse_section_detail_query,
    parse_stated_value,
    parse_suggestion_query,
    parse_summary_query,
    parse_tracker_add,
    parse_tracker_query,
)
from app.coredata.service import (
    _MANUAL_UNIT,
    _RESOURCE_TYPE,
    DOCUMENT_KINDS,
    active_medications,
    add_lifestyle_log,
    latest_body_measurement,
    latest_documents,
    latest_manual_metrics,
    latest_vital,
    lifestyle_totals,
    list_family_connections,
    recent_doctor_consults,
    resolve_family_member,
    resolve_family_member_by_name,
    vital_series,
    window_start,
)

# Module import (not the bare function): tests monkeypatch
# app.documents.service.fetch_ai_result, so the call must resolve late.
from app.documents import service as documents_service
from app.health import ranges as health_ranges
from app.health import reference as health_reference
from app.knowledge.registry import load_condition_index
from app.models.chat import ConversationMessage, McpChunk
from app.models.common import utcnow
from app.models.coredata import Report, UnclassifiedFile
from app.rag.retrieval import resolve_scope

_NOT_MEDICAL_ADVICE = (
    "This is your own recorded data, not medical advice — please discuss any "
    "concerns with your doctor."
)

# Never diagnoses; always routes an out-of-range reading to a clinician.
_NOT_A_DIAGNOSIS_LINE = (
    "This is not a diagnosis — a single reading can't confirm any condition, "
    "and only a doctor can interpret it in the context of your history and "
    "other tests."
)


# --------------------------------------------------------------------------- #
# Stated-value reference-range check ("my sugar is 117 …")
# --------------------------------------------------------------------------- #
def _value_check_reply(stated: StatedValue) -> dict | None:
    """Compare a stated reading to its DRAFT reference range — safe, no diagnosis.

    In range → reassure + keep monitoring. Out of range → flag against the
    typical range and route to a doctor. NEVER names a disease.
    """
    if stated.metric == "blood_pressure":
        verdict = health_ranges.classify_bp(stated.value, stated.secondary)
        reading = (
            f"{_g(stated.value)}/{_g(stated.secondary)} mmHg"
            if stated.secondary is not None else f"{_g(stated.value)} mmHg"
        )
    else:
        verdict = health_ranges.classify(stated.metric, stated.value)
        if verdict is None:
            return None
        unit = health_ranges.RANGES[stated.metric].unit
        reading = f"{_g(stated.value)} {unit}"

    art = _article(verdict.label)
    if verdict.status == "in_range":
        body = (
            f"{art} {verdict.label} of {reading} is within the typical range "
            f"({verdict.range_text}). That's reassuring. Keep monitoring as "
            f"your doctor advises, and mention any symptoms or concerns at your "
            f"next visit."
        )
        action = "review_with_clinician"
    else:
        direction = "above" if verdict.status == "above" else "below"
        body = (
            f"{art} {verdict.label} of {reading} is {direction} the typical "
            f"range ({verdict.range_text}). {_NOT_A_DIAGNOSIS_LINE} Please "
            f"consult your doctor, who can confirm the reading and advise on "
            f"next steps."
        )
        action = "discuss_with_clinician"
    if verdict.note:
        body += f" {verdict.note}"

    return {
        "reply": body,
        "action": action,
        "provenance": {
            "path": "value_check",
            "metric": stated.metric,
            "status": verdict.status,
        },
    }


def _g(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _article(label: str) -> str:
    """'A' / 'An' by the label's first spoken sound (handles vowel-sound
    acronyms like HbA1c, HDL, LDL, SpO2)."""
    w = label.strip().lower()
    if w[:1] in "aeiou" or w[:5] == "hba1c" or w[:3] in ("hdl", "ldl", "spo"):
        return "An"
    return "A"


def _backend_reply(v, reading: str) -> dict:
    """Compose a safe reply from a graduated backend verdict (thp_age_range).

    Severity routes to care: normal → reassure; warn → consult a doctor;
    danger → seek care promptly. Never diagnoses.
    """
    ideal = f"{_g(v.ideal_low)}–{_g(v.ideal_high)} {v.unit}".strip()
    art = _article(v.label)
    if v.severity == "normal":
        return {
            "reply": (
                f"{art} {v.label} of {reading} is within the usual range for "
                f"your age ({ideal}). That's reassuring — keep monitoring as "
                f"your doctor advises."
            ),
            "action": "review_with_clinician",
            "provenance": {"path": "value_check", "source": "backend_ranges",
                           "severity": "normal"},
        }
    word = {"high": "above", "low": "below"}.get(v.direction, "outside")
    if v.severity == "danger":
        lead = (
            f"{art} {v.label} of {reading} is well {word} the usual range "
            f"for your age ({ideal}). Please seek medical advice promptly — "
            f"contact your doctor or urgent care."
        )
        action = "seek_care_promptly"
    else:
        lead = (
            f"{art} {v.label} of {reading} is {word} the usual range for "
            f"your age ({ideal}). Please consult your doctor to review it."
        )
        action = "discuss_with_clinician"
    return {
        "reply": f"{lead} {_NOT_A_DIAGNOSIS_LINE}",
        "action": action,
        "provenance": {"path": "value_check", "source": "backend_ranges",
                       "severity": v.severity},
    }


# Bare timing clarifications the user gives after stating a glucose value.
_FASTING_RE = re.compile(r"\bfasting\b", re.IGNORECASE)
_POSTMEAL_RE = re.compile(
    r"\b(?:after (?:a )?meal|post[- ]?meal|after (?:eating|food)|"
    r"postprandial|\bpp\b|random)\b",
    re.IGNORECASE,
)


async def _recent_user_messages(
    db: AsyncSession, session_id: uuid.UUID | None, limit: int = 8
) -> list[str]:
    if session_id is None:
        return []
    rows = (
        await db.execute(
            select(ConversationMessage.message)
            .where(
                ConversationMessage.session_id == session_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


def _reclassify_glucose(value: float, *, fasting: bool) -> dict:
    """Re-classify a remembered glucose value once the reader clarifies timing."""
    key = "fasting_glucose" if fasting else "random_glucose"
    verdict = health_ranges.classify(key, value)
    label = "fasting blood sugar" if fasting else "post-meal blood sugar"
    reading = f"{_g(value)} mg/dL"
    if verdict is not None and verdict.status == "in_range":
        body = (
            f"Thanks — for a {label} reading, {_g(value)} mg/dL is within the "
            f"typical range ({verdict.range_text}). That's reassuring. Keep "
            f"monitoring as your doctor advises."
        )
        action = "review_with_clinician"
        status = "in_range"
    else:
        direction = (
            "above" if verdict and verdict.status == "above" else "below"
        )
        rng = verdict.range_text if verdict else ""
        body = (
            f"Thanks for clarifying. For a {label} reading, {reading} is "
            f"{direction} the typical range ({rng}). {_NOT_A_DIAGNOSIS_LINE} "
            f"Please consult your doctor, who can confirm the reading and advise "
            f"on next steps."
        )
        action = "discuss_with_clinician"
        status = verdict.status if verdict else "above"
    return {
        "reply": body,
        "action": action,
        "provenance": {
            "path": "value_check", "metric": key, "status": status,
            "carried_value": value,
        },
    }


async def handle_value_check(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    session_id: uuid.UUID | None = None,
) -> dict | None:
    """Deterministic reference-range check.

    1. A value stated in THIS message → classify it directly.
    2. A bare timing clarification ("fasting", "after a meal") that answers an
       earlier "my sugar is 117" → recall that value and re-classify against the
       fasting/post-meal range. This makes the follow-up deterministic rather
       than relying on the model to notice the recent conversation.
    """
    stated = parse_stated_value(message)
    if stated is not None:
        backend = await _try_backend(db, user_id, stated.metric, stated.value)
        if backend is not None:
            return backend
        return _value_check_reply(stated)

    # Clarification path: only for a SHORT reply carrying a timing qualifier.
    fasting = bool(_FASTING_RE.search(message))
    postmeal = bool(_POSTMEAL_RE.search(message))
    if not (fasting or postmeal) or len(message.split()) > 5:
        return None
    for prior in await _recent_user_messages(db, session_id):
        sv = parse_stated_value(prior)
        if sv is not None and sv.metric == "blood_sugar":
            key = "fasting_glucose" if fasting else "random_glucose"
            backend = await _try_backend(db, user_id, key, sv.value)
            if backend is not None:
                return backend
            return _reclassify_glucose(sv.value, fasting=fasting)
    return None


async def _try_backend(
    db: AsyncSession, user_id: uuid.UUID, metric: str, value: float
) -> dict | None:
    """Prefer the backend age-banded ranges; None → caller uses DRAFT constants.

    Blood pressure (two values) stays on the constants path for now.
    """
    if metric == "blood_pressure":
        return None
    age = await health_reference.user_age(db, user_id)
    verdict = await health_reference.evaluate_backend(db, metric, value, age)
    if verdict is None:
        return None
    unit = verdict.unit or (
        health_ranges.RANGES[metric].unit if metric in health_ranges.RANGES else ""
    )
    return _backend_reply(verdict, f"{_g(value)} {unit}".strip())


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
async def handle_document_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    query: DocumentQuery | None = parse_document_query_fuzzy(message)
    if query is None:
        return None

    owner_id, owner_label, include_private = user_id, "you", True
    owner_slug = None  # family member's username — the /family/{slug} route key
    if query.relation or query.owner_name:
        if query.relation:
            member = await resolve_family_member(db, user_id, query.relation)
            asked, label = query.relation, f"your {query.relation}"
        else:
            member = await resolve_family_member_by_name(
                db, user_id, query.owner_name or ""
            )
            asked = query.owner_name or ""
            label = asked.title()
        if member is None:
            return {
                "reply": (
                    f"I couldn't find a connected family member matching "
                    f"'{asked}' with document sharing enabled. They may need "
                    "to accept the family connection or turn on file sharing "
                    "in the app."
                ),
                "action": "none",
                "provenance": {"path": "document_query", "asked": asked,
                               "resolved": False},
            }
        owner_id, owner_label, include_private = member, label, False
        try:
            from app.models.core import User

            owner_slug = (
                await db.execute(
                    select(User.user_name).where(User.id == member)
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — user table may be absent standalone
            owner_slug = None

    hits = await latest_documents(
        db, owner_id, list(query.kinds),
        owner_label=owner_label, include_private=include_private,
        viewer_id=user_id,
    )

    # Chat-uploaded documents live in unclassified_files until the pipeline
    # files them — without this they are invisible to "show my documents"
    # exactly in the window right after uploading.
    pending: list = []
    if query.relation is None and query.owner_name is None:
        pending = list((
            await db.execute(
                select(UnclassifiedFile)
                .where(
                    (UnclassifiedFile.user_id == user_id)
                    | (UnclassifiedFile.created_by == user_id)
                )
                .order_by(UnclassifiedFile.id.desc())
                .limit(3)
            )
        ).scalars().all())

    if not hits and not pending:
        kinds = ", ".join(query.kinds)
        return {
            "reply": (
                f"I couldn't find any {kinds} documents for {owner_label} in "
                "the records I can access."
            ),
            "action": "none",
            "provenance": {"path": "document_query", "kinds": list(query.kinds),
                           "found": 0},
        }

    lines = []
    for h in hits:
        when = h.created_at.strftime("%d %b %Y") if h.created_at else "date unknown"
        # Prefer the AI classification title (production scans have no name
        # column); fall back to the file's basename.
        name = h.title or h.filepath.rsplit("/", 1)[-1]
        lines.append(f"• {h.kind} — {name} ({when})")
    for p_row in pending:
        pname = p_row.name or p_row.filepath.rsplit("/", 1)[-1]
        lines.append(f"• document — {pname} (still being processed)")
    if hits:
        lead = (
            f"Here {'is' if len(hits) == 1 else 'are'} the most recent "
            f"{'document' if len(hits) == 1 else 'documents'} for {owner_label}:"
        )
    else:
        lead = (
            "Your recently uploaded "
            f"{'document is' if len(pending) == 1 else 'documents are'} "
            "still being processed:"
        )
    if query.wants_date and hits and hits[0].created_at:
        lead = (
            f"The most recent {hits[0].kind} for {owner_label} is from "
            f"{hits[0].created_at.strftime('%d %b %Y')}."
        )
    # Document cards: everything a client needs to open the file through the
    # EXISTING app flow (Spring GET /files/{resource_type}/{id}/url or the
    # health-wallet detail routes). Davi never mints URLs or touches S3.
    cards = [
        {
            "kind": h.kind,
            "resource_type": _RESOURCE_TYPE.get(h.kind, h.kind),
            # Spring's file routes address the serial id (verified:
            # FileController GET /files/{type}/{id}/url takes Integer id).
            # Clients open the FILE via that presigned-URL endpoint — the
            # wallet detail page only exists for the viewer's own documents.
            "id": h.doc_id,
            # The wallet detail route's public slug is the file's storage-key
            # UUID — the last segment of the S3 filepath ("reports/<uuid>"),
            # never the DB id (mirrors the app's fileSlugFromPath).
            "slug": h.filepath.rsplit("/", 1)[-1],
            "title": h.title or h.filepath.rsplit("/", 1)[-1],
            "date": h.created_at.isoformat() if h.created_at else None,
            "owner": h.owner_label,
            # For family documents: the member's username, so the client can
            # open the in-app family view (/family/{slug}/{section}/{file})
            # instead of the raw file.
            **({"owner_slug": owner_slug} if owner_slug else {}),
        }
        for h in hits
    ]
    cards.extend(
        {
            "kind": "document",
            "resource_type": "unclassified",
            "id": p_row.id,
            "title": p_row.name or p_row.filepath.rsplit("/", 1)[-1],
            "date": p_row.created_at.isoformat() if p_row.created_at else None,
            "owner": "you",
            "pending": True,
        }
        for p_row in pending
    )
    return {
        "reply": lead + "\n" + "\n".join(lines),
        "action": "open_documents",
        "documents": cards,
        "provenance": {
            "path": "document_query",
            "kinds": list(query.kinds),
            "found": len(hits),
            "documents": [
                {"kind": h.kind, "id": h.doc_id, "created_at":
                 h.created_at.isoformat() if h.created_at else None}
                for h in hits
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Tracker adds
# --------------------------------------------------------------------------- #
async def handle_tracker_add(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    add: TrackerAdd | None = parse_tracker_add(message)
    if add is None:
        return None
    logged_at = utcnow() - timedelta(days=add.day_offset)
    row = await add_lifestyle_log(
        db, user_id, add.log_type, add.quantity, add.unit, logged_at
    )
    day = {0: "today", 1: "yesterday", 2: "the day before yesterday"}.get(
        add.day_offset, f"{add.day_offset} days ago"
    )
    qty = f"{add.quantity:g}"
    if add.quantity == 1:
        unit = add.unit
    elif add.unit.endswith(("s", "sh", "ch", "x")):
        unit = add.unit + "es"  # glass → glasses
    else:
        unit = add.unit + "s"
    note = ""
    if add.log_type == "smoking":
        note = (
            " If you're thinking about cutting down, your doctor can share "
            "options that make quitting easier."
        )
    elif add.log_type == "alcohol":
        note = " Tracked as an alcohol entry."
    # "2 cups of coffee" reads naturally; "5 cigarettes of smoking" does not —
    # skip the kind when the unit already names the thing being counted.
    unit_is_the_kind = add.unit in ("cigarette", "beedi", "drink")
    what = f"{qty} {unit}" if unit_is_the_kind else f"{qty} {unit} of {add.log_type}"
    return {
        "reply": (
            f"Logged: {what} for {day}. "
            f"You can see this in your lifestyle tracker.{note}"
        ),
        "action": "logged",
        "provenance": {
            "path": "tracker_add",
            "log_type": add.log_type,
            "quantity": add.quantity,
            "unit": add.unit,
            "day_offset": add.day_offset,
            "log_id": row.id,
        },
    }


# --------------------------------------------------------------------------- #
# Metric pulls
# --------------------------------------------------------------------------- #
def _search_content_for_param(
    content, terms: tuple[str, ...], exclude: tuple[str, ...] = ()
):
    """Recursively search a report's extracted JSON for a named parameter.

    Handles both the legacy demo shape ({"tests": [{"name","value","unit"}]})
    and the production mhn-ai envelope (content.ai.extraction.results[] with
    "test_name", verbatim string "value" — possibly with comparators — and a
    Python-computed "value_numeric").
    """
    if isinstance(content, dict):
        name = str(
            content.get("name") or content.get("parameter") or content.get("test")
            or content.get("test_name") or ""
        ).lower()
        if (
            name
            and any(t in name for t in terms)
            and not any(x in name for x in exclude)
        ):
            # Production pre-computes the numeric value — trust it first.
            vn = content.get("value_numeric")
            if isinstance(vn, (int, float)):
                return float(vn), content.get("unit")
            for value_key in ("value", "result", "reading"):
                raw = content.get(value_key)
                if raw is not None:
                    m = re.search(r"-?\d+(?:\.\d+)?", str(raw))
                    if m:
                        return float(m.group()), content.get("unit")
        for v in content.values():
            found = _search_content_for_param(v, terms, exclude)
            if found:
                return found
    elif isinstance(content, list):
        for item in content:
            found = _search_content_for_param(item, terms, exclude)
            if found:
                return found
    return None


async def _latest_report_param(
    db: AsyncSession,
    user_id: uuid.UUID,
    terms: tuple[str, ...],
    exclude: tuple[str, ...] = (),
):
    rows = (
        await db.execute(
            select(Report)
            .where(Report.user_id == user_id, Report.content.is_not(None))
            .order_by(Report.created_at.desc().nulls_last(), Report.id.desc())
            .limit(20)
        )
    ).scalars().all()
    for r in rows:
        found = _search_content_for_param(r.content, terms, exclude)
        if found:
            value, unit = found
            return value, unit, r.created_at
    return None


async def handle_metric_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    query: MetricQuery | None = parse_metric_query(message)
    if query is None:
        return None
    spec = METRIC_REGISTRY[query.metric]
    display, unit = spec["display"], spec.get("unit")

    visual = None
    if spec["source"] == "vital":
        point = await latest_vital(db, user_id, spec["vital_type"])
        if point is None:
            # No logged vital — fall back to extracted reports when the spec
            # names report terms ("Glucose - Fasting" lives there).
            if spec.get("param_terms"):
                found = await _latest_report_param(
                    db, user_id, spec["param_terms"],
                    spec.get("param_exclude", ()),
                )
                if found is not None:
                    value, found_unit, created = found
                    value_text = f"{value:g} {found_unit or unit}"
                    when = (
                        created.strftime("%d %b %Y") if created
                        else "date unknown"
                    )
                    return {
                        "reply": (
                            f"Your most recent {display} on record is "
                            f"{value_text} (from a report dated {when}). "
                            f"{_NOT_MEDICAL_ADVICE}"
                        ),
                        "action": "review_with_clinician",
                        "provenance": {"path": "metric_query",
                                       "metric": query.metric,
                                       "value_text": value_text,
                                       "recorded": when,
                                       "source": "report"},
                    }
            return _metric_not_found(display)
        if point.secondary is not None:
            value_text = f"{point.value:g}/{point.secondary:g} {point.unit or unit}"
        else:
            value_text = f"{point.value:g} {point.unit or unit}"
        when = point.at.strftime("%d %b %Y")
        if query.wants_trend:
            series = await vital_series(
                db, user_id, spec["vital_type"], window_start("month")
            )
            if len(series) >= 2:
                visual = chart_payload(
                    "line",
                    f"{display} — last 30 days",
                    [p.at.strftime("%d %b") for p in series],
                    [p.value for p in series],
                    unit=unit,
                )
    elif spec["source"] == "body":
        point = await latest_body_measurement(db, user_id, spec["body_type"])
        if point is None:
            return _metric_not_found(display)
        value_text = f"{point.value:g} {unit}"
        when = point.at.strftime("%d %b %Y")
    else:  # report_param (e.g. HbA1c from extracted lab reports)
        found = await _latest_report_param(
            db, user_id, spec["param_terms"], spec.get("param_exclude", ())
        )
        if found is None:
            return _metric_not_found(display)
        value, found_unit, created = found
        value_text = f"{value:g} {found_unit or unit}"
        when = created.strftime("%d %b %Y") if created else "date unknown"

    return {
        "reply": (
            f"Your most recent {display} on record is {value_text} "
            f"(recorded {when}). {_NOT_MEDICAL_ADVICE}"
        ),
        "action": "review_with_clinician",
        # value_text/recorded are the machine-readable forms the tool
        # executors hand to the model so it can COMBINE this with other
        # facts; the legacy engine ignores them.
        "provenance": {
            "path": "metric_query",
            "metric": query.metric,
            "value_text": value_text,
            "recorded": when,
            "source": spec["source"],
        },
        "visual": visual,
    }


def _metric_not_found(display: str) -> dict:
    return {
        "reply": (
            f"I couldn't find any {display} readings in your records yet. Once "
            "you log one (or a report with it is uploaded), I can pull it up "
            "here."
        ),
        "action": "none",
        "provenance": {"path": "metric_query", "found": False},
    }


# --------------------------------------------------------------------------- #
# Health summary
# --------------------------------------------------------------------------- #
async def handle_summary_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    query: SummaryQuery | None = parse_summary_query(message)
    if query is None:
        return None
    since = window_start(query.period)
    totals = await lifestyle_totals(db, user_id, since)

    lines: list[str] = []
    label = {"week": "past week", "month": "past month", "year": "past year"}[
        query.period
    ]
    if totals:
        parts = [f"{qty:g} {ltype}" for ltype, qty in sorted(totals.items())]
        lines.append("Lifestyle entries: " + ", ".join(parts) + ".")

    metrics_shown: list[str] = []
    for vital_type, display in (
        ("blood_pressure", "blood pressure"),
        ("blood_sugar", "blood sugar"),
        ("heart_rate", "heart rate"),
    ):
        point = await latest_vital(db, user_id, vital_type)
        if point and point.at >= since:
            value = (
                f"{point.value:g}/{point.secondary:g}"
                if point.secondary is not None
                else f"{point.value:g}"
            )
            metrics_shown.append(f"latest {display} {value} {point.unit or ''}".strip())
    if metrics_shown:
        lines.append("Vitals: " + "; ".join(metrics_shown) + ".")

    if not lines:
        return {
            "reply": (
                f"I don't have any logged data for the {label} yet. Log vitals "
                "or lifestyle entries and I can build your summary."
            ),
            "action": "none",
            "provenance": {"path": "health_summary", "period": query.period,
                           "empty": True},
        }

    visual = None
    if totals:
        labels = sorted(totals)
        visual = chart_payload(
            "bar",
            f"Lifestyle totals — {label}",
            labels,
            [totals[k] for k in labels],
        )
    return {
        "reply": (
            f"Here's your health summary for the {label}:\n"
            + "\n".join(f"• {ln}" for ln in lines)
            + f"\n{_NOT_MEDICAL_ADVICE}"
        ),
        "action": "review_with_clinician",
        "provenance": {"path": "health_summary", "period": query.period},
        "visual": visual,
    }


# --------------------------------------------------------------------------- #
# MCP suggestions (clinically validated corpus, rendered readably)
# --------------------------------------------------------------------------- #
_SUGGESTION_SEGMENT_RE = re.compile(
    r";\s*(?=(?:LHP|Type|Suggestion|Profile|Importance):)"
)
# Friendly section headers for the corpus LHP categories.
_LHP_HEADERS = {
    "food": "Food & diet",
    "physical activity": "Staying active",
    "education": "Know the basics",
    "sleep": "Sleep",
    "smoking": "Tobacco",
    "alcohol": "Alcohol",
    "mental health": "Mind & stress",
    "water": "Hydration",
    "hygiene": "Hygiene",
}
_MAX_SECTIONS = 4
_MAX_BULLETS_PER_SECTION = 4


def _parse_suggestion_line(line: str) -> tuple[str, list[str]] | None:
    """One flattened table row → (section header, clean bullets)."""
    fields: dict[str, str] = {}
    for segment in _SUGGESTION_SEGMENT_RE.split(line):
        key, _, value = segment.partition(":")
        fields[key.strip().lower()] = value.strip()
    suggestion = fields.get("suggestion")
    if not suggestion:
        return None
    bullets = [
        re.sub(r"^[•\s]+", "", b).strip()
        for b in re.split(r"\s*/\s*•", suggestion)
    ]
    bullets = [b for b in bullets if len(b) > 10]
    if not bullets:
        return None
    lhp = fields.get("lhp", "").strip().lower()
    header = _LHP_HEADERS.get(lhp, lhp.title() if lhp else "General")
    return header, bullets


def format_suggestions(rows_content: list[str], display_names: list[str]) -> str:
    """Render suggestions chunks as clean, sectioned, readable text."""
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    for content in rows_content:
        body = content.split("\n", 1)[1] if "\n" in content else content
        for line in body.split("\n"):
            parsed = _parse_suggestion_line(line)
            if parsed is None:
                continue
            header, bullets = parsed
            if header not in sections:
                sections[header] = []
                order.append(header)
            for b in bullets:
                if b not in sections[header]:
                    sections[header].append(b)

    if not sections:
        return ""
    names = " and ".join(display_names[:2]) if display_names else "your conditions"
    parts = [
        f"Based on our clinically reviewed profiles for {names}, "
        "here's what generally helps:"
    ]
    for header in order[:_MAX_SECTIONS]:
        bullets = sections[header][:_MAX_BULLETS_PER_SECTION]
        parts.append(
            f"**{header}**\n" + "\n".join(f"• {b}" for b in bullets)
        )
    parts.append(
        "These are general, educational pointers — not a personal "
        "prescription. Your doctor can tailor them to you."
    )
    return "\n\n".join(parts)


async def handle_suggestion_query(
    db: AsyncSession, user_id: uuid.UUID, message: str, user_codes: set[str]
) -> dict | None:
    if not parse_suggestion_query(message):
        return None
    codes = await resolve_scope(db, message, user_codes)
    index = await load_condition_index(db)
    if not codes or index is None:
        return None  # fall through to the RAG path

    # Conditions the MESSAGE itself names outrank the user's background
    # conditions — "tips for polycystic kidney disease" must not lose its
    # chunk budget to an alphabetically-earlier background condition.
    ranked = index.match_message_ranked(message)
    if ranked:
        best = max(ranked.values())
        # Keep only the most specifically-named conditions (full-name matches
        # beat shared head-noun matches by length).
        preferred = {c for c, score in ranked.items() if score >= best - 2} & codes
        preferred = preferred or codes
    else:
        preferred = codes

    rows = list(
        (
            await db.execute(
                select(McpChunk)
                .where(
                    McpChunk.condition_code.in_(preferred),
                    McpChunk.chunk_type.like("suggestions%"),
                )
                .order_by(McpChunk.condition_code, McpChunk.chunk_type)
                .limit(4)
            )
        ).scalars().all()
    )
    if not rows:
        return None

    display_names = sorted(
        {
            index.by_code[r.condition_code].display_name
            for r in rows
            if r.condition_code in index.by_code
        }
    )
    reply = format_suggestions([r.content for r in rows], display_names)
    if not reply:
        return None
    return {
        "reply": reply,
        "action": "discuss_with_clinician",
        "provenance": {
            "path": "mcp_suggestions",
            "conditions": sorted({r.condition_code for r in rows}),
            "chunks": [str(r.id) for r in rows],
        },
        "citations": [
            {
                "source": "mcp_master_profile",
                "condition_code": r.condition_code,
                "section": r.chunk_type,
                "display_name": index.by_code[r.condition_code].display_name
                if r.condition_code in index.by_code
                else r.condition_code,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Family connections & doctor consults
# --------------------------------------------------------------------------- #
async def handle_family_list_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    if not parse_family_list_query(message):
        return None
    members = await list_family_connections(db, user_id)
    if not members:
        return {
            "reply": (
                "You don't have any family connections yet. You can invite "
                "family members from the Family Connect section of the app."
            ),
            "action": "none",
            "provenance": {"path": "family_connections", "found": 0},
        }
    lines = []
    for m in members:
        who = m.name or "A family member"
        rel = f" — your {m.relation}" if m.relation else ""
        if not m.accepted:
            status = "invitation pending"
        elif m.shares_documents:
            status = "shares documents with you"
        else:
            status = "connected"
        lines.append(f"• {who}{rel} ({status})")
    n = len(members)
    lead = (
        f"You have {n} {'person' if n == 1 else 'people'} in your "
        "Family Connect:"
    )
    return {
        "reply": lead + "\n" + "\n".join(lines),
        "action": "none",
        "provenance": {"path": "family_connections", "found": n},
    }


async def handle_doctor_consult_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    if not parse_doctor_consult_query(message):
        return None
    consults = await recent_doctor_consults(db, user_id)
    if not consults:
        return {
            "reply": (
                "I couldn't find any doctor consultations through the app "
                "yet. You can connect with a doctor from the Doctor Connect "
                "section."
            ),
            "action": "none",
            "provenance": {"path": "doctor_consults", "found": 0},
        }
    latest = consults[0]
    who = latest.doctor_name or "a doctor"
    spec = f" ({latest.specialization})" if latest.specialization else ""
    when = (
        f" on {latest.connected_at.strftime('%d %b %Y')}"
        if latest.connected_at
        else ""
    )
    if latest.status == "connected":
        lead = f"Your most recent doctor connection is {who}{spec}, connected{when}."
    else:
        lead = (
            f"Your most recent doctor request is with {who}{spec}, "
            f"still pending acceptance{when}."
        )
    extra = ""
    if len(consults) > 1:
        others = []
        for c in consults[1:4]:
            nm = c.doctor_name or "a doctor"
            sp = f" ({c.specialization})" if c.specialization else ""
            others.append(f"• {nm}{sp} — {c.status}")
        extra = "\nOther doctor connections:\n" + "\n".join(others)
    return {
        "reply": lead + extra,
        "action": "none",
        "provenance": {"path": "doctor_consults", "found": len(consults)},
    }


# --------------------------------------------------------------------------- #
# Document AI results — pulled from mhn-ai, never generated here
# --------------------------------------------------------------------------- #
async def handle_ai_result_query(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    session_id: uuid.UUID | None = None,
) -> dict | None:
    """"Get insights for this report" → mhn-ai's ai-result for the user's
    most recent upload. Insights for reports; section extraction for other
    document types. Davi renders what the pipeline produced — it never
    invents analysis of a document it cannot see."""
    if not parse_ai_result_query(message):
        return None
    # Resolve "this report" to a pipeline document id, in order:
    #   0. the document the CONVERSATION is about — the last reply in this
    #      session that carried document cards (persisted in message meta);
    #   1. a still-unclassified upload (processing not finished — the row is
    #      deleted when mhn-ai files the document);
    #   2. otherwise the newest FILED document, whose content.ai envelope
    #      carries the ORIGINAL document_id the ai-result endpoint is
    #      addressed by (verified: mhn-ai assembly.py writes it).
    document_id: int | None = None
    name = "your document"

    if session_id is not None:
        recent = (
            await db.execute(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.role == "assistant",
                )
                .order_by(
                    ConversationMessage.created_at.desc(),
                    ConversationMessage.id.desc(),
                )
                .limit(10)
            )
        ).scalars().all()
        _CARD_MODELS = {model.__tablename__: model
                        for model, _lbl in DOCUMENT_KINDS.values()}
        for m in recent:
            cards = ((m.extracted_intent or {}).get("documents")) or []
            if not cards:
                continue
            card = cards[0]
            card_id = card.get("id")
            rtype = card.get("resource_type")
            if not isinstance(card_id, int):
                break
            if rtype == "unclassified":
                # A pending upload's card id IS the pipeline document id.
                document_id = card_id
                name = str(card.get("title") or name)
            else:
                model = _CARD_MODELS.get(str(rtype))
                if model is not None:
                    doc = (
                        await db.execute(
                            select(model).where(model.id == card_id)  # type: ignore[attr-defined]
                        )
                    ).scalars().first()
                    if doc is not None and doc.user_id == user_id:
                        ai = (getattr(doc, "content", None) or {}).get("ai") or {}
                        if isinstance(ai.get("document_id"), int):
                            document_id = ai["document_id"]
                            name = str(card.get("title") or name)
            break  # only the most recent card-bearing reply counts as "this"

    row = None if document_id is not None else (
        await db.execute(
            select(UnclassifiedFile)
            .where(
                (UnclassifiedFile.user_id == user_id)
                | (UnclassifiedFile.created_by == user_id)
            )
            .order_by(UnclassifiedFile.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is not None:
        document_id = row.id
        name = row.name or row.filepath.rsplit("/", 1)[-1]
    elif document_id is None:
        newest_at = None
        for kind in DOCUMENT_KINDS:
            model, _label = DOCUMENT_KINDS[kind]
            doc = (
                await db.execute(
                    select(model)
                    .where(model.user_id == user_id)  # type: ignore[attr-defined]
                    .order_by(
                        model.created_at.desc().nulls_last(),  # type: ignore[attr-defined]
                        model.id.desc(),  # type: ignore[attr-defined]
                    )
                    .limit(1)
                )
            ).scalars().first()
            if doc is None:
                continue
            ai = (getattr(doc, "content", None) or {}).get("ai") or {}
            doc_id = ai.get("document_id")
            if not isinstance(doc_id, int):
                continue
            at = getattr(doc, "created_at", None)
            if newest_at is None or (at is not None and at > newest_at):
                newest_at = at
                document_id = doc_id
                title = (ai.get("classification") or {}).get("title")
                name = str(title) if title else doc.filepath.rsplit("/", 1)[-1]
    if document_id is None:
        return {
            "reply": (
                "I couldn't find an uploaded document to analyze yet. Attach "
                "a file here in chat (or upload one in your Health Wallet) "
                "and I can pull its processed results once it's ready."
            ),
            "action": "none",
            "provenance": {"path": "ai_result", "found": 0},
        }

    fetch = await documents_service.fetch_ai_result(document_id)
    if not fetch.ok:
        return {
            "reply": (
                f"I couldn't reach the document-processing service for "
                f"'{name}' just now. The document is safe — please try again "
                "shortly."
            ),
            "action": "none",
            "provenance": {"path": "ai_result", "document_id": document_id,
                           "error": fetch.reason},
        }
    result: dict | None = None
    if fetch.result is not None and fetch.status == "completed":
        result = fetch.result
    if result is None:
        state = fetch.status or "queued"
        verdict = (fetch.name_check or {}).get("verdict")
        if fetch.error_code == "name_mismatch" or (
            verdict == "mismatch"
            and not (fetch.name_check or {}).get("confirmed")
        ):
            printed = (fetch.name_check or {}).get("document_name")
            printed_note = f" ('{printed}')" if printed else ""
            reply = (
                f"'{name}' wasn't filed automatically: the patient name "
                f"printed on it{printed_note} doesn't match this account. "
                "If it is yours, you can confirm that from the document's "
                "card in the app; if it belongs to a family member, you can "
                "move it to them there."
            )
            return {
                "reply": reply,
                "action": "none",
                "provenance": {"path": "ai_result",
                               "document_id": document_id,
                               "status": state,
                               "name_check": "mismatch"},
            }
        if state == "failed":
            reply = (
                f"Automatic processing of '{name}' didn't complete. It can "
                "be retried from the document's page in your Health Wallet."
            )
        else:
            reply = (
                f"'{name}' is still being processed (status: {state}). Ask "
                "me again in a little while and I'll pull the results."
            )
        return {
            "reply": reply,
            "action": "none",
            "provenance": {"path": "ai_result", "document_id": document_id,
                           "status": state},
        }

    title = name
    classification = result.get("classification") or {}
    if classification.get("title"):
        title = str(classification["title"])

    lines: list[str] = []
    action = "review_with_clinician"
    insights = (result.get("insights") or {})
    extraction = (result.get("extraction") or {})
    section_extraction = (result.get("section_extraction") or {})

    if insights.get("insights") or insights.get("summary"):
        if insights.get("summary"):
            lines.append(str(insights["summary"]))
        for item in (insights.get("insights") or [])[:4]:
            heading = item.get("heading") or "Finding"
            lines.append(f"\n**{heading}**")
            if item.get("explanation"):
                lines.append(str(item["explanation"]))
            if item.get("suggestion_heading") and item.get("suggestions"):
                lines.append(
                    f"_{item['suggestion_heading']}:_ {item['suggestions']}"
                )
        disclaimer = insights.get("disclaimer")
        lines.append(
            str(disclaimer)
            if disclaimer
            else (
                "These insights are informational only, not a diagnosis — "
                "please review them with your doctor."
            )
        )
        action = "discuss_with_clinician"
    elif extraction.get("results"):
        results = extraction["results"]
        lines.append(f"Here's what was read from \"{title}\":")
        abnormal = 0
        for r in results[:8]:
            flag = (r.get("abnormal_flag") or "").strip().lower()
            if _is_abnormal_flag(flag):
                abnormal += 1
                flag_note = f" — {flag}"
            else:
                flag_note = ""
            unit = f" {r.get('unit')}" if r.get("unit") else ""
            lines.append(
                f"• {r.get('test_name', 'value')}: {r.get('value', '?')}"
                f"{unit}{flag_note}"
            )
        if len(results) > 8:
            lines.append(f"…and {len(results) - 8} more values.")
        if abnormal:
            lines.append(
                f"{abnormal} value{'s are' if abnormal != 1 else ' is'} "
                "flagged outside the printed reference range — worth "
                "discussing with your doctor."
            )
            action = "discuss_with_clinician"
        else:
            lines.append(
                "The extracted values sit within their printed reference "
                "ranges. Your doctor can confirm what they mean for you."
            )
    elif section_extraction.get("fields"):
        lines.append(f"Here's what was read from \"{title}\":")
        for key, value in list(section_extraction["fields"].items())[:8]:
            if value in (None, "", []):
                continue
            pretty = str(key).replace("_", " ")
            lines.append(f"• {pretty}: {value}")
        flags = section_extraction.get("flags") or []
        if flags:
            lines.append(
                "Note: " + ", ".join(str(f).replace("_", " ") for f in flags[:3])
                + "."
            )
    else:
        lines.append(
            f"'{title}' finished processing, but no extractable details came "
            "back for it. You can view the document itself in your Health "
            "Wallet."
        )

    return {
        "reply": "\n".join(lines),
        "action": action,
        "provenance": {
            "path": "ai_result",
            "document_id": document_id,
            "document_type": fetch.document_type,
            "source": "mhn_ai",
        },
    }


# mhn-ai writes the flag explicitly for every row — "normal" is a value, not
# an omission, and must never be reported as a deviation.
_NON_ABNORMAL_FLAGS = frozenset(
    {"", "normal", "n", "na", "n/a", "none", "within range", "-"}
)


def _is_abnormal_flag(flag: str) -> bool:
    return flag.strip().lower() not in _NON_ABNORMAL_FLAGS


# --------------------------------------------------------------------------- #
# Dynamic report-parameter asks ("what is my basophils")
# --------------------------------------------------------------------------- #
async def handle_report_param_ask(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    """Answer ANY parameter present in the user's extracted reports — the
    curated registry covers headline metrics; this covers the rest of the
    THPs (basophils, RDW, GGT, …) by matching the asked term against the
    test names actually on file. Only answers when a match exists."""
    term = parse_report_param_ask(message)
    if term is None:
        return None
    want = param_tokens(term)
    if not want:
        return None

    rows = (
        await db.execute(
            select(Report)
            .where(Report.user_id == user_id, Report.content.is_not(None))
            .order_by(Report.created_at.desc().nulls_last(), Report.id.desc())
            .limit(20)
        )
    ).scalars().all()
    for r in rows:
        ai = (r.content or {}).get("ai") or {}
        results = ((ai.get("extraction") or {}).get("results")) or []
        for item in results:
            test_name = str(item.get("test_name") or "")
            if not test_name:
                continue
            if not want <= param_tokens(test_name):
                continue
            value = item.get("value_numeric")
            if not isinstance(value, (int, float)):
                raw = item.get("value")
                m = re.search(r"-?\d+(?:\.\d+)?", str(raw or ""))
                if not m:
                    continue
                value = float(m.group())
            unit = item.get("unit") or ""
            flag = (item.get("abnormal_flag") or "").strip().lower()
            abnormal = _is_abnormal_flag(flag)
            when = (
                r.created_at.strftime("%d %b %Y") if r.created_at
                else "date unknown"
            )
            if abnormal:
                flag_note = (
                    f" It is flagged {flag} against the printed reference "
                    "range."
                )
            elif flag:
                flag_note = " It is within the printed reference range."
            else:
                flag_note = ""
            return {
                "reply": (
                    f"Your most recent {test_name} on record is "
                    f"{value:g} {unit}".rstrip()
                    + f" (from a report dated {when}).{flag_note} "
                    f"{_NOT_MEDICAL_ADVICE}"
                ),
                "action": (
                    "discuss_with_clinician" if abnormal
                    else "review_with_clinician"
                ),
                "provenance": {"path": "report_param", "term": term,
                               "matched": test_name},
            }
    return None


# --------------------------------------------------------------------------- #
# Section-detail asks — answered from section_extraction fields
# --------------------------------------------------------------------------- #
async def handle_section_detail_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    kind = parse_section_detail_query(message)
    if kind is None or kind not in DOCUMENT_KINDS:
        return None
    model, label = DOCUMENT_KINDS[kind]
    doc = (
        await db.execute(
            select(model)
            .where(
                model.user_id == user_id,  # type: ignore[attr-defined]
                model.content.is_not(None),  # type: ignore[attr-defined]
            )
            .order_by(
                model.created_at.desc().nulls_last(),  # type: ignore[attr-defined]
                model.id.desc(),  # type: ignore[attr-defined]
            )
            .limit(1)
        )
    ).scalars().first()
    if doc is None:
        return {
            "reply": (
                f"I couldn't find any {label} with extracted details in your "
                "records yet."
            ),
            "action": "none",
            "provenance": {"path": "section_detail", "kind": kind, "found": 0},
        }
    ai = (doc.content or {}).get("ai") or {}
    title = (
        (ai.get("classification") or {}).get("title")
        or (doc.filepath or "").rsplit("/", 1)[-1]
        or label
    )
    fields = ((ai.get("section_extraction") or {}).get("fields")) or {}
    if not fields:
        state = ai.get("state") or "pending"
        return {
            "reply": (
                f"\"{title}\" doesn't have extracted details yet "
                f"(processing state: {state}). Ask me again once it's done."
            ),
            "action": "none",
            "provenance": {"path": "section_detail", "kind": kind,
                           "state": state},
        }
    lines = [f"Here's what was read from \"{title}\":"]
    for key, value in list(fields.items())[:10]:
        if value in (None, "", []):
            continue
        lines.append(f"• {str(key).replace('_', ' ')}: {value}")
    flags = ((ai.get("section_extraction") or {}).get("flags")) or []
    if flags:
        lines.append(
            "Note: "
            + ", ".join(str(f).replace("_", " ") for f in flags[:3]) + "."
        )
    return {
        "reply": "\n".join(lines),
        "action": "none",
        "provenance": {"path": "section_detail", "kind": kind,
                       "document_id": doc.id},
    }


# --------------------------------------------------------------------------- #
# Manual tracker lookups
# --------------------------------------------------------------------------- #
_TRACKER_PHRASE = {
    "water": ("glass", "glasses"),
    "coffee": ("cup", "cups"),
    "tea": ("cup", "cups"),
    "alcohol": ("drink", "drinks"),
    "smoking": ("cigarette", "cigarettes"),
}
_PERIOD_WORDS = {"week": "7 days", "month": "30 days", "year": "year"}


async def handle_tracker_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    """Answer "how much water did I drink this week?" from the reader's logs.

    These reached the LLM before: 4-12s and a paraphrase, for a number the
    reader logged themselves and we already read for the [P] block. A lookup is
    exact and roughly 150ms.
    """
    query = parse_tracker_query(message)
    if query is None:
        return None

    since = window_start(query.period)
    window = _PERIOD_WORDS.get(query.period, "7 days")

    if query.source == "medications":
        meds = await active_medications(db, user_id)
        if not meds:
            reply = (
                "I don't have any current medications on record for you. "
                "Adding them under Medications lets me check them against "
                "anything you ask about."
            )
        else:
            reply = (
                "Your current medications on record are: "
                + "; ".join(meds)
                + ". Private entries are not included, and this reflects what "
                "is recorded here rather than anything I can verify myself."
            )
        return {
            "reply": reply,
            "action": "discuss_with_prescriber",
            "provenance": {
                "path": "tracker_query",
                "source": "medications",
                "count": len(meds),
            },
        }

    if query.source == "lifestyle":
        totals = await lifestyle_totals(db, user_id, since)
        amount = totals.get(query.key)
        if not amount:
            reply = (
                f"You have not logged any {query.key} in the past {window}. "
                "If you have been tracking it elsewhere, adding it here lets me "
                "include it next time."
            )
        else:
            singular, plural = _TRACKER_PHRASE.get(query.key, ("entry", "entries"))
            noun = singular if float(amount) == 1 else plural
            reply = (
                f"You have logged {_g(amount)} {noun} of {query.key} in the past "
                f"{window}. That is what is on record here, not a complete "
                "picture of your intake."
            )
    else:
        metrics = await latest_manual_metrics(db, user_id, since)
        point = metrics.get(query.key)
        if point is None:
            reply = (
                f"You have no {query.key} entries in the past {window}. "
                "Once some are logged I can pull them up here."
            )
        else:
            unit = point.unit or _MANUAL_UNIT.get(query.key, "")
            when = point.at.strftime("%d %b") if point.at else "recently"
            reply = (
                f"Your most recent {query.key} entry is {_g(point.value)} "
                f"{unit}, recorded {when}."
            )

    return {
        "reply": reply,
        "action": "self_care",
        "provenance": {
            "path": "tracker_query",
            "source": query.source,
            "metric": query.key,
            "period": query.period,
        },
    }


# --------------------------------------------------------------------------- #
# Medication commands — the write is mhn-spring's (add/stop/remove a course)
# --------------------------------------------------------------------------- #
# Copy that never CONFIRMS a save that did not happen: a write failure says so.
_MED_UNAVAILABLE = (
    "I can't update your medications from here right now. You can add or "
    "change them in the Medications section of the app."
)


_SLOT_WORDS = {"M": "morning", "A": "afternoon", "E": "evening", "N": "night"}
_TIMES_WORD = {1: "once", 2: "twice", 3: "three times", 4: "four times"}


def _schedule_phrase(*, is_prn: bool, schedule_pattern: str | None) -> str:
    """A human description of the dosing, for an honest confirmation reply."""
    if is_prn or not schedule_pattern:
        return "as needed"
    slots = [s for s in schedule_pattern if s in _SLOT_WORDS]
    if not slots:
        return "as needed"
    when = " and ".join(_SLOT_WORDS[s] for s in slots)
    times = _TIMES_WORD.get(len(slots), f"{len(slots)} times")
    return f"{times} daily ({when})"


async def perform_medication_write(
    db: AsyncSession, user_id: uuid.UUID, action: str, name: str, *,
    strength: str | None = None, is_prn: bool = False,
    schedule_pattern: str | None = None, day_pattern: str = "daily",
) -> dict:
    """Do one medication write AS the reader and return a validator-safe reply.

    The structured entry point shared by the deterministic parser (legacy) and
    the agentic add/stop/remove tools. Davi never writes the row — it calls
    mhn-spring's MedicineController. A write that does not land is reported
    honestly, so the model is never left to invent a false confirmation.
    """
    from app.medicines.service import (
        _resolve,
        add_course,
        delete_course,
        stop_course,
    )

    if action in ("remove_all", "stop_all"):
        # Every course matching the name, one confirmed sweep (duplicates on
        # the list are indistinguishable by name, so per-item prompts would
        # be unanswerable). Each write still reports honestly.
        base_action = action.split("_", 1)[0]
        resolved = await _resolve(
            user_id, name, active_only=(base_action == "stop"))
        matches = list(resolved.courses) if resolved.courses else (
            [resolved.course] if resolved.course else [])
        from app.medicines.service import _request

        done, failed = 0, 0
        for course in matches:
            # Delete/stop by the RESOLVED id, not by re-resolving the name —
            # each removal changes what the name would resolve to.
            path = (f"/medicine/courses/{course.tracking_id}/stop"
                    if base_action == "stop"
                    else f"/medicine/courses/{course.tracking_id}")
            got = await _request(
                "POST" if base_action == "stop" else "DELETE", path, user_id)
            if got is not None and got[0] in (200, 204):
                done += 1
            else:
                failed += 1
        verb = "Stopped" if base_action == "stop" else "Removed"
        prov = {"path": "medication_command", "action": action, "name": name,
                "ok": failed == 0 and done > 0,
                "reason": None if failed == 0 else "partial"}
        if done and not failed:
            reply = (f"{verb} all {done} matching entries of {name}. You can "
                     "see the change in the Medications section.")
        elif done:
            reply = (f"{verb} {done} of {done + failed} matching entries — "
                     f"{failed} could not be updated just now. Please check "
                     "the Medications section.")
        else:
            reply = ("I couldn't update those just now — please try again in "
                     "a moment, or use the Medications section of the app.")
        return {"reply": reply,
                "action": "medication_updated" if done else "none",
                "provenance": prov}

    if action == "add":
        result = await add_course(
            user_id, name, strength=strength, is_prn=is_prn,
            schedule_pattern=schedule_pattern, day_pattern=day_pattern,
        )
    elif action == "stop":
        result = await stop_course(user_id, name)
    else:
        result = await delete_course(user_id, name)

    prov = {"path": "medication_command", "action": action,
            "name": name, "ok": result.ok, "reason": result.reason}

    if result.ok:
        shown = (result.course.name if result.course and result.course.name
                 else name)
        if action == "add":
            # Never re-append a strength the shown name already ends with —
            # a bare-number strength lives inside the name ("dolo 650"), and
            # the confirmation read "Added dolo 650 650" (live bug).
            dose = (f" {strength}"
                    if strength
                    and not shown.lower().endswith(strength.lower())
                    else "")
            sched = _schedule_phrase(
                is_prn=is_prn, schedule_pattern=schedule_pattern
            )
            reply = (f"Added {shown}{dose}, {sched}, to your medications. You "
                     "can see it in the Medications section.")
        elif action == "stop":
            reply = (f"Marked {shown} as stopped. It will no longer show as an "
                     "active medication.")
        else:
            reply = f"Removed {shown} from your medications."
        return {"reply": reply, "action": "medication_updated", "provenance": prov}

    # Not completed — be honest about which failure it was.
    if result.reason in ("not_configured", "no_token"):
        reply = _MED_UNAVAILABLE
    elif result.reason == "not_found":
        reply = (f"I couldn't find an active '{name}' in your medications, "
                 "so there was nothing to change. You can check the list in the "
                 "Medications section.")
    elif result.reason == "ambiguous":
        names = ", ".join(c.name for c in result.courses[:4])
        reply = (f"You have more than one medication matching '{name}' "
                 f"({names}). Which one did you mean?")
    else:
        reply = ("I couldn't update that just now — please try again in a "
                 "moment, or use the Medications section of the app.")
    return {"reply": reply, "action": "none", "provenance": prov}


async def handle_medication_command(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    """"Add metformin 500 mg" / "stopped my amoxicillin" / "remove atorvastatin".

    The legacy deterministic path: parse the phrase, then perform the write.
    Returns None only when the message is not a medication command. The agentic
    engine reaches the same writes through the add/stop/remove tools, which pass
    structured frequency data ``perform_medication_write`` accepts directly.
    """
    cmd: MedicationCommand | None = parse_medication_command(message)
    if cmd is None:
        return None
    return await perform_medication_write(
        db, user_id, cmd.action, cmd.name,
        strength=cmd.strength, is_prn=cmd.is_prn,
    )

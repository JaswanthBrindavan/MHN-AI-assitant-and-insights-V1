"""Handlers for the deterministic chat data-abilities.

Each returns a plain dict {reply, action, provenance, visual?} or None (not
applicable / nothing found → caller falls through). All replies are
validator-safe and never diagnostic; tracker writes always confirm what was
recorded. Everything here is deterministic — no LLM.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TypeVar

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
    TrackerQuery,
    param_tokens,
    parse_ai_result_query,
    parse_correlation_query,
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
from app.chat.correlation import (
    WINDOW_DAYS,
    co_occurrence,
    render_co_occurrence,
)
from app.coredata.service import (
    _MANUAL_UNIT,
    _RESOURCE_TYPE,
    DOCUMENT_KINDS,
    HRV_SIBLING,
    WearablePoint,
    active_medications,
    add_lifestyle_log,
    allergy_rank,
    calendar_window,
    canonical_amount,
    first_lifestyle_day,
    format_wearable,
    latest_body_measurement,
    latest_documents,
    latest_lifestyle_day,
    latest_manual_metrics,
    latest_vital,
    latest_vitals,
    lifestyle_calendar_total,
    lifestyle_days,
    lifestyle_phrase,
    lifestyle_totals,
    list_family_connections,
    medical_records,
    plural_unit,
    recent_doctor_consults,
    recent_lab_values,
    resolve_family_member,
    resolve_family_member_by_name,
    sahha_meta,
    vital_series,
    wearable_display,
    wearable_latest,
    wearable_totals,
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
from app.models.coredata import MedicalCondition, Report, UnclassifiedFile
from app.rag.retrieval import RetrievedChunk, resolve_scope
from app.telemetry import record_fail_open

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

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
    """Compose a safe reply from a backend verdict (thp_age_range).

    Two zones since mhn-spring's V28: normal → reassure; warn → consult a
    doctor. Never diagnoses.

    The third zone ("well above … seek medical advice promptly", action
    ``seek_care_promptly``) is gone with the columns that drove it — see
    ``app.health.reference._classify_bands`` for why that escalation was
    never real and is not being replaced. Out-of-range now answers the same
    way the rest of the app does (an abnormal report flag also routes to
    ``discuss_with_clinician``), and ``seek_care_promptly`` still reaches the
    API from the triage floor, which is the escalation that means something.
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
    lead = (
        f"{art} {v.label} of {reading} is {word} the usual range for "
        f"your age ({ideal}). Please consult your doctor to review it."
    )
    return {
        "reply": f"{lead} {_NOT_A_DIAGNOSIS_LINE}",
        "action": "discuss_with_clinician",
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


# A wearable number is never graded. Davi has no reference ranges for sleep,
# steps, HRV or a wrist-measured resting heart rate, and the client contract
# forbids putting a band, grade or traffic light on one -- 48 bpm in a trained
# reader is not a finding. It lives here, next to the handler both engines
# reach, so the reader's own typed phrasing is covered too; the tool executor
# checks its structured `metric` argument against the same pattern, because a
# metric this parser cannot read ("hrv") would otherwise slip past.
# A bare clinic pulse ("my heart rate is 72") is untouched.
_WEARABLE_GRADE_RE = re.compile(
    r"\bresting heart rate\b|\brhr\b|\bhrv\b|"
    r"\bheart rate variability\b|\bsleep\b|\bsteps?\b",
    re.IGNORECASE,
)
_NO_WEARABLE_RANGE: dict = {
    "reply": (
        "I don't have a reference range for wearable readings like resting "
        "heart rate, heart rate variability, sleep or steps, so I can't tell "
        "you whether that figure is high or low. What is normal there depends "
        "on the device and on the person. I can show you what your device "
        "recorded, and your doctor is the right person to say what it means "
        "for you."
    ),
    "action": "discuss_with_clinician",
    "provenance": {"path": "value_check", "declined": "wearable_no_range"},
}


async def handle_value_check(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    session_id: uuid.UUID | None = None,
    *,
    stated: StatedValue | None = None,
) -> dict | None:
    """Deterministic reference-range check.

    ``stated`` is the parsed reading; a TOOL CALL passes it directly rather
    than synthesising an English sentence for `parse_stated_value` to re-read.
    That round trip is what made "blood_sugar" answer "nothing on file" and
    "random glucose" get judged against the FASTING band.

    1. A value stated in THIS message → classify it directly.
    2. A bare timing clarification ("fasting", "after a meal") that answers an
       earlier "my sugar is 117" → recall that value and re-classify against the
       fasting/post-meal range. This makes the follow-up deterministic rather
       than relying on the model to notice the recent conversation.
    """
    stated = stated or parse_stated_value(message)
    if stated is not None:
        # Gated on a PARSED value on purpose: an unqualified match would swallow
        # "how much sleep did I get this week", which is a lookup, not a grade.
        if _WEARABLE_GRADE_RE.search(message):
            return _NO_WEARABLE_RANGE
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
    # Age AND sex, in one query: 28 of the seeded parameters band by both, and
    # grading a woman's HDL against the male range is a wrong answer about her
    # own body.
    age, sex = await health_reference.reader_bands(db, user_id)
    verdict = await health_reference.evaluate_backend(
        db, metric, value, age, sex
    )
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
    db: AsyncSession, user_id: uuid.UUID, message: str,
    *,
    query: DocumentQuery | None = None,
) -> dict | None:
    """List stored documents. ``message`` is parsed unless ``query`` is given.

    A TOOL CALL already has the kinds and the owner as structured data and
    passes ``query`` directly. It must NOT hand us a rebuilt English sentence:
    the parser requires an ownership marker (my/our/all/the/every), a
    synthesised "show me report" carries none, and every document tool call
    silently returned nothing.
    """
    if query is None:
        query = parse_document_query_fuzzy(message)
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
    # mhn-spring stores water and alcohol in MILLILITRES and rejects any other
    # unit with a 400; its rollups sum `quantity` whatever the unit says. A
    # vessel with no sanctioned size in `drink_serving_size` is asked about,
    # never guessed -- the row would land in the reader's own chart in the app.
    # The DRINK, not the roll-up bucket: a bottle is 330 ml of beer and 750 of
    # wine, and the reply has to name what the reader said or the substitution
    # it exists to let them correct is invisible to them.
    named = add.kind if add.kind not in ("", "drink", "drinks") else add.log_type
    canonical = canonical_amount(add.log_type, add.quantity, add.unit, named)
    if canonical is None:
        vessel = f"{add.unit} of {named}" if add.unit != named else named
        return {
            "reply": (
                f"I track {named} in millilitres, and I don't have a standard "
                f"size for a {vessel}. Tell me roughly how much it was and "
                "I'll log it — a glass of water is about 250 ml, a beer "
                "bottle 330 ml, a glass of wine 150 ml."
            ),
            "action": "none",
            "provenance": {
                "path": "tracker_add",
                "log_type": add.log_type,
                "kind": named,
                "declined": "no_sanctioned_size",
                "unit": add.unit,
            },
        }
    logged_at = utcnow() - timedelta(days=add.day_offset)
    row = await add_lifestyle_log(
        db, user_id, add.log_type, add.quantity, add.unit, logged_at, named
    )
    day = {0: "today", 1: "yesterday", 2: "the day before yesterday"}.get(
        add.day_offset, f"{add.day_offset} days ago"
    )
    qty = f"{add.quantity:g}"
    unit = plural_unit(add.unit, add.quantity)
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
    what = f"{qty} {unit}" if unit_is_the_kind else f"{qty} {unit} of {named}"
    # Echo the stored amount too when converting changed the NUMBER, so the
    # reader can see what their tracker will show and correct it if the
    # standard size is not what they drank. Not when only the noun changed
    # ("2 cigarettes" is stored as 2 `count`, and "2 count" says nothing).
    stored_qty, stored_unit = canonical
    if stored_qty != add.quantity:
        what += f" ({stored_qty:g} {stored_unit})"
    return {
        "reply": (
            f"Logged: {what} for {day}. "
            f"You can see this in your lifestyle tracker.{note}"
        ),
        "action": "logged",
        "provenance": {
            "path": "tracker_add",
            "log_type": add.log_type,
            "kind": named,
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
                    source="vital",
                    metric=query.metric,
                    window_days=30,
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
# Health summary - every source the reader's own record actually holds
# --------------------------------------------------------------------------- #
#: Wearable metrics a summary reports, in reading order. HRV is ONE entry, not
#: two: SDNN and RMSSD measure the same thing and a device reports whichever it
#: reports, so the sibling is tried only when the first is empty.
_SUMMARY_WEARABLES = (
    "sleep_duration",
    "steps",
    "heart_rate_resting",
    "heart_rate_variability_sdnn",
)

_PERIOD_LABEL = {"week": "past week", "month": "past month", "year": "past year"}
_PERIOD_DAYS = {"week": 7, "month": 30, "year": 365}

# Keep the reply readable on a phone. A reader with thirty rows gets the first
# few and an honest count, never a wall.
_MAX_LISTED = 6


async def _section(
    db: AsyncSession,
    failed: list[str],
    name: str,
    factory: Callable[[], Awaitable[_T]],
) -> _T | None:
    """Run one summary read. A failure degrades THAT section, nothing else.

    A failed read records its name in ``failed`` and returns None, so the
    caller can tell "this could not be read" from "there is nothing here".
    Printing the second for the first is the one failure this handler must
    never have: a crashed allergy query must not read as "you have none".

    The SAVEPOINT is not decoration. On PostgreSQL a failed statement aborts
    the whole transaction, so a bare try/except would leave every LATER section
    failing too, and one broken reader would cost the reader the entire
    summary. The suite runs on aiosqlite, which tolerates the missing
    savepoint, so that defect would be invisible locally.
    """
    try:
        async with db.begin_nested():
            return await factory()
    except Exception:  # noqa: BLE001 - a section must never break the reply
        logger.warning("health summary section %r failed", name, exc_info=True)
        record_fail_open(f"summary_{name}")
        failed.append(name)
        return None


def _listed(parts: list[str]) -> str:
    """Join at most ``_MAX_LISTED`` entries, counting the rest honestly."""
    shown = "; ".join(parts[:_MAX_LISTED])
    extra = len(parts) - _MAX_LISTED
    return shown if extra <= 0 else f"{shown}; and {extra} more"


def _record_phrase(row: MedicalCondition) -> str:
    """``name (status)`` - the status the ROW carries, never one inferred.

    'Improving' and 'Stable' are in no column and are never emitted; a row with
    no status is named without one rather than assumed active.
    """
    status = (row.status or "").strip().lower()
    return f"{row.name} ({status})" if status else str(row.name)


def _allergy_phrase(row: MedicalCondition) -> str:
    detail = ", ".join(
        p for p in ((row.reaction or "").strip(), (row.severity or "").strip()) if p
    )
    return f"{row.name} — {detail}" if detail else str(row.name)


async def _wearable_headlines(
    db: AsyncSession, user_id: uuid.UUID, window_open: date
) -> tuple[list[tuple[str, WearablePoint]], bool]:
    """The latest weekly rollup for each summary metric that has one.

    ONE query for all of them (``wearable_latest``) rather than one per metric:
    a summary is the turn that asks for everything, and five index lookups are
    five network hops. Same headline formula as the tracker answer, so the two
    cannot print different numbers for the same week.

    ``window_open - 7 days`` is ``_fresh_buckets``' rule expressed in SQL: a
    weekly bucket counts while any of its days fall in the asked-about window,
    so a device that stopped syncing last year does not answer "this week".

    Returns ``(rows, stale)``. When the window holds nothing, the UNBOUNDED
    read runs and ``stale`` is True: a device that lapsed three weeks ago has
    rows, and dropping them made the whole summary fall into the "I don't have
    any logged data" branch — absence of a record, asserted out of a window
    that simply did not reach them. ``_vitals_line`` already refuses to do
    that for vitals; this is the same rule applied to the other window-scoped
    section.
    """
    wanted: list[str] = list(_SUMMARY_WEARABLES)
    for metric in _SUMMARY_WEARABLES:
        if metric in HRV_SIBLING:
            wanted.append(HRV_SIBLING[metric])

    def _pick(latest: dict[str, WearablePoint]) -> list[tuple[str, WearablePoint]]:
        found: list[tuple[str, WearablePoint]] = []
        for metric in _SUMMARY_WEARABLES:
            # SDNN and RMSSD are two measures of the same thing and a device
            # reports whichever it reports. Prefer the asked-for key, fall back
            # to the sibling, and NAME whichever answered -- never merge them.
            for key in (metric, HRV_SIBLING.get(metric)):
                if key and key in latest:
                    found.append((key, latest[key]))
                    break
        return found

    found = _pick(
        await wearable_latest(
            db, user_id, wanted, since=window_open - timedelta(days=7)
        )
    )
    if found:
        return found, False
    # `since=date.min` is "at any age": the honest unbounded read, whose whole
    # job here is to tell a lapsed device from an absent one.
    return _pick(await wearable_latest(db, user_id, wanted, since=date.min)), True


def _wearable_line(
    found: list[tuple[str, WearablePoint]], *, stale: bool = False,
    window: str = "past week",
) -> str:
    """The week's device readings — each one saying whether it is a sum.

    Sleep and steps are WEEK TOTALS; resting heart rate and HRV are means over
    the week. Printed in one undifferentiated list they read as the same kind
    of figure, and a reader quoting "sleep 46.7 h" to a clinician cannot tell
    whether that is a night, an average or a week. Same two words
    ``handle_tracker_query`` uses for the identical numbers.
    """
    parts: list[str] = []
    for key, point in found:
        label, _unit, is_sum = sahha_meta(key)
        text = format_wearable(key, point.value)
        # "52,300 steps" already carries its noun, and the label IS that noun.
        if text.endswith(label):
            text = text[: -len(label)].strip()
        # The same part-week signal `handle_tracker_query` prints, from the
        # same column, so the two readers of this row cannot describe the same
        # week differently. `entries` is readings and cannot say this.
        span = "" if point.days_counted >= 7 else (
            f" (a part week: {point.days_counted} of 7 days)"
        )
        parts.append(
            f"{label} {'totalled' if is_sum else 'averaged'} {text}{span}"
        )
    weeks = {p.bucket_start for _, p in found}
    when = (
        f"week of {next(iter(weeks)).strftime('%d %b %Y')}"
        if len(weeks) == 1
        else "the most recent week on record for each"
    )
    if stale:
        # Nothing in the asked-about window, but the rows exist. Name the date
        # rather than letting the section read as "no device data".
        return (
            f"Wearable readings: nothing in the {window} — your most recent "
            f"is the {when}: {'; '.join(parts)}. That is what your device "
            "recorded, not a measurement I can verify, and I have no "
            "reference range to say whether any of it is high or low."
        )
    return (
        f"From your connected device ({when}): {'; '.join(parts)}. That is what "
        "your device recorded, not a measurement I can verify, and I have no "
        "reference range to say whether any of it is high or low."
    )


def _vital_value(point) -> str:
    """One vital as the reader should see it: value, pair, unit."""
    value = (
        f"{point.value:g}/{point.secondary:g}"
        if point.secondary is not None
        else f"{point.value:g}"
    )
    return f"{value} {point.unit or ''}".strip()


def _vitals_line(vitals: dict, since: datetime, window: str) -> str | None:
    """The in-window vitals, or -- when every reading predates the window --
    a line saying the record EXISTS but is older.

    Returning None for a stale-but-present read put "vitals" in the summary's
    "Not on record for you" list, which is the one wording rule 1 of
    ``handle_summary_query`` forbids: the reader's blood pressure IS on
    record, it is just not from this week.
    """
    shown: list[str] = []
    stale: list[tuple[str, object]] = []
    for vital_type, display in (
        ("blood_pressure", "blood pressure"),
        ("blood_sugar", "blood sugar"),
        ("heart_rate", "heart rate"),
        ("spo2", "SpO2"),
    ):
        point = vitals.get(vital_type)
        if point is None:
            continue
        if point.at < since:
            stale.append((display, point))
            continue
        shown.append(
            f"latest {display} {_vital_value(point)}".strip()
        )
    if shown:
        return "Vitals: " + "; ".join(shown) + "."
    if stale:
        display, point = max(stale, key=lambda s: s[1].at)  # type: ignore[attr-defined]
        return (
            f"Vitals: nothing logged in the {window} — your most recent is a "
            f"{display} of {_vital_value(point)} from "  # type: ignore[arg-type]
            f"{point.at.strftime('%d %b %Y')}."  # type: ignore[attr-defined]
        )
    return None


def _lifestyle_chart(totals: dict, label: str, period: str) -> dict | None:
    """The single-unit lifestyle bars - the headline chart when no device.

    One chart, ONE unit. The payload contract carries a single ``unit`` for the
    whole series, so cups drawn beside millilitres label one of them wrong, and
    bars in different units were never comparable anyway. The most common unit
    wins and the rest are reported in the text but not plotted.
    """
    charted = sorted(totals.items())
    if not charted:
        return None
    unit = Counter(t.unit for _, t in charted).most_common(1)[0][0]
    bars = [(k, t) for k, t in charted if t.unit == unit]
    return chart_payload(
        "bar",
        f"Lifestyle totals ({unit}) - {label}",
        [k for k, _ in bars],
        [t.total for _, t in bars],
        unit=unit,
        source="lifestyle",
        window_days=_PERIOD_DAYS.get(period, 7),
    )


async def handle_summary_query(
    db: AsyncSession, user_id: uuid.UUID, message: str,
    *,
    query: SummaryQuery | None = None,
) -> dict | None:
    """Everything on the reader's record, in one deterministic answer.

    Lifestyle logs, wearable rollups, conditions, medications, allergies,
    vitals and lab values - each read independently and each fail-open, so one
    broken source costs its own line and never the reply.

    Three rules the wording is built on:

    * **Absence is stated as absence of a RECORD.** "Not on record" for
      allergies, never "no allergies" - the second is a clinical claim made out
      of an empty table. And for a WINDOW-SCOPED section (lifestyle, wearable,
      vitals) not even that: an empty week is not an empty record, so those say
      "nothing logged in the past week" and `_vitals_line` names the date of
      the most recent reading it did find.
    * **A failed read is never an empty one.** ``_UNAVAILABLE`` gets its own
      sentence, so a crashed query can never read as "you have none".
    * **Nothing is graded.** Records framing throughout ("your records list"),
      only the status the row carries, and no band on a wearable number - there
      is no reference range for one and the client contract forbids it.

    ``message`` is parsed unless ``query`` is given: a tool call already HAS the
    period as structured data and passes it, rather than synthesising an English
    sentence for this parser to re-read.
    """
    if query is None:
        query = parse_summary_query(message)
    if query is None:
        return None
    since = window_start(query.period)
    label = _PERIOD_LABEL.get(query.period, "past week")

    # Every read fires here and ONLY here, behind the summary parse. An
    # ordinary turn never reaches this function, so its round-trip cost is zero
    # unless the reader actually asked for a summary.
    failed: list[str] = []
    totals = await _section(
        db, failed, "lifestyle", lambda: lifestyle_totals(db, user_id, since)
    )
    wearable, wearable_stale = await _section(
        db, failed, "wearable",
        lambda: _wearable_headlines(db, user_id, since.date()),
    ) or ([], False)
    records = await _section(
        db, failed, "records", lambda: medical_records(db, user_id)
    )
    meds = await _section(
        db, failed, "medications", lambda: active_medications(db, user_id)
    )
    vitals = await _section(
        db, failed, "vitals",
        lambda: latest_vitals(
            db, user_id, ("blood_pressure", "blood_sugar", "heart_rate", "spo2")
        ),
    )
    labs = await _section(
        db, failed, "labs", lambda: recent_lab_values(db, user_id)
    )

    lines: list[str] = []
    missing: list[str] = []
    nothing_recent: list[str] = []
    unavailable: list[str] = []
    sections: list[str] = []

    def _place(name: str, read: str, line: str | None, *, scoped: bool = False) -> None:
        """One section -> exactly one of the four buckets.

        ``read`` names the query it came from, so a section whose READ failed
        is reported as unreadable even though its value is the same empty
        list an account with no rows would give.

        ``scoped`` marks a section whose query is WINDOW-limited. Its emptiness
        means "nothing this week", not "nothing on record", and saying the
        second breaks rule 1 for a reader whose last reading is a month old.
        """
        if read in failed:
            unavailable.append(name)
        elif line:
            lines.append(line)
            sections.append(name)
        elif scoped:
            nothing_recent.append(name)
        else:
            missing.append(name)

    _place(
        "lifestyle entries", "lifestyle",
        "Lifestyle entries: "
        + ", ".join(lifestyle_phrase(t) for _, t in sorted(totals.items()))
        + "." if totals else None,
        scoped=True,
    )
    _place(
        "wearable readings", "wearable",
        _wearable_line(wearable, stale=wearable_stale, window=label)
        if wearable else None,
        scoped=True,
    )

    # Allergies here are ALL categories (food, environmental, medication), not
    # only the medication ones the drug path reads.
    conditions = [r for r in records or [] if (r.type or "condition") == "condition"]
    allergies = sorted(
        (r for r in records or [] if r.type == "allergy"), key=allergy_rank
    )
    _place(
        "conditions", "records",
        "Your records list: " + _listed([_record_phrase(r) for r in conditions]) + "."
        if conditions else None,
    )
    _place(
        "allergy information", "records",
        "Allergies on record: " + _listed([_allergy_phrase(r) for r in allergies]) + "."
        if allergies else None,
    )
    _place(
        "current medications", "medications",
        "Current medications on record: " + _listed(list(meds)) + "."
        if meds else None,
    )
    _place(
        "vitals", "vitals",
        _vitals_line(vitals, since, label) if vitals else None,
        scoped=True,
    )
    _place(
        "lab values", "labs",
        "Recent lab values: "
        + _listed([f"{v.name} {v.value} {v.unit or ''}".strip() for v in labs])
        + "." if labs else None,
    )

    if not lines and not unavailable:
        return {
            "reply": (
                f"I don't have any logged data for the {label} yet. Log vitals "
                "or lifestyle entries and I can build your summary."
            ),
            "action": "none",
            "provenance": {"path": "health_summary", "period": query.period,
                           "empty": True},
        }

    # The headline trend: the wearable's daily bars when a device is connected
    # (a real series over time), the lifestyle totals otherwise. Chart text
    # passes through no validator on either engine, so both titles are
    # template-generated and never composed from model or corpus text.
    visual = None
    if wearable:
        key, point = wearable[0]
        # Same table and same failure mode as the section above, so it reuses
        # that section's name rather than paying for another savepoint.
        visual = await _section(
            db, failed, "wearable",
            lambda: _wearable_chart(db, user_id, key, sahha_meta(key)[0], point),
        )
    elif totals:
        visual = _lifestyle_chart(totals, label, query.period)

    body = [f"Here's your health summary for the {label}:"]
    body += [f"\u2022 {ln}" for ln in lines]
    if nothing_recent:
        # Window-scoped: the rows may well exist, just not in this window.
        # "Not on record" here would assert an absence the query never checked.
        body.append(
            f"Nothing logged in the {label} for: "
            + ", ".join(nothing_recent) + "."
        )
    if missing:
        # Never "you have no X": an empty table is not a clinical finding.
        body.append("Not on record for you: " + ", ".join(missing) + ".")
    if unavailable:
        body.append(
            "I couldn't read these just now, so they are missing rather than "
            "empty: " + ", ".join(unavailable) + "."
        )
    body.append(_NOT_MEDICAL_ADVICE)

    return {
        "reply": "\n".join(body),
        "action": "review_with_clinician",
        "provenance": {
            "path": "health_summary",
            "period": query.period,
            "sections": sections,
            "missing": missing,
            "nothing_recent": nothing_recent,
            "unavailable": unavailable,
        },
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


def format_suggestions(
    rows_content: list[str], display_names: list[str]
) -> tuple[str, list[int]]:
    """Render suggestions chunks as clean, sectioned, readable text.

    Returns ``(text, indices)`` — the indices of ``rows_content`` that
    actually supplied a RENDERED bullet. ONE pass, one answer: the caller used
    to build its citation list from a parallel slice of the query result, and
    this renderer drops rows in three ways (`order[:4]`, `bullets[:4]`, and
    anything `_parse_suggestion_line` cannot read), so a reply carrying one
    chunk's bullets cited four chunks. That is the same drift the `Used`
    threading closed one level up, re-opened by a handler computing its own
    list from the wrong thing.
    """
    sections: dict[str, list[str]] = {}
    origin: dict[tuple[str, str], int] = {}
    order: list[str] = []
    for index, content in enumerate(rows_content):
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
                    origin[(header, b)] = index

    if not sections:
        return "", []
    names = " and ".join(display_names[:2]) if display_names else "your conditions"
    parts = [
        f"Based on our clinically reviewed profiles for {names}, "
        "here's what generally helps:"
    ]
    used: set[int] = set()
    for header in order[:_MAX_SECTIONS]:
        bullets = sections[header][:_MAX_BULLETS_PER_SECTION]
        used.update(origin[(header, b)] for b in bullets)
        parts.append(
            f"**{header}**\n" + "\n".join(f"• {b}" for b in bullets)
        )
    parts.append(
        "These are general, educational pointers — not a personal "
        "prescription. Your doctor can tailor them to you."
    )
    return "\n\n".join(parts), sorted(used)


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
    reply, rendered = format_suggestions([r.content for r in rows], display_names)
    if not reply:
        return None
    # The rows the RENDERER emitted, not the rows the query returned. A long
    # section is chunked as `suggestions`, `suggestions_2`, ... and the first
    # chunk can supply all four headers on its own, leaving the other three
    # contributing nothing to the text while the reply cited all four.
    used_rows = [rows[i] for i in rendered]
    return {
        "reply": reply,
        "action": "discuss_with_clinician",
        "provenance": {
            "path": "mcp_suggestions",
            "conditions": sorted({r.condition_code for r in used_rows}),
            "chunks": [str(r.id) for r in rows],
        },
        # The corpus blocks this reply RENDERED, so the caller cites
        # exactly them. Every other handler answers from the reader's own
        # rows and returns nothing here, which is how a tracker total
        # ends up citing nothing.
        "used_chunks": [
            RetrievedChunk(
                id=str(r.id),
                condition_code=r.condition_code,
                chunk_type=r.chunk_type,
                content=r.content,
                score=1.0,
            )
            for r in used_rows
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
# The whole prepositional phrase, not a bare noun: a calendar window cannot be
# said as "in the past X" without turning a Sunday-to-today total into a
# rolling one in the reader's head.
_PERIOD_PHRASE = {
    "week": "in the past 7 days",
    "month": "in the past 30 days",
    "year": "in the past year",
    "today": "today",
    "yesterday": "yesterday",
    "this_week": "so far this week",
    "last_week": "last week",
}
# Wearable metric → the manual_tracking type that answers it when no device is
# connected. heart_rate_resting has no manual twin but vital_reading does, so
# it falls through to handle_metric_query instead (see below).
_WEARABLE_MANUAL = {"steps": "steps", "sleep_duration": "sleep"}


def _fresh_buckets(
    points: list[WearablePoint], window_open: date
) -> list[WearablePoint]:
    """Drop WEEKLY rollups that ended before the asked-about window opened.

    ``wearable_totals`` returns the last rows that EXIST, at any age. Without
    this a device that stopped syncing a year ago answers "how did I sleep this
    week" with last August's number, in the present tense — and one stale
    weekly row permanently outranks a manual entry logged yesterday, because
    the fall-through fires only on "no rows at all".
    """
    return [
        p for p in points
        if p.bucket_start + timedelta(days=7) >= window_open
    ]


async def _wearable_chart(
    db: AsyncSession, user_id: uuid.UUID, metric: str, label: str,
    headline: WearablePoint,
) -> dict | None:
    """The daily bars for exactly the week the sentence names.

    Not "the last 7 daily rows": those straddle two weekly buckets on every day
    but the week's last, so the bars did not add up to the total printed above
    them, and for a sparse device they could span months under a "last 7 days"
    title. Chart text passes through no validator on either engine, so a wrong
    title is permanent.

    Every day of the week gets a slot, ``None`` where the device recorded
    nothing — the run of slots is the axis for both mobile clients, and a
    measured 0 and an unmeasured day are different readings.
    """
    start = headline.bucket_start
    days = [start + timedelta(days=i) for i in range(7)]
    daily = await wearable_totals(
        db, user_id, metric, grain="day", limit=7,
        since=start, until=start + timedelta(days=7),
    )
    by_day = {d.bucket_start: d for d in daily}
    values: list[float | None] = [
        wearable_display(metric, by_day[d].value)[0] if d in by_day else None
        for d in days
    ]
    if sum(v is not None for v in values) < 2:
        # One bar is not a trend, and chart_payload pads a flat single-bar
        # series by +/-1, which looks like data.
        return None
    return chart_payload(
        "bar",
        f"{label} — week of {start.strftime('%d %b %Y')}",
        [d.strftime("%d %b") for d in days],
        values,
        unit=wearable_display(metric, 0.0)[1],
        source="wearable",
        metric=metric,
        grain="day",
        window_days=7,
    )


async def handle_correlation_query(
    db: AsyncSession, user_id: uuid.UUID, message: str,
) -> dict | None:
    """Answer "does coffee affect my sleep" as a CO-OCCURRENCE, not a cause.

    One logged habit against one wearable reading over the last
    ``WINDOW_DAYS`` finished days: the arithmetic and the wording both live in
    the pure ``app.chat.correlation`` module, and this function only fetches
    the two series and names the metric.

    Runs in the SHARED prologue above the tracker slot, because
    ``parse_tracker_query`` claimed exactly these phrasings and answered them
    as a coffee total. A handler placed in the legacy chain would be dead on
    arrival for the questions it exists to serve, and invisible on the agentic
    engine besides -- the bypass class this codebase keeps rediscovering.

    Never a medication on either side: ``CORRELATION_INPUTS`` is four lifestyle
    log types and nothing else, so no phrasing can reach a drug through here.

    No ``query=`` override and no tool: the prologue placement means BOTH
    engines reach this from the message, so there is nothing for a tool to
    call and no English sentence for an executor to synthesise.
    """
    from app.chat.abilities import CorrelationQuery
    from app.chat.abilities import medication_candidates as _med_cands
    from app.drugs.service import find_drug

    async def _names_a_medication(
        db: AsyncSession, user_id: uuid.UUID, message: str
    ) -> bool:
        """True when an effect question names one of the reader's medicines,
        or anything in the catalogue.

        The reader's own list is checked first and is the more reliable of the
        two: `medicine_master` is populated by mhn-spring's V19 merge and can
        be empty in an environment that has not run it, whereas a medicine
        someone is actually taking is on their record by definition.

        Fails open to False — this decides whether to DECLINE, so a lookup
        failure must not turn an ordinary question into a refusal.
        """
        candidates = _med_cands(message)
        if not candidates:
            return False
        try:
            own = await active_medications(db, user_id)
            names = {
                part.lower()
                for entry in own
                for part in entry.split()
                if len(part) > 3 and part.isalpha()
            }
            if names & set(candidates):
                return True
            for term in candidates:
                if await find_drug(db, term) is not None:
                    return True
        except Exception:  # noqa: BLE001 — never turn a question into a refusal
            logger.warning("medication check failed", exc_info=True)
            return False
        return False
    query = parse_correlation_query(message)
    # The parser is pure, so it declines on medication NOUNS and cannot see a
    # bare brand or generic name. Two things went wrong because of that, and
    # both answered a question the reader did not ask:
    #
    #   "does my metformin affect my sleep"
    #       -> parser None -> the TRACKER slot claimed it -> "you have no
    #          sleep entries in the past 7 days"
    #   "does my metformin affect my sleep when i drink coffee"
    #       -> parsed as coffee-vs-sleep -> answered about COFFEE, with the
    #          drug never mentioned
    #
    # So the catalogue check runs here, where there is a database, and it runs
    # whether or not the parser claimed the turn. It costs nothing on an
    # ordinary message: `medication_candidates` returns empty unless the
    # message is a first-person effect question.
    if query is None or not query.declined:
        if await _names_a_medication(db, user_id, message):
            query = CorrelationQuery(
                input_key="", outcome_metric=None, declined="medication"
            )
    if query is None:
        return None
    if query.declined == "medication":
        # The message names a medicine. Answering it about coffee instead --
        # which is what happened, silently, whenever any habit term also
        # matched -- is answering a different question and never saying so.
        return {
            "reply": (
                "You asked about a medicine there, and I can't line a "
                "medication up against a reading and tell you what it is "
                "doing to you — that is a side-effect question, and it "
                "belongs with the prescriber who knows your full history. "
                "Don't change or stop anything on your own. I can show you "
                "either reading on its own if that would help."
            ),
            "action": "discuss_with_prescriber",
            "provenance": {
                "path": "correlation_query",
                "declined": "medication",
            },
        }
    if query.outcome_metric is None:
        # A pair I cannot read. SAYING so is the whole point: returning None
        # here would drop the message into the tracker slot below, which
        # answered "does coffee affect my blood pressure" with a coffee total.
        return {
            "reply": (
                "I can only line up something you log — water, coffee, tea, "
                "alcohol or smoking — against a daily reading from a connected "
                "device: sleep, steps, resting heart rate or HRV. What you "
                "asked about isn't one of those, so I can't put it beside "
                f"your {query.input_key} log. I can show you either one on its "
                "own if that helps."
            ),
            "action": "self_care",
            "provenance": {
                "path": "correlation_query",
                "input": query.input_key,
                "declined": "unpairable_outcome",
            },
        }

    # Half-open [since, until) ending at TODAY, so today is excluded: a day in
    # progress has partial steps and no sleep yet, and both rollups are rebuilt
    # as late syncs land. Comparing finished days only.
    until = utcnow().date()
    since = until - timedelta(days=WINDOW_DAYS)
    # Never reach back past the day the reader started tracking AT ALL. A day
    # before their first lifestyle row is a day the feature did not exist for
    # them, not a day without the habit -- and counting those in the "did not
    # log" group manufactured a finding out of 21 days of nothing for the most
    # likely reader this feature has: someone who started logging last week.
    # Shortening the window makes `enough` fail on its own, and the refusal
    # branch below already has the right words for that.
    first_logged = await first_lifestyle_day(db, user_id)
    if first_logged is not None and first_logged > since:
        since = first_logged
    window_days = (until - since).days
    logged = set(
        await lifestyle_days(db, user_id, query.input_key, since=since, until=until)
    )

    # SDNN and RMSSD measure the same thing differently and a device reports
    # whichever it reports. Try the asked-for key, then its sibling, and keep
    # whichever ANSWERED -- never merge them, and name the one that did.
    metrics = [query.outcome_metric]
    if query.outcome_metric in HRV_SIBLING:
        metrics.append(HRV_SIBLING[query.outcome_metric])
    finding = None
    for metric in metrics:
        points = await wearable_totals(
            db, user_id, metric, grain="day",
            limit=WINDOW_DAYS, since=since, until=until,
        )
        candidate = co_occurrence(
            query.input_key, metric, logged,
            {p.bucket_start: p.value for p in points},
            window_days=window_days,
        )
        if candidate.enough:
            finding = candidate
            break
        if finding is None or candidate.measured_days > finding.measured_days:
            finding = candidate
    assert finding is not None  # `metrics` is never empty

    label, unit, _is_sum = sahha_meta(finding.outcome_metric)
    return {
        "reply": render_co_occurrence(finding, label=label, unit=unit),
        "action": "self_care",
        "provenance": {
            "path": "correlation_query",
            "input": finding.input_key,
            "outcome": finding.outcome_metric,
            "window_days": finding.window_days,
            "days_with": finding.days_with,
            "days_without": finding.days_without,
            "enough": finding.enough,
            # No chart. The payload contract is single-series (one `metric`,
            # one `unit`), so a two-series comparison would be a second
            # renderer -- and a chart is the fastest way to make a
            # co-occurrence look like a finding.
        },
    }


async def handle_tracker_query(
    db: AsyncSession, user_id: uuid.UUID, message: str,
    *,
    query: TrackerQuery | None = None,
) -> dict | None:
    """Answer "how much water did I drink this week?" from the reader's logs.

    These reached the LLM before: 4-12s and a paraphrase, for a number the
    reader logged themselves and we already read for the [P] block. A lookup is
    exact and roughly 150ms.

    ``message`` is parsed unless ``query`` is given. A TOOL CALL already has the
    metric and the period as structured data and passes ``query`` directly --
    it must NOT synthesise an English sentence for this parser to re-read. That
    is the bug that made every document tool call return nothing.
    """
    if query is None:
        query = parse_tracker_query(message)
    if query is None:
        return None

    # Exactly one of these two is in play: `span` is a half-open calendar
    # [since, until) for yesterday/this_week/last_week, None for the rolling
    # periods, which keep `since` and their existing meaning.
    span = calendar_window(query.period)
    since = window_start(query.period)
    window = _PERIOD_PHRASE.get(query.period, "in the past 7 days")

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

    wearable_reply: str | None = None
    stale_reply: str | None = None
    visual: dict | None = None
    if query.source == "wearable":
        # SDNN and RMSSD are two different HRV measures and a device reports
        # whichever it reports. Try the asked-for key, then its sibling, and
        # name the one that answered -- never merge them, and never tell a
        # reader with seven RMSSD readings that they have no HRV.
        keys = [query.key]
        if query.key in HRV_SIBLING:
            keys.append(HRV_SIBLING[query.key])
        points: list[WearablePoint] = []
        # "yesterday" is the only period the DAILY rollup answers; the calendar
        # weeks pick a weekly bucket by asking which one bucket_start falls in
        # the span. `bucket_start` itself is never re-derived -- it is read as
        # Spring wrote it, and only compared.
        grain = "day" if query.period in ("today", "yesterday") else "week"
        for key in keys:
            if span is not None:
                points = await wearable_totals(
                    db, user_id, key, grain=grain, limit=1,
                    since=span[0], until=span[1],
                )
            else:
                points = _fresh_buckets(
                    await wearable_totals(
                        db, user_id, key, grain="week", limit=1
                    ),
                    since.date(),
                )
            if points:
                query = replace(query, key=key)
                break
        label, _unit, is_sum = sahha_meta(query.key)
        # A device that stopped syncing three weeks ago HAS rows; only the
        # window excludes them. Reporting that as "there is nothing here until
        # one is linked" is false twice over, and it is the exact wording rule
        # 1 of `handle_summary_query` forbids -- `_vitals_line` names the date
        # of the most recent reading instead, and this is that shape.
        if not points:
            for key in keys:
                # The SAME grain the ask used. A "yesterday" question that
                # finds no daily bucket beside a live weekly one is not a
                # lapsed device, and saying so would be its own wrong answer.
                older = await wearable_totals(
                    db, user_id, key, grain=grain, limit=1
                )
                if older:
                    when = f"{older[0].bucket_start:%d %b %Y}"
                    if grain == "week":
                        when = f"the week of {when}"
                    # The VALUE, not just the date. The reading was already
                    # fetched, and "your most recent is from 31 Aug" answers
                    # half a question the reader has to ask again to finish.
                    # It is still framed as NOT the window they asked about.
                    stale_reply = (
                        f"I have no {sahha_meta(key)[0]} from your connected "
                        f"device {window}. Your most recent is "
                        f"{format_wearable(key, older[0].value)} from {when}, "
                        "so it looks like the device has not synced since."
                    )
                    break
        if points:
            p = points[0]
            if span is not None:
                # A calendar ask was served exactly, so no downgrade note and
                # no period rewrite: the receipt names the window that answered.
                note = ""
                when = {
                    "today": f"today ({p.bucket_start:%d %b %Y})",
                    "yesterday": f"yesterday ({p.bucket_start:%d %b %Y})",
                    "this_week":
                        f"so far this week (the week of {p.bucket_start:%d %b %Y})",
                    "last_week":
                        f"last week (the week of {p.bucket_start:%d %b %Y})",
                }[query.period]
            else:
                # The rollups are weekly and daily only. A month or year
                # question gets a week's number, so it must SAY so and the
                # provenance must report the week it actually read -- a receipt
                # claiming "month" beside a week's total is a confident wrong
                # answer.
                note = "" if query.period == "week" else (
                    "I only have weekly totals from your connected device. "
                )
                query = replace(query, period="week")
                when = f"in the week of {p.bucket_start:%d %b %Y}"
            # `entries` is READINGS, not days: a device syncing hourly makes it
            # several times the day count, so it can never say whether a week
            # is complete. `days_counted` is the rollup's own answer to that,
            # and a week in progress reported as a week total is a wrong number.
            partial = "" if grain == "day" or p.days_counted >= 7 else (
                f" That covers {p.days_counted} of 7 days, so it is a "
                "part-week figure rather than a full week."
            )
            wearable_reply = (
                f"{note}Your {label} {'totalled' if is_sum else 'averaged'} "
                f"{format_wearable(query.key, p.value)} {when}, across "
                f"{p.entries} reading{'' if p.entries == 1 else 's'} from your "
                f"connected device.{partial} That is what your device "
                "recorded, not a measurement I can verify."
            )
            # A daily bucket is not a week: `_wearable_chart` lays out the seven
            # days from `bucket_start`, which for "yesterday" would be a chart
            # of the week AFTER the number above it.
            if grain == "week":
                visual = await _wearable_chart(db, user_id, query.key, label, p)
        elif query.key in _WEARABLE_MANUAL:
            # No device data — answer from what they logged by hand, as before.
            query = replace(
                query, source="manual", key=_WEARABLE_MANUAL[query.key]
            )
        elif query.key == "heart_rate_resting":
            # No device data, but a logged pulse is still the honest answer.
            # This USED to return None and let handle_metric_query answer six
            # slots below -- which works only for a caller that HAS a chain
            # underneath it. A tool call has none, and turned the same None
            # into "Nothing on file" while a real reading sat one table away.
            # Serving it here is the one answer both engines can give.
            return await handle_metric_query(
                db, user_id, "what is my latest heart rate"
            )
        else:
            wearable_reply = stale_reply or (
                f"I don't have any {label} readings on record for you. Those "
                "come from a connected wearable, so there is nothing here "
                "until one is linked."
            )

    if wearable_reply is not None:
        reply = wearable_reply
    elif query.source == "lifestyle":
        logged_days = 0
        if query.period == "today":
            # The daily rollup is Spring's and compiles OVERNIGHT, so today's
            # bucket is empty or partial all day. "How much water today" is
            # the one window where the rollup is the wrong table: read the
            # log rows Davi itself writes, since midnight.
            total = (await lifestyle_totals(db, user_id, since)).get(query.key)
        elif span is not None:
            total, logged_days = await lifestyle_calendar_total(
                db, user_id, query.key, since=span[0], until=span[1]
            )
        else:
            total = (await lifestyle_totals(db, user_id, since)).get(query.key)
        # An empty window is not an empty record. When the asked-about period
        # holds nothing, name the last day that DID -- with its figure, which
        # is the whole point (owner: "when yesterdays data is not available
        # then its better to show the latest data"). Only ever OUTSIDE the
        # window asked about, and never presented as the answer to it.
        stale = ""
        if total is None:
            latest = await latest_lifestyle_day(db, user_id, query.key)
            floor = span[0] if span is not None else since.date()
            if latest is not None and latest[0] < floor:
                stale = (
                    f" Your most recent is {lifestyle_phrase(latest[1])} on "
                    f"{latest[0]:%d %b %Y}."
                )
        # The overnight excuse belongs ONLY to a window that still contains
        # today. Applied to `yesterday` or `last_week` it is simply false --
        # the rollup ran days ago, nothing in those windows was logged today,
        # and a reader who genuinely drank no water last week was told their
        # data might still be pending.
        if (
            total is None
            and span is not None
            and query.period != "today"
            and span[1] > utcnow().date()
        ):
            # NOT "you logged nothing": the daily totals are Spring's, compiled
            # overnight, so a row added here today is genuinely absent from
            # them. Claiming the reader logged nothing would be a wrong answer.
            # ponytail: rollup-only, which is what keeps the day boundary in
            # Spring's tracking zone. Reconcile Davi's own same-day writes into
            # the read if "logged it a minute ago" turns out to matter.
            reply = (
                f"I have no {query.key} in your daily totals {window}. Those "
                "are compiled overnight, so anything logged today may not be "
                f"counted yet.{stale}"
            )
        elif total is None and stale:
            reply = f"You have not logged any {query.key} {window}.{stale}"
        elif total is None:
            reply = (
                f"You have not logged any {query.key} {window}. "
                "If you have been tracking it elsewhere, adding it here lets me "
                "include it next time."
            )
        else:
            # A week to date is not a week. Say how many days actually carry a
            # log rather than presenting three days as a weekly total.
            partial = "" if query.period != "this_week" else (
                f" That covers {logged_days} day"
                f"{'' if logged_days == 1 else 's'} so far -- the week is not "
                "over, and today's logs are added when the daily totals "
                "compile overnight."
            )
            # `lifestyle_phrase` carries the unit, and says "2 glasses and 500
            # ml" rather than adding two units into one authoritative-looking
            # number when they do not convert.
            reply = (
                f"You have logged {lifestyle_phrase(total)} {window}."
                f"{partial} That is what is on record here, not a complete "
                "picture of your intake."
            )
    else:
        if span is not None:
            # manual_tracking holds instants and no Spring-assigned day, so a
            # UTC midnight is the only boundary there is. Unbounded above, an
            # "entries yesterday" ask answers with today's reading.
            metrics = await latest_manual_metrics(
                db, user_id,
                datetime.combine(span[0], time.min, tzinfo=UTC),
                datetime.combine(span[1], time.min, tzinfo=UTC),
            )
        else:
            metrics = await latest_manual_metrics(db, user_id, since)
        point = metrics.get(query.key)
        if point is None:
            # `stale_reply` is set only when the device rows exist and the
            # window excluded them. It outranks "you have no entries", which
            # is the false-absence wording, and is empty on every other path.
            reply = stale_reply or (
                f"You have no {query.key} entries {window}. "
                "Once some are logged I can pull them up here."
            )
        else:
            unit = point.unit or _MANUAL_UNIT.get(query.key, "")
            when = point.at.strftime("%d %b") if point.at else "recently"
            reply = (
                f"Your most recent {query.key} entry is {_g(point.value)} "
                f"{unit}, recorded {when}."
            )

    out: dict = {
        "reply": reply,
        "action": "self_care",
        "provenance": {
            "path": "tracker_query",
            "source": query.source,
            "metric": query.key,
            "period": query.period,
        },
    }
    if visual:
        out["visual"] = visual
    return out


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

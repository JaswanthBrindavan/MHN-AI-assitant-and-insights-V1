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
    DocumentQuery,
    MetricQuery,
    StatedValue,
    SummaryQuery,
    TrackerAdd,
    parse_document_query,
    parse_metric_query,
    parse_stated_value,
    parse_suggestion_query,
    parse_summary_query,
    parse_tracker_add,
)
from app.coredata.service import (
    add_lifestyle_log,
    latest_body_measurement,
    latest_documents,
    latest_vital,
    lifestyle_totals,
    resolve_family_member,
    vital_series,
    window_start,
)
from app.health import ranges as health_ranges
from app.knowledge.registry import load_condition_index
from app.models.chat import McpChunk
from app.models.common import utcnow
from app.models.coredata import Report
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
    from app.models.chat import ConversationMessage
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
        return _value_check_reply(stated)

    # Clarification path: only for a SHORT reply carrying a timing qualifier.
    fasting = bool(_FASTING_RE.search(message))
    postmeal = bool(_POSTMEAL_RE.search(message))
    if not (fasting or postmeal) or len(message.split()) > 5:
        return None
    for prior in await _recent_user_messages(db, session_id):
        sv = parse_stated_value(prior)
        if sv is not None and sv.metric == "blood_sugar":
            return _reclassify_glucose(sv.value, fasting=fasting)
    return None


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
async def handle_document_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    query: DocumentQuery | None = parse_document_query(message)
    if query is None:
        return None

    owner_id, owner_label, include_private = user_id, "you", True
    if query.relation:
        member = await resolve_family_member(db, user_id, query.relation)
        if member is None:
            return {
                "reply": (
                    f"I couldn't find a connected family member matching "
                    f"'{query.relation}' with document sharing enabled. They "
                    "may need to accept the family connection or turn on file "
                    "sharing in the app."
                ),
                "action": "none",
                "provenance": {"path": "document_query", "relation": query.relation,
                               "resolved": False},
            }
        owner_id, owner_label, include_private = member, f"your {query.relation}", False

    hits = await latest_documents(
        db, owner_id, list(query.kinds),
        owner_label=owner_label, include_private=include_private,
    )
    if not hits:
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
        name = h.filepath.rsplit("/", 1)[-1]
        lines.append(f"• {h.kind} — {name} ({when})")
    lead = (
        f"Here {'is' if len(hits) == 1 else 'are'} the most recent "
        f"{'document' if len(hits) == 1 else 'documents'} for {owner_label}:"
    )
    if query.wants_date and hits[0].created_at:
        lead = (
            f"The most recent {hits[0].kind} for {owner_label} is from "
            f"{hits[0].created_at.strftime('%d %b %Y')}."
        )
    return {
        "reply": lead + "\n" + "\n".join(lines),
        "action": "open_documents",
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
    unit = add.unit if add.quantity == 1 else add.unit + (
        "" if add.unit.endswith("s") else "s"
    )
    note = ""
    if add.log_type == "smoking":
        note = (
            " If you're thinking about cutting down, your doctor can share "
            "options that make quitting easier."
        )
    elif add.log_type == "alcohol":
        note = " Tracked as an alcohol entry."
    return {
        "reply": (
            f"Logged: {qty} {unit} of {add.log_type} for {day}. "
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
def _search_content_for_param(content, terms: tuple[str, ...]):
    """Recursively search a report's extracted JSON for a named parameter."""
    if isinstance(content, dict):
        name = str(
            content.get("name") or content.get("parameter") or content.get("test")
            or ""
        ).lower()
        if name and any(t in name for t in terms):
            for value_key in ("value", "result", "reading"):
                raw = content.get(value_key)
                if raw is not None:
                    m = re.search(r"-?\d+(?:\.\d+)?", str(raw))
                    if m:
                        return float(m.group()), content.get("unit")
        for v in content.values():
            found = _search_content_for_param(v, terms)
            if found:
                return found
    elif isinstance(content, list):
        for item in content:
            found = _search_content_for_param(item, terms)
            if found:
                return found
    return None


async def _latest_report_param(
    db: AsyncSession, user_id: uuid.UUID, terms: tuple[str, ...]
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
        found = _search_content_for_param(r.content, terms)
        if found:
            value, unit = found
            return value, unit, r.created_at
    return None


async def handle_metric_query(
    db: AsyncSession, user_id: uuid.UUID, message: str
) -> dict | None:
    from app.chat.abilities import METRIC_REGISTRY

    query: MetricQuery | None = parse_metric_query(message)
    if query is None:
        return None
    spec = METRIC_REGISTRY[query.metric]
    display, unit = spec["display"], spec.get("unit")

    visual = None
    if spec["source"] == "vital":
        point = await latest_vital(db, user_id, spec["vital_type"])
        if point is None:
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
        found = await _latest_report_param(db, user_id, spec["param_terms"])
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
        "provenance": {"path": "metric_query", "metric": query.metric},
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
    if parse_suggestion_query(message) is None:
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

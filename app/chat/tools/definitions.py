"""JSON Schema tool specs for the data abilities. Pure — no DB, no I/O.

Every schema sets ``additionalProperties: false`` and lists its required
fields. That is not pedantry: a loose schema is exactly how an open-weight
model produces arguments the executor cannot use, and a strict one turns a
model mistake into a validation error the loop can recover from.

Descriptions are written FOR THE MODEL. They say when to reach for the tool,
not how it is implemented.
"""

from __future__ import annotations

from app.llm.tools import ToolSpec


def _obj(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


GET_LATEST_METRIC = ToolSpec(
    name="get_latest_metric",
    description=(
        "Get the reader's most recent recorded value for a health metric — "
        "blood pressure, blood sugar, HbA1c, weight, heart rate, SpO2. Use "
        "this whenever they ask what their latest reading was, or when a "
        "question about how they feel would be better answered with their "
        "actual numbers in front of you."
    ),
    input_schema=_obj(
        {
            "metric": {
                "type": "string",
                "description": (
                    "Metric key, e.g. 'blood_pressure', 'blood_sugar', "
                    "'hba1c', 'weight', 'heart_rate', 'spo2'."
                ),
            }
        },
        ["metric"],
    ),
)

GET_REPORT_PARAMETER = ToolSpec(
    name="get_report_parameter",
    description=(
        "Look up any single parameter extracted from the reader's lab reports "
        "— creatinine, basophils, RDW, TSH, anything a report carries. Returns "
        "the value, the report's own abnormal flag, and the date. Only answers "
        "when that test is actually on file; it never estimates."
    ),
    input_schema=_obj(
        {
            "parameter": {
                "type": "string",
                "description": "Test name as the reader said it, e.g. 'creatinine'.",
            }
        },
        ["parameter"],
    ),
)

GET_DOCUMENTS = ToolSpec(
    name="get_documents",
    description=(
        "List the reader's stored documents, or a connected family member's "
        "documents when they name one ('my father's reports'). Family access "
        "already enforces accepted connections, the owner's file-sharing "
        "grant, privacy flags and per-file exclusions — you cannot bypass it "
        "and must not imply the reader can."
    ),
    input_schema=_obj(
        {
            "kinds": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Document kinds, e.g. ['report', 'scan', 'prescription', "
                    "'vaccination']. Omit for all kinds."
                ),
            },
            "relation": {
                "type": "string",
                "description": "A relation, e.g. 'father'. Omit for the reader's own.",
            },
            "owner_name": {
                "type": "string",
                "description": "A connected member's name, when they use one.",
            },
        },
        [],
    ),
)

CHECK_VALUE_AGAINST_RANGE = ToolSpec(
    name="check_value_against_range",
    description=(
        "Compare a reading the reader states right now ('my sugar is 117') "
        "against its reference range. Returns in-range / above / below with "
        "the typical range and how urgently it should be looked at. This "
        "NEVER returns a diagnosis and neither should you."
    ),
    input_schema=_obj(
        {
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "secondary": {
                "type": "number",
                "description": "The diastolic number, for blood pressure only.",
            },
        },
        ["metric", "value"],
    ),
)

LOG_LIFESTYLE_ENTRY = ToolSpec(
    name="log_lifestyle_entry",
    description=(
        "Record a lifestyle entry the reader reports — water, coffee, tea, "
        "alcohol, smoking — optionally backdated. Only call this when they are "
        "telling you something happened, not when they are asking about it. "
        "Always confirm back what was recorded."
    ),
    input_schema=_obj(
        {
            "kind": {
                "type": "string",
                "description": "One of: water, coffee, tea, alcohol, smoking.",
            },
            "quantity": {"type": "number"},
            "days_ago": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30,
                "description": "0 for today, 1 for yesterday.",
            },
        },
        ["kind", "quantity"],
    ),
)

GET_HEALTH_SUMMARY = ToolSpec(
    name="get_health_summary",
    description=(
        "Summarise the reader's recorded data over a week, month or year, "
        "with a chart. Use for 'how have I been doing' style questions."
    ),
    input_schema=_obj(
        {"period": {"type": "string", "enum": ["week", "month", "year"]}},
        ["period"],
    ),
)

GET_FAMILY_MEMBERS = ToolSpec(
    name="get_family_members",
    description=(
        "List the reader's connected family members and what each of them "
        "shares. Use before answering questions about a relative's records, so "
        "you know who is actually connected."
    ),
    input_schema=_obj({}, []),
)

GET_CONDITION_GUIDANCE = ToolSpec(
    name="get_condition_guidance",
    description=(
        "Fetch clinically reviewed guidance for a named condition from the "
        "validated Master Condition Profiles, with citations. Strongly prefer "
        "this over answering a condition question from general knowledge — "
        "this content has been reviewed and general knowledge has not."
    ),
    input_schema=_obj(
        {"condition": {"type": "string", "description": "e.g. 'type 2 diabetes'."}},
        ["condition"],
    ),
)

LOOKUP_MEDICINE = ToolSpec(
    name="lookup_medicine",
    description=(
        "Look up a medicine in the validated medicines database — composition, "
        "what it is generally used for, reported side effects, substitutes. "
        "IMPORTANT: this database holds NO drug-interaction data. If the "
        "reader asks whether two things can be taken together, say plainly "
        "that you cannot check that and send them to a pharmacist."
    ),
    input_schema=_obj(
        {"name": {"type": "string", "description": "Brand or generic name."}},
        ["name"],
    ),
)

ANALYZE_IMAGE = ToolSpec(
    name="analyze_image",
    description=(
        "Look at a photographed document, medicine pack, or visible concern "
        "the reader has stored, and report what is legibly visible. Use it "
        "when a question is about what a picture SHOWS and the extracted text "
        "does not answer it — a photographed report the pipeline could not "
        "read, a medicine pack, a skin or eye concern. It reads only what is "
        "there: it does not identify conditions and it cannot diagnose. Get "
        "the document id from get_documents first."
    ),
    input_schema=_obj(
        {
            "document_id": {"type": "integer"},
            "kind": {
                "type": "string",
                "description": (
                    "Document kind from get_documents, e.g. 'report', 'scan', "
                    "'prescription'."
                ),
            },
            "subject": {
                "type": "string",
                "enum": ["document", "medicine", "skin", "unknown"],
                "description": "What the image is of. Governs how it is read.",
            },
            "question": {
                "type": "string",
                "description": "What the reader wants to know about it.",
            },
        },
        ["document_id", "kind"],
    ),
)


ADD_MEDICATION = ToolSpec(
    name="add_medication",
    description=(
        "Record that the reader has STARTED a medication (they say 'add', "
        "'started', 'now on', 'prescribed'). Writes to their medication list in "
        "the app. Do NOT call this when they only mention a medicine or ask "
        "about one.\n"
        "BEFORE calling, you MUST know how often they take it — how many times a "
        "day, or whether it is as-needed. If they haven't said, ASK; do not "
        "guess a frequency. Then CONFIRM the name, strength and frequency back "
        "to them in one short question ('Add Metformin 500 mg, twice a day — "
        "shall I add it?') and only call this tool after they say yes. If it "
        "could not be saved, say so plainly; never pretend it was added."
    ),
    input_schema=_obj(
        {
            "name": {"type": "string", "description": "Medicine name, e.g. 'metformin'."},
            "strength": {"type": "string", "description": "e.g. '500 mg'. Optional."},
            "times_per_day": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": (
                    "How many times a day, for a scheduled medication (1-4). "
                    "Omit when it is as-needed."
                ),
            },
            "as_needed": {
                "type": "boolean",
                "description": "True for PRN / as-needed (no fixed daily schedule).",
            },
        },
        ["name"],
    ),
)

STOP_MEDICATION = ToolSpec(
    name="stop_medication",
    description=(
        "Mark a medication the reader is on as STOPPED or COMPLETED (they say "
        "'stopped', 'finished', 'completed', 'no longer taking'). It stays in "
        "their history but is no longer active. Confirm back; if there was no "
        "active course by that name, say so."
    ),
    input_schema=_obj(
        {"name": {"type": "string", "description": "Medicine name to stop."}},
        ["name"],
    ),
)

REMOVE_MEDICATION = ToolSpec(
    name="remove_medication",
    description=(
        "Remove a medication from the reader's list entirely (they say 'remove', "
        "'delete', 'take it off my list'). Confirm back; if there was nothing by "
        "that name, say so."
    ),
    input_schema=_obj(
        {"name": {"type": "string", "description": "Medicine name to remove."}},
        ["name"],
    ),
)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    GET_LATEST_METRIC,
    GET_REPORT_PARAMETER,
    GET_DOCUMENTS,
    CHECK_VALUE_AGAINST_RANGE,
    LOG_LIFESTYLE_ENTRY,
    GET_HEALTH_SUMMARY,
    GET_FAMILY_MEMBERS,
    GET_CONDITION_GUIDANCE,
    LOOKUP_MEDICINE,
    ADD_MEDICATION,
    STOP_MEDICATION,
    REMOVE_MEDICATION,
    ANALYZE_IMAGE,
)

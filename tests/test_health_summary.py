"""The composite health summary — every source, and what is missing.

The invariants worth a test are the ones a confident wrong answer would break:
an empty table read as a clinical finding, a deleted row read as current, a
failed query read as "you have none", a wearable number given a grade, and the
two engines disagreeing about the same week.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.chat.data_handlers import handle_summary_query
from app.chat.orchestrator import handle_chat
from app.chat.replies import safe_reply
from app.chat.validation import validate_reply
from app.coredata import service as coredata
from app.llm.fake import FakeProvider
from app.models.common import utcnow
from app.models.coredata import (
    LifestyleLog,
    MedicalCondition,
    MedicineTracking,
    Report,
    SahhaDailyTotal,
    SahhaWeeklyTotal,
    VitalReading,
)
from app.triage.red_flags import EMERGENCY, HIGH, NONE

USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
pytestmark = pytest.mark.asyncio


def _week_open(today: date | None = None) -> date:
    """The Sunday that opens the current rollup week (Sahha's own convention)."""
    today = today or utcnow().date()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _seed_wearable(db, *, metric="sleep_duration", total=2800.0, entries=7):
    start = _week_open()
    db.add(
        SahhaWeeklyTotal(
            user_id=USER, metric=metric, bucket_start=start,
            total=total, entries=entries, days_counted=entries,
        )
    )
    for i in range(entries):
        db.add(
            SahhaDailyTotal(
                user_id=USER, metric=metric, bucket_start=start + timedelta(days=i),
                total=total / entries, entries=1, days_counted=1,
            )
        )
    return start


def _seed_records(db):
    db.add(MedicalCondition(
        id=1, user_id=USER, name="Hypertension", type="condition", status="active",
        started_on=utcnow() - timedelta(days=900),
    ))
    db.add(MedicalCondition(
        id=2, user_id=USER, name="Penicillin", type="allergy", category="medication",
        severity="severe", reaction="rash",
    ))


# --------------------------------------------------------------------------- #
# It says what is missing, and never that the reader HAS none
# --------------------------------------------------------------------------- #
async def test_no_wearable_is_stated_plainly_and_gets_no_empty_chart(db_session):
    db_session.add(LifestyleLog(
        id=1, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        servings=3, logged_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    # WINDOW-SCOPED: an empty week is not an empty record, so it is
    # reported as "nothing logged in the past week", never "not on record".
    assert "wearable readings" in out["provenance"]["nothing_recent"]
    assert "wearable readings" not in out["provenance"]["missing"]
    assert "Nothing logged in the past week for:" in out["reply"]
    assert "Not on record for you:" in out["reply"]
    assert "connected device" not in out["reply"]
    # The chart falls back to what there IS — never a wearable chart with no bars.
    assert out["visual"] is not None and out["visual"]["source"] == "lifestyle"


async def test_absence_is_never_worded_as_a_clinical_finding(db_session):
    db_session.add(LifestyleLog(
        id=1, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        servings=3, logged_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")
    assert out is not None
    reply = out["reply"].lower()

    for forbidden in ("no allergies", "no medications", "no conditions",
                      "you have none", "you are healthy"):
        assert forbidden not in reply


async def test_a_failed_section_is_reported_as_unreadable_not_as_empty(
    db_session, monkeypatch
):
    db_session.add(LifestyleLog(
        id=1, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        servings=3, logged_at=utcnow() - timedelta(days=1),
    ))
    _seed_records(db_session)
    await db_session.flush()

    async def _boom(*_a, **_k):
        raise RuntimeError("medical_condition is on fire")

    monkeypatch.setattr("app.chat.data_handlers.medical_records", _boom)

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    assert "conditions" in out["provenance"]["unavailable"]
    assert "couldn't read these just now" in out["reply"]
    # And the OTHER sections survived it — that is the whole point of the
    # per-section savepoint.
    assert "Lifestyle entries" in out["reply"]


async def test_an_empty_account_still_gets_the_short_honest_answer(db_session):
    out = await handle_summary_query(db_session, USER, "health summary for the month")

    assert out is not None and out["provenance"]["empty"] is True
    assert "don't have any logged data" in out["reply"]


# --------------------------------------------------------------------------- #
# Privacy and soft deletes
# --------------------------------------------------------------------------- #
async def test_a_deleted_condition_is_not_reported_as_current(db_session):
    db_session.add(MedicalCondition(
        id=1, user_id=USER, name="Hypertension", type="condition", status="active",
        started_on=utcnow(), deleted_at=utcnow(),
    ))
    db_session.add(MedicalCondition(
        id=2, user_id=USER, name="Asthma", type="condition", status="active",
        started_on=utcnow(),
    ))
    await db_session.flush()

    rows = await coredata.medical_records(db_session, USER)

    assert [r.name for r in rows] == ["Asthma"]


async def test_a_deleted_medication_is_not_reported_as_current(db_session):
    db_session.add(MedicineTracking(
        id=1, user_id=USER, name="Metformin", strength="500mg",
        private=False, is_prn=False, deleted_at=utcnow(),
    ))
    db_session.add(MedicineTracking(
        id=2, user_id=USER, name="Amlodipine", strength="5mg",
        private=False, is_prn=False,
    ))
    await db_session.flush()

    assert await coredata.active_medications(db_session, USER) == ["Amlodipine 5mg"]


async def test_a_private_row_never_reaches_the_summary(db_session):
    db_session.add(MedicalCondition(
        id=1, user_id=USER, name="Depression", type="condition", status="active",
        started_on=utcnow(), private=True,
    ))
    db_session.add(MedicalCondition(
        id=2, user_id=USER, name="Asthma", type="condition", status="active",
        started_on=utcnow(),
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    assert "Asthma" in out["reply"] and "Depression" not in out["reply"]


async def test_a_deleted_allergy_no_longer_reaches_the_drug_path_either(db_session):
    """The soft-delete filter lives in the shared reader, so every caller gets it."""
    db_session.add(MedicalCondition(
        id=1, user_id=USER, name="Penicillin", type="allergy", category="medication",
        severity="severe", deleted_at=utcnow(),
    ))
    await db_session.flush()

    assert await coredata.medication_allergies(db_session, USER) == []


# --------------------------------------------------------------------------- #
# The wearable half — reported, never graded
# --------------------------------------------------------------------------- #
async def test_the_wearable_section_reports_the_number_and_grades_nothing(db_session):
    _seed_wearable(db_session)          # 2800 min over 7 readings
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    # A week TOTAL says so: printed beside a mean ("resting heart rate
    # averaged 62 bpm") an undifferentiated "sleep 46.7 h" is a wrong number.
    assert "sleep totalled 46.7 h" in out["reply"]   # 2800 minutes, a SUM
    low = out["reply"].lower()
    for graded in ("good sleep", "poor sleep", "your sleep is low",
                   "normal range", "healthy"):
        assert graded not in low
    assert "no reference range" in low


async def test_the_headline_chart_is_the_wearable_series_when_there_is_one(db_session):
    start = _seed_wearable(db_session)
    db_session.add(LifestyleLog(
        id=1, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        servings=3, logged_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")
    assert out is not None
    visual = out["visual"]

    assert visual["source"] == "wearable" and visual["metric"] == "sleep_duration"
    assert visual["grain"] == "day" and visual["window_days"] == 7
    assert len(visual["labels"]) == len(visual["values"]) == 7
    assert visual["unit"] == "h"                       # as DISPLAYED, per §5
    assert visual["title"].endswith(start.strftime("%d %b %Y"))
    assert visual["svg"].startswith("<svg")


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
async def test_the_full_house_summary_passes_the_output_validator(db_session):
    """Hypertension + type 2 diabetes + a penicillin allergy + an out-of-range
    lab is the narrative most likely to be silently replaced by the safe reply:
    ``_DIAGNOSTIC_RE`` blocks "you ... have <condition>" within 40 characters.
    Records framing is what keeps it on the page."""
    _seed_records(db_session)
    _seed_wearable(db_session)
    db_session.add(MedicalCondition(
        id=3, user_id=USER, name="Type 2 diabetes", type="condition",
        status="chronic", started_on=utcnow() - timedelta(days=400),
    ))
    db_session.add(MedicineTracking(
        id=1, user_id=USER, name="Metformin", strength="500mg",
        private=False, is_prn=False,
    ))
    db_session.add(VitalReading(
        id=1, user_id=USER, vital_type="blood_pressure", value_primary=150,
        value_secondary=95, unit="mmHg", recorded_at=utcnow() - timedelta(days=1),
    ))
    db_session.add(LifestyleLog(
        id=1, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        servings=3, logged_at=utcnow() - timedelta(days=1),
    ))
    db_session.add(Report(
        id=1, user_id=USER, filepath="reports/1", private=False,
        created_at=utcnow() - timedelta(days=10),
        content={"ai": {"classification": {"section": "reports", "title": "Lab"},
                        "extraction": {"results": [
                            {"test_name": "HbA1c", "value": "7.2", "unit": "%",
                             "abnormal_flag": "high"}]}}},
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    verdict = validate_reply(out["reply"], NONE)
    assert verdict.ok, verdict.reason
    assert out["reply"] != safe_reply(NONE, None)
    # A 150/95 reading beside a hypertension ROW is still records framing,
    # never "you have hypertension".
    assert "Your records list: Hypertension (active); Type 2 diabetes (chronic)" in (
        out["reply"]
    )
    assert "HbA1c 7.2 (high) %" in out["reply"]
    assert "you have" not in out["reply"].lower()


async def test_a_red_flag_wins_and_no_summary_runs_at_all(db_session):
    _seed_wearable(db_session)
    await db_session.flush()

    for message, expected in (
        ("give me my health summary, also I can't breathe", EMERGENCY),
        ("my health summary please, and my chest hurts", HIGH),
    ):
        result = await handle_chat(db_session, USER, message, FakeProvider())

        assert result.risk_level == expected
        assert result.provenance.get("path") != "health_summary"
        assert "connected device" not in result.response_message


# --------------------------------------------------------------------------- #
# Both engines, same answer
# --------------------------------------------------------------------------- #
async def test_both_engines_produce_the_same_summary_text_and_chart(
    db_session, monkeypatch
):
    """Not 'both plausible' — byte-identical.

    The legacy chain calls the handler; the tool hands the model the SAME
    handler's wording as ``deterministic_reply`` and carries the chart out of
    band. If those two ever diverge, one engine is quoting numbers the other
    does not have.
    """
    from app.chat.tools import executors
    from app.config import get_settings

    _seed_records(db_session)
    _seed_wearable(db_session)
    await db_session.flush()

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_ENGINE", "legacy")
    try:
        legacy = await handle_chat(
            db_session, USER, "health summary for the week", FakeProvider()
        )
    finally:
        get_settings.cache_clear()

    payload = await executors.get_health_summary(
        db_session, USER, {"period": "week"}, None
    )

    assert payload is not None
    assert payload["deterministic_reply"] == legacy.response_message
    assert payload[executors.OUT_OF_BAND_VISUAL] == legacy.visual
    assert "sleep totalled 46.7 h" in legacy.response_message
    assert "Your records list: Hypertension (active)" in legacy.response_message


async def test_the_chart_survives_the_agentic_engine(db_session, monkeypatch):
    from app.config import get_settings
    from app.llm.tools import LLMTurn, ToolCall

    _seed_wearable(db_session)
    await db_session.flush()

    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(
                        id="c1", name="get_health_summary",
                        arguments={"period": "week"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(text="Your sleep totalled 46.7 h in the week on record."),
        ]
    )

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    try:
        result = await handle_chat(
            db_session, USER, "health summary for the week", provider
        )
    finally:
        get_settings.cache_clear()

    assert result.visual is not None
    assert result.visual["metric"] == "sleep_duration"
    assert result.visual["source"] == "wearable"


async def test_the_tool_takes_the_period_as_data_not_as_a_sentence(db_session):
    """The executor must never synthesise English for the free-text parser."""
    from app.chat.tools import executors

    _seed_records(db_session)
    await db_session.flush()

    payload = await executors.get_health_summary(
        db_session, USER, {"period": "month"}, None
    )

    assert payload is not None
    assert payload["provenance"]["period"] == "month"
    assert "past month" in payload["deterministic_reply"]


async def test_a_number_the_model_quotes_from_the_summary_survives_fidelity(
    db_session, monkeypatch
):
    """The agentic fidelity guard replaces a whole reply whose unit-bearing
    values it cannot trace to a tool result. Every figure in the summary is
    computed by the handler and carried in ``deterministic_reply``, so quoting
    one back is traceable — and inventing one is not."""
    from app.config import get_settings
    from app.llm.tools import LLMTurn, ToolCall

    _seed_wearable(db_session, metric="heart_rate_resting", total=434.0)
    await db_session.flush()

    def _provider(text: str) -> FakeProvider:
        return FakeProvider(
            turns=[
                LLMTurn(
                    tool_calls=(
                        ToolCall(id="c1", name="get_health_summary",
                                 arguments={"period": "week"}),
                    ),
                    stop_reason="tool_use",
                ),
                LLMTurn(text=text),
            ]
        )

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    try:
        quoted = await handle_chat(
            db_session, USER, "health summary for the week",
            _provider("Your device recorded a resting heart rate of 62 bpm."),
        )
        invented = await handle_chat(
            db_session, USER, "health summary for the week",
            _provider("Your device recorded a resting heart rate of 99 bpm."),
        )
    finally:
        get_settings.cache_clear()

    assert quoted.provenance.get("degraded") != "fidelity"
    assert "62 bpm" in quoted.response_message
    # The invented figure never reaches the reader: the guard either recovers
    # on a second pass or falls back to the safe reply. Either way the number
    # is gone.
    assert "99 bpm" not in invented.response_message


# --------------------------------------------------------------------------- #
# On the agentic engine the summary's safety wording was a PROMPT INSTRUCTION.
# The tool description asks the model not to grade and not to read an empty
# table as a finding; nothing downstream enforced it, and all three of these
# came back verbatim. `validate_reply` runs behind BOTH engines, so the rule
# lives there and these drive the whole engine to prove it.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("generated", "banned"),
    [
        (
            "You have no allergies on record, so you are clear to take any "
            "medication.",
            "personal-clearance",
        ),
        (
            "Your sleep of 46.7 h this week is good and well within the "
            "normal range.",
            "wearable-grading",
        ),
        (
            "You have no medical conditions and no current medications.",
            "absence-as-finding",
        ),
    ],
)
async def test_the_agentic_engine_cannot_grade_or_clear_after_the_summary(
    db_session, monkeypatch, generated, banned
):
    from app.chat.validation import find_banned
    from app.config import get_settings
    from app.llm.tools import LLMTurn, ToolCall

    _seed_wearable(db_session)
    _seed_records(db_session)
    await db_session.flush()

    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(id="c1", name="get_health_summary",
                             arguments={"period": "week"}),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(text=generated),
        ]
    )

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    try:
        result = await handle_chat(
            db_session, USER, "summarise my health", provider
        )
    finally:
        get_settings.cache_clear()

    # The whole engine, not just the regex: the model composed this after the
    # tool call and something downstream has to stop it.
    assert generated not in result.response_message
    assert find_banned(generated) == banned


@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
@pytest.mark.parametrize(
    "generated",
    [
        "Your steps come from your phone, so the count is only as good as how "
        "often you have it with you.",
        "Your steps are counted by the wrist sensor, which is fine for "
        "walking but under-counts a bike ride.",
    ],
)
async def test_descriptive_prose_about_a_wearable_reaches_the_reader(
    db_session, monkeypatch, engine_name, generated
):
    """The other half of the guard. These are ordinary answers to "how does
    the app know my step count is accurate", and the whole reply was replaced
    by the safe reply on BOTH engines -- a safe-reply fallback for an ordinary
    data question, which is how a guard teaches everyone to route around it."""
    from app.config import get_settings
    from app.llm.tools import LLMTurn

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_ENGINE", engine_name)
    try:
        result = await handle_chat(
            db_session, USER, "why might the step figure from a wrist tracker be off",
            FakeProvider(responses=[generated], turns=[LLMTurn(text=generated)]),
        )
    finally:
        get_settings.cache_clear()

    assert result.provenance.get("degraded") != "validation"
    assert generated in result.response_message


async def test_a_stale_reading_is_nothing_recent_never_not_on_record(db_session):
    """Rule 1 says absence is absence of a RECORD. A 60-day-old blood pressure
    IS a record; only the week's window excludes it, and `_vitals_line`
    returning None for that put "vitals" in the "Not on record for you" list.
    """
    db_session.add(VitalReading(
        id=9, user_id=USER, vital_type="blood_pressure", value_primary=128,
        value_secondary=84, unit="mmHg",
        recorded_at=utcnow() - timedelta(days=60),
    ))
    db_session.add(LifestyleLog(
        id=9, user_id=USER, log_type="coffee", quantity=3, unit="cup",
        logged_at=utcnow() - timedelta(days=1),
    ))
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "summarise my health")

    assert out is not None
    assert "vitals" not in out["provenance"]["missing"]
    assert "Vitals: nothing logged in the past week" in out["reply"]
    assert "your most recent is a blood pressure reading from" in out["reply"]

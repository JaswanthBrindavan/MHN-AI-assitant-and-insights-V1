"""Data abilities, charts, i18n, citations, and agnostic providers."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.charts.svg import bar_chart, chart_payload, line_chart
from app.chat.abilities import (
    parse_document_query,
    parse_metric_query,
    parse_suggestion_query,
    parse_summary_query,
    parse_tracker_add,
)
from app.chat.orchestrator import handle_chat
from app.i18n.language import detect_language, language_directive
from app.llm.fake import FakeProvider
from app.models.common import utcnow
from app.models.coredata import (
    FamilyConnect,
    LifestyleLog,
    Relation,
    Report,
    VitalReading,
)
from app.triage.red_flags import EMERGENCY, triage

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
DAD = uuid.UUID("77777777-7777-7777-7777-777777777777")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_parse_document_query_variants():
    q = parse_document_query("find my latest blood report")
    assert q is not None and q.kinds == ("report",) and q.relation is None

    q = parse_document_query("when was my father's last test done?")
    assert q is not None and q.relation == "father" and q.wants_date

    q = parse_document_query("show my last prescription and x-ray")
    assert q is not None
    assert set(q.kinds) == {"prescription", "scan"}

    assert parse_document_query("what causes diabetes?") is None
    assert parse_document_query("report me to the police") is None  # no "my"


def test_parse_tracker_add_variants():
    t = parse_tracker_add("i had 3 cups of coffee today")
    assert t is not None
    assert (t.log_type, t.quantity, t.unit, t.day_offset) == ("coffee", 3.0, "cup", 0)

    t = parse_tracker_add("I smoked 2 cigs yesterday")
    assert t is not None
    assert (t.log_type, t.quantity, t.unit, t.day_offset) == ("smoking", 2.0, "cigarette", 1)

    t = parse_tracker_add("drank two glasses of water")
    assert t is not None
    assert (t.log_type, t.quantity, t.unit) == ("water", 2.0, "glass")

    t = parse_tracker_add("had a beer last night")
    assert t is not None
    assert (t.log_type, t.quantity, t.day_offset) == ("alcohol", 1.0, 1)

    assert parse_tracker_add("I had a headache today") is None
    assert parse_tracker_add("smoked salmon is tasty") is None  # no quantity
    assert parse_tracker_add("i had 500 cups of coffee") is None  # implausible


def test_parse_metric_query_variants():
    q = parse_metric_query("what's my latest hba1c?")
    assert q is not None and q.metric == "hba1c"

    q = parse_metric_query("show my last blood pressure trend")
    assert q is not None and q.metric == "blood_pressure" and q.wants_trend

    # Educational questions must NOT be hijacked.
    assert parse_metric_query("what is a normal blood pressure range?") is None
    assert parse_metric_query("what should my sugar level be?") is None
    # No possessive/lookup framing → not a metric pull.
    assert parse_metric_query("blood pressure is measured in mmHg") is None


def test_parse_summary_and_suggestions():
    s = parse_summary_query("health summary for the month")
    assert s is not None and s.period == "month"
    s = parse_summary_query("show my weekly health summary")
    assert s is not None and s.period == "week"
    s = parse_summary_query("yearly health summary please")
    assert s is not None and s.period == "year"
    assert parse_summary_query("summarize this article") is None

    assert parse_suggestion_query("any tips for my diabetes?") is not None
    assert parse_suggestion_query("how can I manage my blood pressure?") is not None
    assert parse_suggestion_query("what is diabetes?") is None


# --------------------------------------------------------------------------- #
# Tracker add end-to-end (writes lifestyle_log)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tracker_add_writes_lifestyle_log(db_session):
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "I had 3 cups of coffee today", provider
    )
    assert result.provenance["path"] == "tracker_add"
    assert "Logged: 3 cup" in result.response_message
    rows = (
        await db_session.execute(
            select(LifestyleLog).where(LifestyleLog.user_id == USER)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].log_type == "coffee" and float(rows[0].quantity) == 3.0
    # No LLM was involved.
    assert provider.calls == []


@pytest.mark.asyncio
async def test_tracker_add_yesterday_backdates(db_session):
    await handle_chat(db_session, USER, "I smoked 2 cigs yesterday", FakeProvider())
    row = (
        await db_session.execute(select(LifestyleLog))
    ).scalars().first()
    assert row is not None and row.log_type == "smoking"
    # sqlite returns naive datetimes; normalize before comparing.
    logged = row.logged_at.replace(tzinfo=None)
    assert logged <= (utcnow() - timedelta(hours=20)).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Document query end-to-end (self + family consent)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_document_query_self(db_session):
    db_session.add(
        Report(id=1, user_id=USER, filepath="docs/cbc_report.pdf",
               created_at=utcnow())
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "find my latest blood report", FakeProvider()
    )
    assert result.provenance["path"] == "document_query"
    assert "cbc_report.pdf" in result.response_message


@pytest.mark.asyncio
async def test_document_query_father_with_consent(db_session):
    db_session.add(Relation(id=1, name="Father", inverse="Son"))
    db_session.add(
        FamilyConnect(id=1, requester_id=USER, acceptor_id=DAD, accepted=True,
                      req_file_share=True, acc_file_share=True, relation_id=1)
    )
    db_session.add(
        Report(id=2, user_id=DAD, filepath="docs/dad_lipid.pdf",
               private=False, created_at=utcnow())
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "when was my father's last test done?", FakeProvider()
    )
    assert result.provenance["path"] == "document_query"
    assert "your father" in result.response_message


@pytest.mark.asyncio
async def test_document_query_father_without_consent(db_session):
    db_session.add(Relation(id=1, name="Father", inverse="Son"))
    db_session.add(
        FamilyConnect(id=1, requester_id=USER, acceptor_id=DAD, accepted=True,
                      req_file_share=True, acc_file_share=False, relation_id=1)
    )
    db_session.add(
        Report(id=2, user_id=DAD, filepath="docs/dad_lipid.pdf",
               private=False, created_at=utcnow())
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "find my father's latest report", FakeProvider()
    )
    # Sharing off → no access, and no document leaks.
    assert "dad_lipid" not in result.response_message
    assert result.provenance.get("resolved") is False


@pytest.mark.asyncio
async def test_document_query_private_family_doc_hidden(db_session):
    db_session.add(Relation(id=1, name="Father", inverse="Son"))
    db_session.add(
        FamilyConnect(id=1, requester_id=USER, acceptor_id=DAD, accepted=True,
                      req_file_share=True, acc_file_share=True, relation_id=1)
    )
    db_session.add(
        Report(id=2, user_id=DAD, filepath="docs/dad_secret.pdf",
               private=True, created_at=utcnow())
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "find my father's latest report", FakeProvider()
    )
    assert "dad_secret" not in result.response_message


# --------------------------------------------------------------------------- #
# Metric query end-to-end
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_metric_query_blood_pressure(db_session):
    db_session.add(
        VitalReading(id=1, user_id=USER, vital_type="blood_pressure",
                     value_primary=138, value_secondary=88, unit="mmHg",
                     recorded_at=utcnow())
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "what was my last blood pressure?", FakeProvider()
    )
    assert result.provenance["path"] == "metric_query"
    assert "138/88" in result.response_message
    # Data replies carry the not-medical-advice framing.
    assert "not medical advice" in result.response_message


@pytest.mark.asyncio
async def test_metric_query_hba1c_from_report_content(db_session):
    db_session.add(
        Report(
            id=3, user_id=USER, filepath="docs/labs.pdf", created_at=utcnow(),
            content={"tests": [
                {"name": "Hemoglobin", "value": "13.2", "unit": "g/dL"},
                {"name": "HbA1c (Glycated Hemoglobin)", "value": "6.1", "unit": "%"},
            ]},
        )
    )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "what's my latest hba1c?", FakeProvider()
    )
    assert result.provenance["path"] == "metric_query"
    assert "6.1" in result.response_message


@pytest.mark.asyncio
async def test_metric_query_not_found(db_session):
    result = await handle_chat(
        db_session, USER, "what's my latest hba1c?", FakeProvider()
    )
    assert result.provenance["path"] == "metric_query"
    assert "couldn't find" in result.response_message


# --------------------------------------------------------------------------- #
# Summary + visual
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_health_summary_with_chart(db_session):
    now = utcnow()
    for i, (ltype, qty) in enumerate(
        [("coffee", 2), ("coffee", 3), ("water", 8), ("smoking", 1)]
    ):
        db_session.add(
            LifestyleLog(id=i + 1, user_id=USER, log_type=ltype, quantity=qty,
                         unit="unit", logged_at=now - timedelta(days=1))
        )
    await db_session.flush()
    result = await handle_chat(
        db_session, USER, "health summary for the week", FakeProvider()
    )
    assert result.provenance["path"] == "health_summary"
    assert "coffee" in result.response_message
    assert result.visual is not None
    assert result.visual["type"] == "bar"
    assert result.visual["svg"].startswith("<svg")
    assert result.visual["values"]  # numbers present


@pytest.mark.asyncio
async def test_health_summary_empty(db_session):
    result = await handle_chat(
        db_session, USER, "health summary for the month", FakeProvider()
    )
    assert result.provenance["path"] == "health_summary"
    assert "don't have any logged data" in result.response_message


# --------------------------------------------------------------------------- #
# Charts (pure)
# --------------------------------------------------------------------------- #
def test_line_chart_svg_valid():
    svg = line_chart("BP", ["1 Jan", "2 Jan", "3 Jan"], [120, 130, 125])
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "polyline" in svg and "BP" in svg


def test_bar_chart_flat_and_escape():
    svg = bar_chart("A<b> & test", ["x"], [5])
    assert "A&lt;b&gt; &amp; test" in svg


def test_chart_errors():
    with pytest.raises(ValueError):
        line_chart("t", ["a"], [])
    with pytest.raises(ValueError):
        bar_chart("t", ["a", "b"], [1.0])


def test_chart_payload_shape():
    p = chart_payload("line", "T", ["a", "b"], [1.0, 2.0], unit="mg/dL")
    assert set(p) == {"type", "title", "unit", "labels", "values", "svg"}


# --------------------------------------------------------------------------- #
# i18n
# --------------------------------------------------------------------------- #
def test_detect_language_scripts_and_hinglish():
    assert detect_language("what helps blood pressure") == "en"
    assert detect_language("मुझे सिर दर्द है और बुखार भी") == "hi"
    assert detect_language("mujhe bahut dard hai bukhar bhi hai") == "hi-Latn"
    assert detect_language("எனக்கு தலைவலி இருக்கிறது") == "ta"
    # A stray Devanagari char or two must not flip the language.
    assert detect_language("my BP is ठीक today") == "en"


def test_language_directive():
    assert language_directive("en") == ""
    assert "Hindi" in language_directive("hi")


def test_hindi_triage_phrases_fire():
    assert triage("saans nahi aa rahi hai").level == EMERGENCY
    assert triage("वह बेहोश है").level == EMERGENCY
    # ACS pair in Hinglish
    assert triage("seene mein dard aur paseena aa raha hai").level == EMERGENCY


@pytest.mark.asyncio
async def test_emergency_reply_localized(db_session):
    result = await handle_chat(
        db_session, USER, "saans nahi aa rahi, madad karo", FakeProvider()
    )
    assert result.risk_level == "emergency"
    assert result.language == "hi-Latn"
    assert "emergency" in result.response_message.lower()  # bilingual tail


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_citations_built_from_cited_markers(db_session, set_grounding_mode):
    from pathlib import Path

    from scripts.ingest_knowledge import ingest_folder

    set_grounding_mode("log")
    knowledge_dir = Path(__file__).resolve().parent.parent / "knowledge"  # noqa: ASYNC240
    await ingest_folder(db_session, knowledge_dir, embed=False)
    await db_session.commit()
    provider = FakeProvider(
        responses=[
            "An HbA1c above 48 mmol/mol is worth discussing with a doctor [1]."
        ]
    )
    result = await handle_chat(
        db_session, USER, "tell me about diabetes and blood sugar", provider
    )
    assert result.citations is not None
    assert result.citations[0]["marker"] == "1"
    assert result.citations[0]["condition_code"] == "T2DM"
    # Markers are stripped from display but preserved as citations.
    assert "[1]" not in result.response_message


# --------------------------------------------------------------------------- #
# Agnostic providers (offline construction checks only)
# --------------------------------------------------------------------------- #
def test_provider_selection_by_env(monkeypatch):
    from app.config import get_settings
    from app.llm import get_provider
    from app.llm.providers import AnthropicProvider, OpenAICompatibleProvider

    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b")
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.model_name == "llama-3.3-70b"

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    p = get_provider()
    assert isinstance(p, AnthropicProvider)
    assert p.model_name == "claude-sonnet-5"

    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    from app.llm.fake import FakeProvider as FP

    assert isinstance(get_provider(), FP)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Floor ordering: red flag beats every ability
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tracker_message_with_red_flag_stays_emergency(db_session):
    result = await handle_chat(
        db_session, USER,
        "I had 3 cups of coffee today and now I can't breathe",
        FakeProvider(),
    )
    assert result.risk_level == "emergency"
    # Nothing was logged — the safety floor preempted the tracker write.
    rows = (
        await db_session.execute(select(LifestyleLog))
    ).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Parser coverage extensions (command-verb tracker, lab metrics, verb forms)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "log_type", "qty", "unit", "offset"),
    [
        ("Log 2 cups of coffee", "coffee", 2.0, "cup", 0),
        ("add 3 cups of tea to my tracker", "tea", 3.0, "cup", 0),
        ("record 2 glasses of water", "water", 2.0, "glass", 0),
        ("track one peg of whisky", "alcohol", 1.0, "peg", 0),
        ("I had 5 cigarettes yesterday.", "smoking", 5.0, "cigarette", 1),
        ("log 2 cigs for me", "smoking", 2.0, "cigarette", 0),
        ("add 2 beedis yesterday", "smoking", 2.0, "beedi", 1),
    ],
)
def test_parse_tracker_command_and_had_cigarette_forms(
    message, log_type, qty, unit, offset
):
    add = parse_tracker_add(message)
    assert add is not None
    assert add.log_type == log_type
    assert add.quantity == qty
    assert add.unit == unit
    assert add.day_offset == offset


def test_parse_tracker_had_requires_cigarette_unit():
    # "had N <not-a-tracked-thing>" must not log anything.
    assert parse_tracker_add("I had 2 idlis today") is None
    assert parse_tracker_add("I had 3 meetings yesterday") is None


@pytest.mark.parametrize(
    ("message", "metric"),
    [
        ("What was my last TSH value?", "tsh"),
        ("What is my latest creatinine?", "creatinine"),
        ("What was my last uric acid reading?", "uric_acid"),
        ("What's my latest vitamin D level?", "vitamin_d"),
        ("Show my most recent B12 level.", "vitamin_b12"),
        ("What is my most recent SGPT level?", "sgpt"),
        ("What was my most recent LDL?", "ldl"),
        ("What was my last HDL?", "hdl"),
        ("What are my latest triglycerides?", "triglycerides"),
        ("What's my most recent cholesterol level?", "total_cholesterol"),
        ("What was my last hemoglobin value?", "hemoglobin"),
    ],
)
def test_parse_metric_lab_params(message, metric):
    q = parse_metric_query(message)
    assert q is not None and q.metric == metric


def test_parse_metric_lab_params_guards_hold():
    # Education phrasing must not hijack the data path.
    assert parse_metric_query("What is a normal TSH range?") is None
    assert parse_metric_query("Are eggs good or bad for cholesterol?") is None
    # "glycated hemoglobin" still resolves to HbA1c, not hemoglobin.
    q = parse_metric_query("my latest glycated hemoglobin")
    assert q is not None and q.metric == "hba1c"


def test_parse_summary_summarize_verb():
    q = parse_summary_query("Summarize my health for this month.")
    assert q is not None and q.period == "month"


def test_parse_suggestion_suggest_verb():
    assert parse_suggestion_query("Suggest healthy habits for my PCOS.") is not None

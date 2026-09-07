"""A reader asks about a connected family member and is ANSWERED.

"what is my mother's hba1c?", "any THP in their latest doc?", "what medical
conditions does my mother have?" -- and the paraphrases that mean the same.

What may be read is decided by consent, never by a model, and the four ways
of having nothing to say are four different sentences:

* not connected            -- no accepted Family Connect link matches;
* connected, not sharing   -- the owner-side read grant is off;
* sharing, nothing on file -- and a private or excluded document is simply
                              not among the shared ones: its values never
                              appear and its existence is never asserted;
* sharing, but this KIND is never shared -- vitals, trackers, lifestyle logs.

And never, on either engine, the READER's own figure presented as theirs
(audit H7).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.chat.abilities import (
    names_another_person,
    parse_family_record_query,
)
from app.chat.data_handlers import handle_family_record_query
from app.chat.orchestrator import handle_chat
from app.chat.tools.registry import execute_tool
from app.coredata.service import lookup_family_member, medical_records
from app.llm.fake import FakeProvider
from app.llm.tools import ToolCall
from app.models.chat import ConversationMessage, ConversationSession
from app.models.core import User
from app.models.coredata import (
    FamilyConnect,
    FileAccessExclusion,
    MedicalCondition,
    Relation,
    Report,
    VitalReading,
)

VIEWER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
MOTHER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1")
NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


async def _connect(db, other=MOTHER, *, name="Mother", inverse="Child", shares=True):
    """The viewer sent the request, so the OWNER's grant is ``acc_read``."""
    rel = Relation(name=name, inverse=inverse)
    db.add(rel)
    await db.flush()
    db.add(FamilyConnect(
        requester_id=VIEWER, acceptor_id=other, accepted=True,
        relation_id=rel.id, req_read=True, acc_read=shares,
    ))
    await db.flush()


def _report(owner, id_, title, results, *, private=False, days_ago=0):
    return Report(
        id=id_, user_id=owner, filepath=f"reports/{id_}.pdf", private=private,
        created_at=NOW - timedelta(days=days_ago),
        content={"ai": {
            "classification": {"section": "reports", "title": title},
            "extraction": {"results": results},
        }},
    )


def _hba1c(value, flag="high"):
    return {"test_name": "HbA1c", "value": value, "unit": "%",
            "value_numeric": float(value), "abnormal_flag": flag}


@pytest.fixture
async def mother_sharing(db_session):
    """Connected and sharing, with one shared report on file."""
    db_session.add(User(
        id=MOTHER, name="Lakshmi Rao", email="l@example.com", user_name="lakshmi",
        health_card_number="HC-L", hashcode="x", gender="female",
    ))
    await _connect(db_session)
    db_session.add(_report(MOTHER, 501, "Full body checkup", [
        _hba1c("6.8"),
        {"test_name": "Total Cholesterol", "value": "182", "unit": "mg/dL",
         "value_numeric": 182.0, "abnormal_flag": "normal"},
    ], days_ago=10))
    await db_session.flush()


# --------------------------------------------------------------------------- #
# Paraphrase coverage -- the parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "ask", "relation", "parameter"),
    [
        ("what is my mother's hba1c?", "parameter", "mother", "hba1c"),
        ("mom's sugar levels", "parameter", "mother", "glucose"),
        ("what did dad's last report show for cholesterol", "parameter", "father", "cholesterol"),
        ("what was my father's creatinine in his last report", "parameter", "father", "creatinine"),
        ("what were my father's thyroid levels", "parameter", "father", "thyroid"),
        ("what is my mother's blood pressure?", "parameter", "mother", "blood pressure"),
        ("what medical conditions does my mother have?", "conditions", "mother", None),
        ("does my father have any conditions", "conditions", "father", None),
        ("what illnesses does my dad suffer from", "conditions", "father", None),
        ("my mother's medical history", "conditions", "mother", None),
        ("what is mom diagnosed with", "conditions", "mother", None),
        ("what are my mother's lab results", "parameters", "mother", None),
        ("what's in mom's latest report", "parameters", "mother", None),
        ("what did my mother's last report show", "parameters", "mother", None),
    ],
)
def test_paraphrases_mean_the_same(message, ask, relation, parameter):
    q = parse_family_record_query(message)
    assert q is not None, message
    assert (q.ask, q.relation, q.parameter) == (ask, relation, parameter), message


def test_a_pronoun_carries_the_subject_forward():
    q = parse_family_record_query("her hba1c")
    assert q is not None and q.pronoun == "her" and q.parameter == "hba1c"
    q = parse_family_record_query("any THP in their latest doc?")
    assert q is not None and q.pronoun == "their" and q.ask == "parameters"


def test_a_named_member_is_a_subject_too():
    q = parse_family_record_query("bhargava's cholesterol")
    assert q is not None and q.owner_name == "bhargava" and q.parameter == "cholesterol"


@pytest.mark.parametrize(
    "message",
    [
        "show my mother's reports",                # a listing: the document handler's
        "insights from my father's latest report", # the pipeline's result path
        "my mother's hba1c is 7.2",                # a value stated, not asked
        "my father has diabetes, is my sugar ok",  # history; the READER's question
        "what is my hba1c",                        # the reader's own
        "my mother's doctor",                      # not a lab ask
        "how are you",
    ],
)
def test_what_the_parser_leaves_alone(message):
    assert parse_family_record_query(message) is None, message


def test_a_question_about_a_relative_names_another_person():
    """The H7 gate must see these, or a reader-only tool answers them."""
    assert names_another_person("does my father have any conditions")
    assert names_another_person("mom's sugar levels")
    assert names_another_person("what did dad's last report show for cholesterol")
    # And the family-history carve-out still holds for a statement.
    assert not names_another_person("my father has diabetes, is my sugar ok")


# --------------------------------------------------------------------------- #
# The four answers
# --------------------------------------------------------------------------- #
async def test_not_connected(db_session):
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert r["provenance"]["reason"] == "not_connected"
    assert "Family Connect" in r["reply"] and "your mother" in r["reply"]
    assert "6.8" not in r["reply"]


async def test_connected_but_not_sharing(db_session):
    await _connect(db_session, shares=False)
    db_session.add(_report(MOTHER, 501, "Full body checkup", [_hba1c("6.8")]))
    await db_session.flush()
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert r["provenance"]["reason"] == "not_sharing"
    assert "hasn't turned on sharing" in r["reply"]
    assert "6.8" not in r["reply"]
    # The consent gate itself tells the two apart.
    lookup = await lookup_family_member(db_session, VIEWER, "mother")
    assert lookup.connected and lookup.member_id is None


async def test_sharing_but_nothing_on_file(db_session):
    await _connect(db_session)
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert r["provenance"]["resolved"] is True and r["provenance"]["found"] == 0
    assert "hasn't shared any reports" in r["reply"]


async def test_the_value_with_its_date_and_document(db_session, mother_sharing):
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert r["provenance"]["path"] == "family_record"
    assert r["provenance"]["matched"] == "HbA1c"
    assert r["provenance"]["document"]["id"] == 501
    reply = r["reply"]
    assert reply.startswith("Your mother's most recent HbA1c")
    assert "6.8 %" in reply and "Full body checkup" in reply and "28 Aug 2026" in reply
    assert "flagged high" in reply
    assert r["action"] == "discuss_with_clinician"
    # The card opens the MEMBER's file, not the reader's.
    card = r["documents"][0]
    assert card["id"] == 501 and card["owner"] == "your mother"
    assert card["owner_slug"] == "lakshmi"


async def test_a_paraphrase_finds_the_same_value(db_session, mother_sharing):
    r = await handle_family_record_query(
        db_session, VIEWER, "what did mom's last report show for cholesterol"
    )
    assert r is not None
    assert "182 mg/dL" in r["reply"], r["reply"]
    assert "HbA1c" not in r["reply"]


async def test_a_private_report_is_never_read(db_session, mother_sharing):
    db_session.add(_report(MOTHER, 502, "Private labs", [_hba1c("9.9")], private=True))
    await db_session.flush()
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert "9.9" not in r["reply"] and "6.8" in r["reply"]


async def test_an_excluded_document_is_never_read(db_session, mother_sharing):
    """file_access_exclusions: the newest report is shared in general but
    excluded for THIS viewer, so the older shared one answers."""
    db_session.add(_report(MOTHER, 503, "Newer labs", [_hba1c("8.8")], days_ago=1))
    db_session.add(FileAccessExclusion(user_id=VIEWER, resource_type="reports", resource_id=503))
    await db_session.flush()
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert "8.8" not in r["reply"] and "6.8" in r["reply"]
    assert r["provenance"]["document"]["id"] == 501


async def test_only_an_excluded_document_has_it(db_session):
    """Excluded is indistinguishable from absent -- its existence is never
    asserted, and the reply is the honest 'not in what is shared'."""
    await _connect(db_session)
    db_session.add(_report(MOTHER, 504, "Labs", [_hba1c("8.8")]))
    db_session.add(FileAccessExclusion(user_id=VIEWER, resource_type="reports", resource_id=504))
    await db_session.flush()
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's hba1c?")
    assert r is not None
    assert "8.8" not in r["reply"] and "Labs" not in r["reply"]
    assert r["provenance"]["found"] == 0
    assert "hasn't shared any reports" in r["reply"]


async def test_a_value_not_in_the_shared_reports(db_session, mother_sharing):
    r = await handle_family_record_query(db_session, VIEWER, "what is my mother's tsh?")
    assert r is not None
    assert r["provenance"]["found"] == 0
    assert "couldn't find tsh in the reports your mother has shared" in r["reply"]
    assert "Full body checkup" in r["reply"]      # what IS available


async def test_a_vital_is_a_kind_that_is_never_shared(db_session, mother_sharing):
    """The live failure: "what is my mother's blood pressure?". Blood pressure
    lives in vital_reading, for which there is no consent mechanism -- so the
    answer names that, offers what IS shared, and never reads anyone's vitals."""
    db_session.add(VitalReading(
        user_id=MOTHER, vital_type="blood_pressure", value_primary=150,
        value_secondary=95, unit="mmHg", recorded_at=NOW,
    ))
    db_session.add(VitalReading(
        user_id=VIEWER, vital_type="blood_pressure", value_primary=118,
        value_secondary=76, unit="mmHg", recorded_at=NOW,
    ))
    await db_session.flush()
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Hypertension", type="condition", private=False,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Depression", type="condition", private=True,
    ))
    await db_session.flush()
    r = await handle_family_record_query(
        db_session, VIEWER, "what is my mother's blood pressure?"
    )
    assert r is not None
    assert r["provenance"]["reason"] == "kind_not_shared"
    assert "vital" in r["reply"] and "your mother" in r["reply"]
    assert "150" not in r["reply"] and "118" not in r["reply"]
    # What IS available: the shared report, and the non-private conditions.
    assert "Full body checkup" in r["reply"]
    assert "Hypertension" in r["reply"] and "Depression" not in r["reply"]


# --------------------------------------------------------------------------- #
# Conditions -- `private` IS the family-sharing switch
# --------------------------------------------------------------------------- #
async def test_conditions_honour_the_private_flag(db_session, mother_sharing):
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Type 2 diabetes", type="condition",
        status="active", private=False,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Depression", type="condition",
        status="active", private=True,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Asthma", type="condition", status="active",
        private=False, deleted_at=NOW,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Migraine", type="condition", status="active",
        private=None,                      # predates the column: not a decision to share
    ))
    await db_session.flush()
    r = await handle_family_record_query(
        db_session, VIEWER, "what medical conditions does my mother have?"
    )
    assert r is not None
    assert r["provenance"]["conditions"] == ["Type 2 diabetes"]
    assert "Type 2 diabetes (active)" in r["reply"]
    for hidden in ("Depression", "Asthma", "Migraine"):
        assert hidden not in r["reply"]
    # The owner's own read is untouched by the flag.
    own = await medical_records(db_session, MOTHER, type_="condition")
    assert {c.name for c in own} == {"Type 2 diabetes", "Depression", "Migraine"}


async def test_no_shared_conditions_is_not_no_conditions(db_session, mother_sharing):
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Depression", type="condition", private=True,
    ))
    await db_session.flush()
    r = await handle_family_record_query(db_session, VIEWER, "does my mother have any conditions")
    assert r is not None
    assert "no conditions in the records your mother has shared" in r["reply"]
    assert "not a statement that they have none" in r["reply"]
    assert "Depression" not in r["reply"]


# --------------------------------------------------------------------------- #
# Every value in the latest shared report, and a pronoun follow-up
# --------------------------------------------------------------------------- #
async def _session_with(db, *messages: str) -> uuid.UUID:
    session = ConversationSession(user_id=VIEWER)
    db.add(session)
    await db.flush()
    for i, m in enumerate(messages):
        db.add(ConversationMessage(
            session_id=session.id, role="user", message=m,
            created_at=NOW + timedelta(seconds=i),
        ))
    await db.flush()
    return session.id


async def test_every_thp_in_their_latest_doc(db_session, mother_sharing):
    sid = await _session_with(db_session, "what is my mother's hba1c?",
                              "any THP in their latest doc?")
    r = await handle_family_record_query(
        db_session, VIEWER, "any THP in their latest doc?", sid
    )
    assert r is not None
    assert r["provenance"]["ask"] == "parameters" and r["provenance"]["found"] == 2
    assert "your mother's latest shared report" in r["reply"]
    assert "HbA1c: 6.8 (high) %" in r["reply"]
    assert "Total Cholesterol: 182 mg/dL" in r["reply"]
    assert r["documents"][0]["id"] == 501


async def test_her_hba1c_after_the_mother_was_the_subject(db_session, mother_sharing):
    sid = await _session_with(db_session, "what is my mother's blood pressure?", "her hba1c")
    r = await handle_family_record_query(db_session, VIEWER, "her hba1c", sid)
    assert r is not None and "6.8 %" in r["reply"]


async def test_his_does_not_resolve_to_the_mother(db_session, mother_sharing):
    sid = await _session_with(db_session, "what is my mother's blood pressure?", "his hba1c")
    assert await handle_family_record_query(db_session, VIEWER, "his hba1c", sid) is None


async def test_a_pronoun_with_nobody_named_answers_nothing(db_session, mother_sharing):
    assert await handle_family_record_query(db_session, VIEWER, "her hba1c") is None


# --------------------------------------------------------------------------- #
# End to end, on BOTH engines -- deterministic, no model call
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["legacy", "agentic"])
def chat_engine(request, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", request.param)
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


async def test_the_answer_needs_no_model(db_session, mother_sharing, chat_engine):
    provider = FakeProvider()
    result = await handle_chat(db_session, VIEWER, "what is my mother's hba1c?", provider)
    assert result.provenance["path"] == "family_record", result.provenance
    assert "6.8 %" in result.response_message
    assert provider.calls == []
    assert result.documents and result.documents[0]["id"] == 501
    assert result.recommended_action == "discuss_with_clinician"


async def test_the_live_failure_is_gone(db_session, mother_sharing, chat_engine):
    """Production answered this with a textbook entry on hypertension that
    never mentioned the mother."""
    provider = FakeProvider()
    result = await handle_chat(
        db_session, VIEWER, "what is my mother's blood pressure?", provider
    )
    assert result.provenance["path"] == "family_record"
    assert "your mother" in result.response_message
    assert "hypertension" not in result.response_message.lower()
    assert provider.calls == []


async def test_not_connected_end_to_end(db_session, chat_engine):
    provider = FakeProvider()
    result = await handle_chat(
        db_session, VIEWER, "what medical conditions does my mother have?", provider
    )
    assert result.provenance["path"] == "family_record"
    assert result.provenance["reason"] == "not_connected"
    assert provider.calls == []


async def test_a_pronoun_follow_up_end_to_end(db_session, mother_sharing, chat_engine):
    provider = FakeProvider()
    first = await handle_chat(db_session, VIEWER, "what is my mother's hba1c?", provider)
    second = await handle_chat(
        db_session, VIEWER, "any THP in her latest doc?", provider,
        session_id=first.session_id,
    )
    assert second.provenance["path"] == "family_record"
    assert "Total Cholesterol: 182" in second.response_message


async def test_a_summary_about_a_relative_is_not_the_readers(
    db_session, mother_sharing, chat_engine
):
    provider = FakeProvider()
    result = await handle_chat(
        db_session, VIEWER, "summary of my mother's conditions", provider
    )
    assert result.provenance["path"] != "health_summary"
    assert result.provenance["path"] == "family_record"


# --------------------------------------------------------------------------- #
# The tool path -- what the model calls for a phrasing the parser missed
# --------------------------------------------------------------------------- #
async def _call(db, name, arguments, asked):
    result = await execute_tool(
        db, VIEWER, ToolCall(id="c1", name=name, arguments=arguments), None, asked=asked
    )
    return json.loads(result.content)


async def test_the_tool_answers_from_the_shared_report(db_session, mother_sharing):
    body = await _call(
        db_session, "get_family_member_record",
        {"relation": "mother", "ask": "parameter", "parameter": "hba1c"},
        "could you tell me how my mum's sugar control has been",
    )
    assert "6.8 %" in body["deterministic_reply"], body
    assert body["provenance"]["matched"] == "HbA1c"


async def test_the_tool_lists_conditions_and_the_latest_document(db_session, mother_sharing):
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Type 2 diabetes", type="condition", private=False,
    ))
    await db_session.flush()
    body = await _call(
        db_session, "get_family_member_record",
        {"relation": "mother", "ask": "conditions"}, "is my mum diabetic",
    )
    assert body["provenance"]["conditions"] == ["Type 2 diabetes"]
    body = await _call(
        db_session, "get_family_member_record",
        {"relation": "mother", "ask": "latest_document_values"}, "what's on mum's report",
    )
    assert body["provenance"]["found"] == 2


async def test_the_tool_needs_a_subject(db_session, mother_sharing):
    body = await _call(db_session, "get_family_member_record", {"ask": "conditions"}, "conditions?")
    assert body["found"] is False and "which relative" in body["note"]


async def test_the_tool_declines_a_vital_for_a_relative(db_session, mother_sharing):
    body = await _call(
        db_session, "get_family_member_record",
        {"relation": "mother", "ask": "parameter", "parameter": "blood pressure"},
        "how is my mother's bp doing",
    )
    assert body["provenance"]["reason"] == "kind_not_shared"


async def test_the_reader_only_decline_now_points_here(db_session, mother_sharing):
    body = await _call(
        db_session, "get_report_parameter", {"parameter": "hba1c"},
        "what is my mother's hba1c",
    )
    assert body["found"] is False
    assert "get_family_member_record" in body["note"]

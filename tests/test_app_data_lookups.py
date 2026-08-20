"""Family-connect / doctor-consult abilities, routing precedence, MMR rerank,
and the no-model-name trace guarantee."""

from __future__ import annotations

import uuid

import pytest

from app.chat.abilities import (
    parse_doctor_consult_query,
    parse_family_list_query,
)
from app.chat.data_handlers import (
    handle_doctor_consult_query,
    handle_family_list_query,
)
from app.chat.orchestrator import handle_chat
from app.chat.validation import validate_reply
from app.llm.fake import FakeProvider
from app.models.core import User
from app.models.coredata import (
    Doctor,
    DoctorConnect,
    DoctorSpecialization,
    FamilyConnect,
    Relation,
)
from app.rag.ranking import mmr_rerank

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _user(uid: uuid.UUID, name: str) -> User:
    return User(
        id=uid, name=name, email=f"{name.lower().replace(' ', '')}@example.com",
        user_name=name.split()[0][:20], health_card_number=f"HC-{name[:6]}",
        hashcode="x",
    )
DAD = uuid.UUID("77777777-7777-7777-7777-777777777777")
DOC = uuid.UUID("88888888-8888-8888-8888-888888888888")


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    [
        "Who all are there in my family connect?",
        "who is in my family connect",
        "List my family members",
        "my family connections",
        "Who am I connected with?",
    ],
)
def test_family_list_parses(message):
    assert parse_family_list_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "What does my family history say about heart disease?",
        "my family risk for diabetes",
        "tell me about diabetes",
    ],
)
def test_family_list_does_not_hijack(message):
    assert not parse_family_list_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "Whom did I last consult?",
        "which doctor did I last see",
        "Who is my doctor?",
        "my recent consultations",
        "my doctor connections",
    ],
)
def test_doctor_consult_parses(message):
    assert parse_doctor_consult_query(message)


def test_doctor_consult_does_not_hijack():
    assert not parse_doctor_consult_query("should I consult a doctor for this?")
    assert not parse_doctor_consult_query("what doctor treats diabetes")


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def _seed_family(db):
    db.add(_user(USER, "Asha"))
    db.add(_user(DAD, "Ramesh"))
    db.add(Relation(id=1, name="father", inverse="son"))
    db.add(
        FamilyConnect(
            requester_id=USER, acceptor_id=DAD, accepted=True,
            relation_id=1, req_file_share=True, acc_file_share=True,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_family_list_handler_lists_members(db_session):
    await _seed_family(db_session)
    out = await handle_family_list_query(
        db_session, USER, "who all are in my family connect?"
    )
    assert out is not None
    assert out["provenance"]["path"] == "family_connections"
    assert "Ramesh" in out["reply"]
    assert "your father" in out["reply"]
    assert validate_reply(out["reply"], "none").ok


@pytest.mark.asyncio
async def test_family_list_handler_empty(db_session):
    out = await handle_family_list_query(db_session, USER, "list my family members")
    assert out is not None
    assert "don't have any family connections" in out["reply"]


@pytest.mark.asyncio
async def test_doctor_consult_handler(db_session):
    db_session.add(_user(DOC, "Dr Meera Nair"))
    db_session.add(DoctorSpecialization(id=1, name="Cardiology"))
    db_session.add(Doctor(id=5, user_id=DOC, verified=True, specialization_id=1))
    db_session.add(
        DoctorConnect(
            id=9, user_id=USER, doctor_id=5,
            doctor_acceptance=True, user_acceptance=True,
        )
    )
    await db_session.flush()
    out = await handle_doctor_consult_query(
        db_session, USER, "whom did I last consult?"
    )
    assert out is not None
    assert out["provenance"]["path"] == "doctor_consults"
    assert "Dr Meera Nair" in out["reply"]
    assert "Cardiology" in out["reply"]
    assert validate_reply(out["reply"], "none").ok


@pytest.mark.asyncio
async def test_doctor_consult_handler_empty(db_session):
    out = await handle_doctor_consult_query(db_session, USER, "who is my doctor?")
    assert out is not None
    assert "couldn't find any doctor consultations" in out["reply"]


# --------------------------------------------------------------------------- #
# Routing precedence: a precise metric parse beats the generic data path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_show_me_my_bp_hits_metric_not_insights(db_session):
    result = await handle_chat(
        db_session, USER, "Show me my last BP reading.", FakeProvider()
    )
    assert result.provenance["path"] == "metric_query"


@pytest.mark.asyncio
async def test_generic_data_query_still_served(db_session):
    result = await handle_chat(
        db_session, USER, "show me my insights", FakeProvider()
    )
    assert result.provenance["path"] == "data_query"


# --------------------------------------------------------------------------- #
# Trace never names the model/provider
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_trace_never_names_model(db_session, set_grounding_mode):
    set_grounding_mode("log")
    provider = FakeProvider()
    result = await handle_chat(
        db_session, USER, "what helps with blood pressure?", provider
    )
    blob = " ".join(
        f"{s.get('step','')} {s.get('detail','')}" for s in result.trace
    ).lower()
    assert provider.model_name not in blob
    assert "fake" not in blob  # FakeProvider.model_name


# --------------------------------------------------------------------------- #
# MMR rerank (pure)
# --------------------------------------------------------------------------- #
def test_mmr_prefers_diverse_over_duplicate():
    vectors = {
        "a": [1.0, 0.0],
        "b": [1.0, 0.0],   # duplicate of a
        "c": [0.0, 1.0],   # different information
    }
    order = mmr_rerank(["a", "b", "c"], vectors, k=2)
    assert order == ["a", "c"]


def test_mmr_keeps_relevance_order_without_vectors():
    assert mmr_rerank(["a", "b", "c"], {}, k=2) == ["a", "b"]


def test_mmr_k_bounds():
    assert mmr_rerank([], {}, k=3) == []
    assert mmr_rerank(["a"], {}, k=0) == []
    assert mmr_rerank(["a"], {}, k=5) == ["a"]

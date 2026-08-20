"""Chat upload triggering (mhn-ai run submission) and chat-history endpoints.

Davi handles no document bytes or rows — files reach S3 + unclassified_files
via Spring's upload flow. Covers app/documents/service.py and the
/chat/upload, /chat/sessions, /chat/sessions/{id}/messages endpoints against
the VERIFIED mhn-ai contract (POST /v1/document-processing-runs, bearer
MHN_SERVICE_TOKEN, the submitted unit being an ``unclassified_files`` id):
run submission (accepted / rejected / unreachable / disabled), ownership
checks, job_runs bookkeeping, conversation persistence of upload turns,
history retrieval, and object-level authorization.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.chat.validation import validate_reply
from app.config import get_settings
from app.documents.service import (
    build_upload_reply,
    get_own_unclassified,
    submit_document,
    trigger_mhn_ai,
)
from app.models.common import utcnow
from app.models.coredata import UnclassifiedFile
from app.models.jobs import JobRun

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("99999999-9999-9999-9999-999999999999")
HDR = {"X-User-Id": str(USER)}
OTHER_HDR = {"X-User-Id": str(OTHER)}

RUN_ID = "3d1a2c66-1f7e-4b58-9a54-0d6a4be2b111"
ITEM_ID = "5e8b9d10-2c3f-4a67-8b12-9c0d1e2f3a45"


def _row(
    user_id: uuid.UUID = USER,
    created_by: uuid.UUID | None = None,
    name: str | None = "cbc_report.pdf",
) -> UnclassifiedFile:
    """An unclassified_files row as Spring's upload flow would create it."""
    return UnclassifiedFile(
        user_id=user_id,
        filepath=f"uploads/{uuid.uuid4().hex}.pdf",
        name=name,
        private=False,
        created_by=created_by or user_id,
        created_at=utcnow(),
    )


def _accepted_body(document_id: int) -> dict:
    return {
        "run_id": RUN_ID,
        "created_at": "2026-08-20T12:00:00Z",
        "items": [
            {
                "document_id": document_id,
                "item_id": ITEM_ID,
                "status": "queued",
                "outcome": "created",
                "error_code": None,
            }
        ],
    }


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# get_own_unclassified — read-only ownership check
# --------------------------------------------------------------------------- #
async def test_get_own_unclassified_scoping(db_session):
    row = _row()
    db_session.add(row)
    await db_session.flush()
    assert await get_own_unclassified(db_session, USER, row.id) is not None
    assert await get_own_unclassified(db_session, OTHER, row.id) is None
    assert await get_own_unclassified(db_session, USER, 999999) is None


async def test_get_own_unclassified_uploader_can_submit(db_session):
    # Family-connect: OTHER uploaded a document ABOUT USER — the uploader
    # may also submit it for processing.
    row = _row(user_id=USER, created_by=OTHER)
    db_session.add(row)
    await db_session.flush()
    assert await get_own_unclassified(db_session, OTHER, row.id) is not None


# --------------------------------------------------------------------------- #
# trigger_mhn_ai — the verified run-submission contract
# --------------------------------------------------------------------------- #
async def test_trigger_disabled_without_base_url():
    assert get_settings().mhn_ai_base_url == ""
    result = await trigger_mhn_ai(1, USER)
    assert result.accepted is False


async def test_trigger_submits_run_with_contract_payload(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    monkeypatch.setenv("MHN_AI_TOKEN", "x" * 32)
    get_settings.cache_clear()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["request_id"] = request.headers.get("x-request-id")
        seen["json"] = json.loads(request.read())
        return httpx.Response(202, json=_accepted_body(42))

    async with _mock_client(handler) as client:
        result = await trigger_mhn_ai(42, USER, client=client)

    assert result.accepted is True
    assert result.run_id == RUN_ID
    assert result.item_status == "queued"
    assert seen["url"] == (
        "http://mhn-ai.internal:8000/v1/document-processing-runs"
    )
    assert seen["auth"] == "Bearer " + "x" * 32
    assert seen["request_id"]
    assert seen["json"] == {
        "documents": [{"document_id": 42, "intended_section": None}],
        "requested_by_user_id": str(USER),
    }


async def test_trigger_rejected_status_not_accepted(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    async with _mock_client(lambda r: httpx.Response(401)) as client:
        result = await trigger_mhn_ai(1, USER, client=client)
    assert result.accepted is False


async def test_trigger_connection_error_never_raises(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with _mock_client(boom) as client:
        result = await trigger_mhn_ai(1, USER, client=client)
    assert result.accepted is False


async def test_trigger_unparseable_202_still_accepted(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    async with _mock_client(
        lambda r: httpx.Response(202, content=b"not json")
    ) as client:
        result = await trigger_mhn_ai(1, USER, client=client)
    assert result.accepted is True
    assert result.run_id is None


# --------------------------------------------------------------------------- #
# submit_document — job bookkeeping
# --------------------------------------------------------------------------- #
async def test_submit_document_success_job(db_session, monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    async with _mock_client(
        lambda r: httpx.Response(202, json=_accepted_body(7))
    ) as client:
        result = await submit_document(db_session, USER, 7, client=client)
    assert result.accepted is True
    assert result.run_id == RUN_ID
    job = (await db_session.execute(select(JobRun))).scalars().one()
    assert job.name == "chat_upload_trigger"
    assert job.trigger == "chat"
    assert job.status == "success"


async def test_submit_document_failed_trigger_job(db_session):
    result = await submit_document(db_session, USER, 7)
    assert result.accepted is False
    job = (await db_session.execute(select(JobRun))).scalars().one()
    assert job.status == "trigger_failed"


# --------------------------------------------------------------------------- #
# Reply copy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("triggered", [True, False])
def test_upload_reply_validator_safe(triggered: bool):
    reply = build_upload_reply("cbc_report.pdf", triggered)
    assert "cbc_report.pdf" in reply
    assert validate_reply(reply, "none").ok


# --------------------------------------------------------------------------- #
# POST /chat/upload endpoint
# --------------------------------------------------------------------------- #
async def _seed_row(sessionmaker, **kw) -> int:
    async with sessionmaker() as db:
        row = _row(**kw)
        db.add(row)
        await db.commit()
        return row.id


@pytest.mark.asyncio
async def test_upload_endpoint_triggers_and_replies(client, sessionmaker):
    doc_id = await _seed_row(sessionmaker)
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        json={"document_id": doc_id, "message": "please store this report"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["resource_type"] == "unclassified_files"
    assert body["document"]["doc_id"] == doc_id
    assert body["document"]["state"] == "pending"
    assert body["document"]["triggered"] is False  # no mhn-ai URL in tests
    assert "cbc_report.pdf" in body["response_message"]
    assert body["session_id"]


@pytest.mark.asyncio
async def test_upload_endpoint_writes_no_document_rows(client, sessionmaker):
    doc_id = await _seed_row(sessionmaker)
    await client.post(
        "/api/v1/chat/upload", headers=HDR, json={"document_id": doc_id}
    )
    async with sessionmaker() as db:
        rows = (
            await db.execute(select(UnclassifiedFile))
        ).scalars().all()
    # Only the seeded row exists — Davi never creates document rows.
    assert [r.id for r in rows] == [doc_id]


@pytest.mark.asyncio
async def test_upload_endpoint_records_conversation(client, sessionmaker):
    doc_id = await _seed_row(sessionmaker)
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        json={"document_id": doc_id, "message": "store it"},
    )
    sid = resp.json()["session_id"]
    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    assert hist.status_code == 200
    msgs = hist.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["message"] == "[uploaded file: cbc_report.pdf] store it"
    assert msgs[1]["role"] == "assistant"
    assert "cbc_report.pdf" in msgs[1]["message"]


@pytest.mark.asyncio
async def test_upload_endpoint_threads_existing_session(client, sessionmaker):
    doc_id = await _seed_row(sessionmaker)
    first = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "hello"}
    )
    sid = first.json()["session_id"]
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        json={"document_id": doc_id, "session_id": sid},
    )
    assert resp.json()["session_id"] == sid
    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    roles = [m["role"] for m in hist.json()]
    assert roles == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_upload_endpoint_unknown_document_404(client):
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, json={"document_id": 999999}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_endpoint_other_users_document_404(client, sessionmaker):
    doc_id = await _seed_row(sessionmaker, user_id=OTHER, created_by=OTHER)
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, json={"document_id": doc_id}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_endpoint_filename_falls_back_to_filepath(
    client, sessionmaker
):
    doc_id = await _seed_row(sessionmaker, name=None)
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, json={"document_id": doc_id}
    )
    # No stored name → the S3 key's basename is used in the reply.
    assert ".pdf" in resp.json()["response_message"]


# --------------------------------------------------------------------------- #
# History endpoints
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sessions_listing_with_preview_and_counts(client):
    r1 = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "what helps sleep?"}
    )
    sid = r1.json()["session_id"]
    await client.post(
        "/api/v1/chat",
        headers=HDR,
        json={"message": "and what about caffeine?", "session_id": sid},
    )
    listing = await client.get("/api/v1/chat/sessions", headers=HDR)
    assert listing.status_code == 200
    sessions = listing.json()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == sid
    assert s["message_count"] == 4  # 2 user + 2 assistant turns
    assert s["preview"] == "what helps sleep?"
    assert s["last_message_at"] is not None


@pytest.mark.asyncio
async def test_sessions_listing_is_scoped_to_user(client):
    await client.post("/api/v1/chat", headers=HDR, json={"message": "hi"})
    listing = await client.get("/api/v1/chat/sessions", headers=OTHER_HDR)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_messages_unknown_session_404(client):
    resp = await client.get(
        f"/api/v1/chat/sessions/{uuid.uuid4()}/messages", headers=HDR
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_messages_other_users_session_403(client):
    r = await client.post("/api/v1/chat", headers=HDR, json={"message": "hi"})
    sid = r.json()["session_id"]
    resp = await client.get(
        f"/api/v1/chat/sessions/{sid}/messages", headers=OTHER_HDR
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_history_preserved_across_requests(client):
    """Context preservation: a later request with the same session sees the
    full stored history via the messages endpoint."""
    r = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "what is anemia?"}
    )
    sid = r.json()["session_id"]
    await client.post(
        "/api/v1/chat",
        headers=HDR,
        json={"message": "is it serious?", "session_id": sid},
    )
    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    texts = [m["message"] for m in hist.json()]
    assert "what is anemia?" in texts
    assert "is it serious?" in texts
    assert len(texts) == 4


# --------------------------------------------------------------------------- #
# Plural document phrasings (found live: "pull my latest lab reports" fell
# through to the LLM, which then wrongly claimed it had no record access)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("pull my latest lab reports", "report"),
        ("show me my blood reports", "report"),
        ("find my recent scans", "scan"),
        ("do I have any prescriptions in my records?", "prescription"),
        ("show my vaccinations", "vaccination"),
        ("my latest x-rays please", "scan"),
    ],
)
def test_parse_document_query_plural_kinds(message, kind):
    from app.chat.abilities import parse_document_query

    q = parse_document_query(message)
    assert q is not None and kind in q.kinds


# --------------------------------------------------------------------------- #
# Restored conversations keep their document cards (meta round-trip) and the
# document parser tolerates spelling mistakes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_history_meta_carries_document_cards(client, sessionmaker):
    from app.models.common import utcnow as _now
    from app.models.coredata import Report

    async with sessionmaker() as db:
        db.add(Report(
            user_id=USER, filepath="reports/abc123uuid.pdf",
            content={"ai": {"classification": {"title": "CBC Report"}}},
            private=False, created_at=_now(),
        ))
        await db.commit()

    r = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "pull my latest lab reports"}
    )
    body = r.json()
    assert body["provenance"]["path"] == "document_query"
    sid = body["session_id"]

    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    msgs = hist.json()
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    assert assistant["meta"] is not None
    docs = assistant["meta"]["documents"]
    assert docs and docs[0]["title"] == "CBC Report"
    assert docs[0]["slug"] == "abc123uuid.pdf"
    assert assistant["meta"]["action"] == "open_documents"
    # User turns never expose their (triage-internal) intent metadata.
    user_turn = [m for m in msgs if m["role"] == "user"][-1]
    assert user_turn["meta"] is None


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("pull my latest lab reprots", "report"),
        ("show my prescriptons", "prescription"),
        ("find my vacination record", "vaccination"),
        ("show me my latest blod report", "report"),
        ("my recent secans please", "scan"),
    ],
)
def test_parse_document_query_fuzzy_spelling(message, kind):
    from app.chat.abilities import parse_document_query_fuzzy

    q = parse_document_query_fuzzy(message)
    assert q is not None and kind in q.kinds


def test_parse_document_query_fuzzy_does_not_invent():
    from app.chat.abilities import parse_document_query_fuzzy

    # Ordinary sentences with no document intent stay unparsed.
    assert parse_document_query_fuzzy("I love mountain resorts") is None
    assert parse_document_query_fuzzy("what helps blood pressure?") is None


# --------------------------------------------------------------------------- #
# "Get insights for this report" — served from mhn-ai's ai-result endpoint
# --------------------------------------------------------------------------- #
from app.chat.data_handlers import handle_ai_result_query  # noqa: E402
from app.documents.service import AiResultFetch, fetch_ai_result  # noqa: E402


async def test_fetch_ai_result_two_call_contract(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    monkeypatch.setenv("MHN_AI_TOKEN", "x" * 32)
    get_settings.cache_clear()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer " + "x" * 32
        if request.url.path == "/v1/documents/7/status":
            return httpx.Response(200, json={
                "document_id": 7, "status": "completed",
                "document_type": "reports", "section_row_id": 12,
            })
        return httpx.Response(200, json={
            "document_id": 7, "status": "completed",
            "classification": {"title": "CBC Report"},
            "insights": {"summary": "All good.", "insights": []},
        })

    async with _mock_client(handler) as client:
        fetch = await fetch_ai_result(7, client=client)
    assert calls == ["/v1/documents/7/status",
                     "/v1/documents/reports/7/ai-result"]
    assert fetch.ok and fetch.document_type == "reports"
    assert fetch.result is not None
    assert fetch.result["insights"]["summary"] == "All good."


async def test_fetch_ai_result_unclassified_yet(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    async with _mock_client(lambda r: httpx.Response(200, json={
        "document_id": 7, "status": "classifying", "document_type": None,
    })) as client:
        fetch = await fetch_ai_result(7, client=client)
    assert fetch.ok and fetch.result is None and fetch.status == "classifying"


async def test_fetch_ai_result_not_configured():
    assert (await fetch_ai_result(7)).reason == "not_configured"


async def _seed_upload(db, name="LR_report.pdf"):
    row = _row(name=name)
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_ai_result_handler_no_upload(db_session):
    out = await handle_ai_result_query(
        db_session, USER, "get insights for this report"
    )
    assert out is not None
    assert "couldn't find an uploaded document" in out["reply"]


@pytest.mark.asyncio
async def test_ai_result_handler_unreachable(db_session, monkeypatch):
    await _seed_upload(db_session)

    async def _down(document_id, client=None):
        return AiResultFetch(ok=False, reason="ConnectError: down")

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _down)
    out = await handle_ai_result_query(
        db_session, USER, "get insights for this report"
    )
    assert out is not None
    assert "couldn't reach the document-processing service" in out["reply"]
    assert out["provenance"]["error"] == "ConnectError: down"


@pytest.mark.asyncio
async def test_ai_result_handler_still_processing(db_session, monkeypatch):
    await _seed_upload(db_session)

    async def _pending(document_id, client=None):
        return AiResultFetch(ok=True, status="classifying")

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _pending)
    out = await handle_ai_result_query(db_session, USER, "analyze this report")
    assert out is not None
    assert "still being processed" in out["reply"]
    assert "classifying" in out["reply"]


@pytest.mark.asyncio
async def test_ai_result_handler_renders_insights(db_session, monkeypatch):
    await _seed_upload(db_session)

    async def _done(document_id, client=None):
        return AiResultFetch(ok=True, status="completed",
            document_type="reports", result={
                "classification": {"title": "Lipid Profile"},
                "insights": {
                    "summary": "Cholesterol values are borderline.",
                    "insights": [{
                        "heading": "LDL slightly elevated",
                        "explanation": "LDL carries cholesterol to tissues.",
                        "risk_patterns": "Above the printed limit.",
                        "suggestion_heading": "Diet adjustments",
                        "suggestions": "More fibre, fewer fried foods.",
                        "related_tests": ["LDL"],
                    }],
                    "disclaimer": "Informational only, not medical advice.",
                },
            })

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _done)
    out = await handle_ai_result_query(
        db_session, USER, "get insights for this report"
    )
    assert out is not None
    assert out["provenance"]["path"] == "ai_result"
    assert out["provenance"]["source"] == "mhn_ai"
    assert "Cholesterol values are borderline." in out["reply"]
    assert "LDL slightly elevated" in out["reply"]
    assert "Diet adjustments" in out["reply"]
    assert "Informational only" in out["reply"]
    assert out["action"] == "discuss_with_clinician"


@pytest.mark.asyncio
async def test_ai_result_handler_renders_section_extraction(
    db_session, monkeypatch
):
    await _seed_upload(db_session, name="vaccine_card.pdf")

    async def _done(document_id, client=None):
        return AiResultFetch(ok=True, status="completed",
            document_type="vaccinations", result={
                "classification": {"title": "Vaccination Card"},
                "section_extraction": {
                    "section": "vaccinations",
                    "fields": {"vaccine_name": "Tetanus", "dose_number": 2},
                    "flags": ["dates_out_of_order"],
                },
            })

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _done)
    out = await handle_ai_result_query(db_session, USER, "analyze my document")
    assert out is not None
    assert "vaccine name: Tetanus" in out["reply"]
    assert "dates out of order" in out["reply"]

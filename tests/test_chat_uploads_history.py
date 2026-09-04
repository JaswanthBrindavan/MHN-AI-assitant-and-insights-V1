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
def test_upload_reply_validator_safe():
    reply = build_upload_reply("cbc_report.pdf")
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


@pytest.mark.asyncio
async def test_ai_result_handler_follows_filed_document(db_session, monkeypatch):
    """After mhn-ai files a document the unclassified row is DELETED — the
    handler must recover the pipeline id from the filed row's content.ai
    envelope (assembly writes the original document_id there)."""
    from app.models.coredata import Report

    db_session.add(Report(
        user_id=USER, filepath="reports/relocated-uuid.pdf",
        content={"ai": {
            "schema_version": "2.1", "state": "complete", "document_id": 42,
            "classification": {"title": "Lab Results"},
        }},
        private=False, created_at=utcnow(),
    ))
    await db_session.flush()
    seen: dict = {}

    async def _done(document_id, client=None):
        seen["id"] = document_id
        return AiResultFetch(ok=True, status="completed",
            document_type="reports", result={
                "classification": {"title": "Lab Results"},
                "extraction": {"results": [
                    {"test_name": "Hemoglobin", "value": "10.2",
                     "unit": "g/dL", "abnormal_flag": "low"},
                ]},
            })

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _done)
    out = await handle_ai_result_query(
        db_session, USER, "get insights for this report"
    )
    assert out is not None
    assert seen["id"] == 42  # the ORIGINAL pipeline id, not the reports row id
    assert "Hemoglobin: 10.2 g/dL — low" in out["reply"]
    assert out["action"] == "discuss_with_clinician"


def test_upload_reply_never_alarms():
    # Spring submits every upload itself — a failed redundant trigger must
    # not read as "classification is not running" (the reply no longer even
    # takes the trigger outcome).
    reply = build_upload_reply("x.pdf")
    assert "could not be started" not in reply
    assert "queued for automatic processing" in reply


# --------------------------------------------------------------------------- #
# Generic document words + pending uploads visible in listings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message",
    ["fetch my documents", "show me my docs", "pull my files",
     "show me my records"],
)
def test_parse_document_query_generic_words(message):
    from app.chat.abilities import ALL_DOCUMENT_KINDS, parse_document_query

    q = parse_document_query(message)
    assert q is not None and tuple(q.kinds) == ALL_DOCUMENT_KINDS


@pytest.mark.asyncio
async def test_document_listing_includes_pending_upload(db_session):
    from app.chat.data_handlers import handle_document_query

    db_session.add(_row(name="LR_W_fresh_upload.pdf"))
    await db_session.flush()
    out = await handle_document_query(db_session, USER, "show me my documents")
    assert out is not None
    assert out["provenance"]["path"] == "document_query"
    assert "LR_W_fresh_upload.pdf (still being processed)" in out["reply"]
    pending_cards = [c for c in out["documents"] if c.get("pending")]
    assert pending_cards and pending_cards[0]["resource_type"] == "unclassified"


@pytest.mark.asyncio
async def test_document_listing_pending_plus_filed(db_session):
    from app.chat.data_handlers import handle_document_query
    from app.models.coredata import Report

    db_session.add(Report(
        user_id=USER, filepath="reports/filed-uuid.pdf",
        content={"ai": {"classification": {"title": "CBC Report"}}},
        private=False, created_at=utcnow(),
    ))
    db_session.add(_row(name="new_upload.pdf"))
    await db_session.flush()
    out = await handle_document_query(db_session, USER, "pull my files")
    assert out is not None
    assert "CBC Report" in out["reply"]
    assert "new_upload.pdf (still being processed)" in out["reply"]


# --------------------------------------------------------------------------- #
# Scheme-less MHN_AI_BASE_URL + envelope fallback when unreachable
# --------------------------------------------------------------------------- #
async def test_trigger_normalizes_schemeless_base_url(monkeypatch):
    # Found live: the env var was set without http:// — httpx rejected every
    # call with UnsupportedProtocol. A missing scheme now defaults to http.
    monkeypatch.setenv("MHN_AI_BASE_URL", "mhn-ai.railway.internal:8000")
    get_settings.cache_clear()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(202, json=_accepted_body(1))

    async with _mock_client(handler) as client:
        result = await trigger_mhn_ai(1, USER, client=client)
    assert result.accepted is True
    assert seen["url"].startswith("http://mhn-ai.railway.internal:8000/")




@pytest.mark.asyncio
async def test_ai_result_this_means_the_previous_turns_document(
    db_session, monkeypatch
):
    """"Give insights for this" right after a document listing must target the
    LISTED document — not whatever was uploaded or filed most recently."""
    from app.chat.conversation import add_message, ensure_session
    from app.models.coredata import Report, ScanImaging

    referenced = Report(
        user_id=USER, filepath="reports/blood.pdf",
        content={"ai": {"document_id": 42,
                        "classification": {"title": "Laboratory Test Results"}}},
        private=False, created_at=utcnow(),
    )
    # A NEWER unrelated document that the old resolution would have chosen.
    newer = ScanImaging(
        user_id=USER, filepath="scans_imaging/dexa.pdf",
        content={"ai": {"document_id": 99,
                        "classification": {"title": "DEXA Scan Report"}}},
        private=False, created_at=utcnow(),
    )
    db_session.add_all([referenced, newer])
    await db_session.flush()

    sid = await ensure_session(db_session, USER, None)
    await add_message(db_session, sid, "user", "show me my latest blood report")
    await add_message(
        db_session, sid, "assistant", "Here is the most recent document…",
        extracted_intent={"documents": [{
            "kind": "report", "resource_type": "reports", "id": referenced.id,
            "title": "Laboratory Test Results", "owner": "you",
        }]},
    )
    seen: dict = {}

    async def _done(document_id, client=None):
        seen["id"] = document_id
        return AiResultFetch(ok=True, status="completed",
            document_type="reports", result={
                "classification": {"title": "Laboratory Test Results"},
                "insights": {"summary": "Values look stable.", "insights": []},
            })

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _done)
    out = await handle_ai_result_query(
        db_session, USER, "give insights for this", session_id=sid
    )
    assert out is not None
    assert seen["id"] == 42  # the LISTED report, not the newer DEXA (99)
    assert "Values look stable." in out["reply"]


@pytest.mark.parametrize(
    ("message", "nkinds"),
    [
        ("list all my documents", 6),
        ("list my reports", 1),
        ("view my scans", 1),
        ("display my prescriptions", 1),
        ("all my documents", 6),
        ("what documents do I have", 6),
        ("do I have any vaccination records", 1),
    ],
)
def test_parse_document_query_list_view_phrasings(message, nkinds):
    from app.chat.abilities import parse_document_query

    q = parse_document_query(message)
    assert q is not None and len(q.kinds) == nkinds


def test_parse_document_query_everyday_lists_stay_unparsed():
    from app.chat.abilities import parse_document_query_fuzzy

    assert parse_document_query_fuzzy("I have a shopping list") is None
    assert parse_document_query_fuzzy("my wish list for diwali") is None
# --------------------------------------------------------------------------- #
# Metric matching against real-world extraction names
# --------------------------------------------------------------------------- #
def _lab_report_content():
    """Extraction shaped like the live report that exposed these bugs —
    note "Hemoglobin A1c" sits BEFORE "Hemoglobin", and HDL/LDL rows exist."""
    return {"ai": {"extraction": {"results": [
        {"test_name": "Glucose - Fasting", "value": "96",
         "value_numeric": 96.0, "unit": "mg/dL"},
        {"test_name": "Hemoglobin A1c", "value": "5.8",
         "value_numeric": 5.8, "unit": "%"},
        {"test_name": "Cholesterol - HDL", "value": "44",
         "value_numeric": 44.0, "unit": "mg/dL"},
        {"test_name": "Cholesterol - Total", "value": "182",
         "value_numeric": 182.0, "unit": "mg/dL"},
        {"test_name": "Hemoglobin", "value": "14.1",
         "value_numeric": 14.1, "unit": "g/dL"},
    ]}}}


async def _seed_lab_report(db):
    from app.models.coredata import Report

    db.add(Report(user_id=USER, filepath="reports/lab.pdf",
                  content=_lab_report_content(), private=False,
                  created_at=utcnow()))
    await db.flush()


@pytest.mark.asyncio
async def test_hba1c_matches_hemoglobin_a1c_name(db_session):
    from app.chat.data_handlers import handle_metric_query

    await _seed_lab_report(db_session)
    out = await handle_metric_query(db_session, USER, "what is my latest hba1c")
    assert out is not None and "5.8 %" in out["reply"]


@pytest.mark.asyncio
async def test_hemoglobin_skips_the_a1c_row(db_session):
    from app.chat.data_handlers import handle_metric_query

    await _seed_lab_report(db_session)
    out = await handle_metric_query(
        db_session, USER, "what was my last hemoglobin value?"
    )
    assert out is not None and "14.1 g/dL" in out["reply"]


@pytest.mark.asyncio
async def test_total_cholesterol_skips_hdl_row(db_session):
    from app.chat.data_handlers import handle_metric_query

    await _seed_lab_report(db_session)
    out = await handle_metric_query(
        db_session, USER, "what's my most recent cholesterol level?"
    )
    assert out is not None and "182 mg/dL" in out["reply"]


@pytest.mark.asyncio
async def test_blood_sugar_falls_back_to_report_glucose(db_session):
    from app.chat.data_handlers import handle_metric_query

    await _seed_lab_report(db_session)  # no logged vitals at all
    out = await handle_metric_query(
        db_session, USER, "show me my latest fasting blood sugar"
    )
    assert out is not None and "96 mg/dL" in out["reply"]
    assert out["provenance"]["source"] == "report"


# --------------------------------------------------------------------------- #
# Dynamic report-parameter asks + section-detail asks
# --------------------------------------------------------------------------- #
def _cbc_report_content():
    return {"ai": {"extraction": {"results": [
        {"test_name": "Basophils - Absolute Count", "value": "0.03",
         "value_numeric": 0.03, "unit": "10^3/uL", "abnormal_flag": ""},
        {"test_name": "RDW", "value": "16.1", "value_numeric": 16.1,
         "unit": "%", "abnormal_flag": "high"},
    ]}}}


@pytest.mark.asyncio
async def test_dynamic_param_finds_basophils(db_session):
    from app.chat.data_handlers import handle_report_param_ask
    from app.models.coredata import Report

    db_session.add(Report(user_id=USER, filepath="reports/cbc.pdf",
                          content=_cbc_report_content(), private=False,
                          created_at=utcnow()))
    await db_session.flush()
    out = await handle_report_param_ask(db_session, USER, "what is my basophils")
    assert out is not None
    assert "Basophils - Absolute Count" in out["reply"]
    assert "0.03" in out["reply"]
    assert out["provenance"]["path"] == "report_param"


@pytest.mark.asyncio
async def test_dynamic_param_flags_abnormal(db_session):
    from app.chat.data_handlers import handle_report_param_ask
    from app.models.coredata import Report

    db_session.add(Report(user_id=USER, filepath="reports/cbc.pdf",
                          content=_cbc_report_content(), private=False,
                          created_at=utcnow()))
    await db_session.flush()
    out = await handle_report_param_ask(db_session, USER, "show me my rdw")
    assert out is not None and "flagged high" in out["reply"]
    assert out["action"] == "discuss_with_clinician"


@pytest.mark.asyncio
async def test_dynamic_param_silent_when_absent(db_session):
    from app.chat.data_handlers import handle_report_param_ask

    out = await handle_report_param_ask(db_session, USER, "what is my basophils")
    assert out is None  # falls through — never invents a value


@pytest.mark.asyncio
async def test_section_detail_insurance_fields(db_session):
    from app.chat.data_handlers import handle_section_detail_query
    from app.models.coredata import Insurance

    db_session.add(Insurance(
        user_id=USER, filepath="insurance/policy.pdf",
        content={"ai": {
            "classification": {"title": "Star Health Policy"},
            "section_extraction": {
                "section": "insurance",
                "fields": {"policy_number": "SH-991", "provider": "Star Health",
                           "valid_till": "2027-03-31"},
                "flags": [],
            },
        }},
        private=False, created_at=utcnow(),
    ))
    await db_session.flush()
    out = await handle_section_detail_query(
        db_session, USER, "what is my policy number"
    )
    assert out is not None
    assert "policy number: SH-991" in out["reply"]
    assert "Star Health Policy" in out["reply"]
    assert out["provenance"]["path"] == "section_detail"
    # "pull my latest insurance" wants the details AND a way to open the
    # file — get_section_details used to return the contents with nothing
    # for the client to open, while only get_documents produced cards.
    assert out["documents"] and len(out["documents"]) == 1
    card = out["documents"][0]
    assert card["kind"] == "insurance"
    assert card["resource_type"] == "insurance"
    assert card["slug"] == "policy.pdf"
    assert card["title"] == "Star Health Policy"
    assert card["owner"] == "you"
    assert card["id"] is not None


@pytest.mark.asyncio
async def test_section_detail_pending_document(db_session):
    from app.chat.data_handlers import handle_section_detail_query
    from app.models.coredata import Bill

    db_session.add(Bill(
        user_id=USER, filepath="bills/inv.pdf",
        content={"ai": {"state": "classifying"}},
        private=False, created_at=utcnow(),
    ))
    await db_session.flush()
    out = await handle_section_detail_query(
        db_session, USER, "how much was my last bill"
    )
    assert out is not None
    assert "doesn't have extracted details yet" in out["reply"]
    assert "classifying" in out["reply"]
    # The document exists even though extraction has not finished — still
    # worth a card so the reader can open the raw file themselves.
    assert out["documents"] and out["documents"][0]["kind"] == "bill"


@pytest.mark.asyncio
async def test_plain_show_my_insurance_lists_documents(db_session):
    """No detail word → the document LISTING answers, not the field view."""
    from app.chat.data_handlers import handle_section_detail_query

    out = await handle_section_detail_query(
        db_session, USER, "show my insurance"
    )
    assert out is None


@pytest.mark.asyncio
async def test_dynamic_param_bare_latest_phrasing(db_session):
    from app.chat.data_handlers import handle_report_param_ask
    from app.models.coredata import Report

    db_session.add(Report(user_id=USER, filepath="reports/cbc.pdf",
        content={"ai": {"extraction": {"results": [
            {"test_name": "RBC COUNT", "value": "5.26", "value_numeric": 5.26,
             "unit": "Millions/cumm", "abnormal_flag": "normal"},
        ]}}}, private=False, created_at=utcnow()))
    await db_session.flush()
    out = await handle_report_param_ask(db_session, USER, "latest rbc count")
    assert out is not None
    assert "5.26" in out["reply"]
    # "normal" is a value mhn-ai writes explicitly — never worded as a
    # deviation, and never escalated.
    assert "flagged" not in out["reply"]
    assert "within the printed reference range" in out["reply"]
    assert out["action"] == "review_with_clinician"


@pytest.mark.asyncio
async def test_ai_result_normal_flags_not_counted_abnormal(
    db_session, monkeypatch
):
    await _seed_upload(db_session)

    async def _done(document_id, client=None):
        return AiResultFetch(ok=True, status="completed",
            document_type="reports", result={
                "classification": {"title": "CBC"},
                "extraction": {"results": [
                    {"test_name": "RBC", "value": "5.2",
                     "abnormal_flag": "normal"},
                    {"test_name": "RDW", "value": "16.1",
                     "abnormal_flag": "high"},
                ]},
            })

    monkeypatch.setattr("app.documents.service.fetch_ai_result", _done)
    out = await handle_ai_result_query(db_session, USER, "analyze this report")
    assert out is not None
    assert "1 value is" in out["reply"]  # only RDW counts, not RBC "normal"
    assert "RBC: 5.2\n" in out["reply"] or "RBC: 5.2" in out["reply"]


# --------------------------------------------------------------------------- #
# Name-check (mhn-ai V10 / name-verification feature)
# --------------------------------------------------------------------------- #
async def test_fetch_ai_result_carries_name_check(monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/documents/7/status"
        return httpx.Response(200, json={
            "document_id": 7, "status": "failed",
            "document_type": "reports",
            "last_error_code": "name_mismatch",
            "name_check": {"verdict": "mismatch",
                           "document_name": "Ramesh Kumar",
                           "confirmed": False},
        })

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        fetch = await fetch_ai_result(7, client=client)
    # A mismatch never asks the typed result route (it would 409).
    assert fetch.ok and fetch.result is None
    assert fetch.error_code == "name_mismatch"
    assert fetch.name_check is not None
    assert fetch.name_check["document_name"] == "Ramesh Kumar"
    get_settings.cache_clear()


async def test_ai_result_name_mismatch_explains_not_retry(
    db_session, monkeypatch
):
    async def _mismatch(document_id, client=None):
        return AiResultFetch(
            ok=True, status="failed", document_type="reports",
            error_code="name_mismatch",
            name_check={"verdict": "mismatch",
                        "document_name": "Ramesh Kumar", "confirmed": False},
        )

    monkeypatch.setattr(
        "app.documents.service.fetch_ai_result", _mismatch
    )
    uid = uuid.uuid4()
    db_session.add(_row(user_id=uid, name="cbc.pdf"))
    await db_session.flush()

    r = await handle_ai_result_query(db_session, uid, "get insights for this report")
    assert r is not None
    assert "doesn't match this account" in r["reply"]
    assert "Ramesh Kumar" in r["reply"]
    assert "retried" not in r["reply"]  # retry is the WRONG guidance here
    assert r["provenance"]["name_check"] == "mismatch"

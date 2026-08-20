"""Chat file uploads (store → trigger mhn-ai) and chat-history endpoints.

Covers app/documents/service.py and the /chat/upload, /chat/sessions,
/chat/sessions/{id}/messages endpoints against the VERIFIED mhn-ai contract
(POST /v1/document-processing-runs, bearer MHN_SERVICE_TOKEN, the submitted
unit being an ``unclassified_files`` id): storage, run submission
(accepted / rejected / unreachable / disabled), job_runs bookkeeping,
conversation persistence of upload turns, history retrieval, and
object-level authorization.
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
    store_and_trigger,
    store_document,
    trigger_mhn_ai,
)
from app.models.coredata import UnclassifiedFile
from app.models.jobs import JobRun

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("99999999-9999-9999-9999-999999999999")
HDR = {"X-User-Id": str(USER)}
OTHER_HDR = {"X-User-Id": str(OTHER)}

RUN_ID = "3d1a2c66-1f7e-4b58-9a54-0d6a4be2b111"
ITEM_ID = "5e8b9d10-2c3f-4a67-8b12-9c0d1e2f3a45"


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


@pytest.fixture(autouse=True)
def _upload_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# store_document — unclassified_files row
# --------------------------------------------------------------------------- #
async def test_store_document_row(db_session):
    row = await store_document(
        db_session, USER, "var/uploads/abc.pdf", name="cbc_report.pdf"
    )
    assert row.id is not None
    assert row.user_id == USER
    assert row.created_by == USER
    assert row.filepath == "var/uploads/abc.pdf"
    assert row.name == "cbc_report.pdf"
    assert row.private is False


async def test_get_own_unclassified_scoping(db_session):
    row = await store_document(db_session, USER, "k.pdf", name="k.pdf")
    assert await get_own_unclassified(db_session, USER, row.id) is not None
    assert await get_own_unclassified(db_session, OTHER, row.id) is None
    assert await get_own_unclassified(db_session, USER, 999999) is None


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
# store_and_trigger — job bookkeeping
# --------------------------------------------------------------------------- #
async def test_store_and_trigger_success_job(db_session, monkeypatch):
    monkeypatch.setenv("MHN_AI_BASE_URL", "http://mhn-ai.internal:8000")
    get_settings.cache_clear()

    async with _mock_client(
        lambda r: httpx.Response(202, json=_accepted_body(1))
    ) as client:
        stored = await store_and_trigger(
            db_session, USER, b"bytes", "var/uploads/k.pdf",
            name="k.pdf", client=client,
        )
    assert stored.triggered is True
    assert stored.resource_type == "unclassified_files"
    assert stored.run_id == RUN_ID
    assert stored.item_status == "queued"
    job = (await db_session.execute(select(JobRun))).scalars().one()
    assert job.name == "chat_upload_trigger"
    assert job.trigger == "chat"
    assert job.status == "success"
    assert job.input_hash is not None and len(job.input_hash) == 64


async def test_store_and_trigger_untriggered_job(db_session):
    stored = await store_and_trigger(
        db_session, USER, b"bytes", "var/uploads/k.pdf", name="k.pdf"
    )
    assert stored.triggered is False
    job = (await db_session.execute(select(JobRun))).scalars().one()
    assert job.status == "stored_not_triggered"
    # The document is stored regardless.
    doc = (await db_session.execute(select(UnclassifiedFile))).scalars().one()
    assert doc.user_id == USER


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
def _file(name: str = "cbc_report.pdf", data: bytes = b"%PDF-1.4 test"):
    return {"file": (name, data, "application/pdf")}


@pytest.mark.asyncio
async def test_upload_endpoint_stores_and_replies(client):
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        files=_file(),
        data={"message": "please store this report"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["resource_type"] == "unclassified_files"
    assert body["document"]["state"] == "pending"
    assert body["document"]["triggered"] is False  # no mhn-ai URL in tests
    assert "Stored your file 'cbc_report.pdf'" in body["response_message"]
    assert body["session_id"]


@pytest.mark.asyncio
async def test_upload_endpoint_records_conversation(client):
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        files=_file(),
        data={"message": "store it"},
    )
    sid = resp.json()["session_id"]
    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    assert hist.status_code == 200
    msgs = hist.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["message"] == "[uploaded file: cbc_report.pdf] store it"
    assert msgs[1]["role"] == "assistant"
    assert "Stored your file" in msgs[1]["message"]


@pytest.mark.asyncio
async def test_upload_endpoint_threads_existing_session(client):
    first = await client.post(
        "/api/v1/chat", headers=HDR, json={"message": "hello"}
    )
    sid = first.json()["session_id"]
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        files=_file(),
        data={"session_id": sid},
    )
    assert resp.json()["session_id"] == sid
    hist = await client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=HDR)
    roles = [m["role"] for m in hist.json()]
    assert roles == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_upload_endpoint_document_id_mode(client, sessionmaker):
    async with sessionmaker() as db:
        row = await store_document(db, USER, "s3/key/abc.pdf", name="abc.pdf")
        await db.commit()
        doc_id = row.id
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        data={"document_id": str(doc_id), "message": "process this"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"]["doc_id"] == doc_id
    assert body["document"]["triggered"] is False  # no mhn-ai URL in tests
    assert "abc.pdf" in body["response_message"]


@pytest.mark.asyncio
async def test_upload_endpoint_document_id_of_other_user_404(
    client, sessionmaker
):
    async with sessionmaker() as db:
        row = await store_document(db, OTHER, "s3/key/x.pdf", name="x.pdf")
        await db.commit()
        doc_id = row.id
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, data={"document_id": str(doc_id)}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_endpoint_requires_exactly_one_mode(client):
    neither = await client.post(
        "/api/v1/chat/upload", headers=HDR, data={"message": "hi"}
    )
    assert neither.status_code == 400
    both = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        files=_file(),
        data={"document_id": "1"},
    )
    assert both.status_code == 400


@pytest.mark.asyncio
async def test_upload_endpoint_rejects_empty_file(client):
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, files=_file(data=b"")
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_endpoint_rejects_oversize(client, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    get_settings.cache_clear()
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, files=_file(data=b"x" * 11)
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_endpoint_rejects_bad_session_id(client):
    resp = await client.post(
        "/api/v1/chat/upload",
        headers=HDR,
        files=_file(),
        data={"session_id": "not-a-uuid"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_endpoint_saves_bytes_to_upload_dir(client, tmp_path):
    resp = await client.post(
        "/api/v1/chat/upload", headers=HDR, files=_file(data=b"CONTENT")
    )
    assert resp.status_code == 200
    upload_dir = tmp_path / "uploads"
    saved = list(upload_dir.iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"CONTENT"
    assert saved[0].suffix == ".pdf"


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

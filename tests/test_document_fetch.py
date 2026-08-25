"""Fetching document bytes — the narrow, audited exception to "no S3".

The property that matters most here is the one that is easiest to lose: a
document the reader is not entitled to must never be read, and the refusal must
happen BEFORE anything leaves the process. Spring re-checks too, but Davi
checking first means a bug in either alone cannot widen access.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.documents.fetch import (
    ALLOWED_CONTENT_TYPES,
    MAX_BYTES,
    fetch_document_bytes,
)
from app.models.jobs import JobRun


class _Resp:
    def __init__(self, status=200, body=b"", headers=None, payload=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    """Stub httpx client: first GET returns the presigned URL, second the bytes."""

    def __init__(self, url_resp=None, bytes_resp=None):
        self.url_resp = url_resp
        self.bytes_resp = bytes_resp
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if len(self.calls) == 1:
            return self.url_resp
        return self.bytes_resp


def _ok_client(body=b"\xff\xd8\xffimagedata", content_type="image/jpeg"):
    return _Client(
        url_resp=_Resp(payload={"url": "https://s3.example/signed"}),
        bytes_resp=_Resp(body=body, headers={"content-type": content_type}),
    )


@pytest.fixture(autouse=True)
def _spring_configured(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MHN_SPRING_BASE_URL", "http://spring.internal:8080")
    monkeypatch.setenv("MHN_SPRING_TOKEN", "s" * 40)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _jobs(db_session) -> list[JobRun]:
    return list(
        (
            await db_session.execute(
                select(JobRun).where(JobRun.name == "document_fetch")
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# Consent — the part that must be airtight
# --------------------------------------------------------------------------- #
async def test_the_owner_can_read_their_own_document(db_session):
    user_id = uuid.uuid4()
    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is not None
    assert doc.content_type == "image/jpeg"


async def test_an_unconnected_stranger_is_refused_before_any_network_call(
    db_session,
):
    """The refusal must happen BEFORE anything leaves the process."""
    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=uuid.uuid4(), owner_id=uuid.uuid4(),
        kind="report", resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    assert client.calls == [], "a network call was made for a refused document"


async def test_a_private_document_is_refused_even_for_a_connection(db_session):
    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=uuid.uuid4(), owner_id=uuid.uuid4(),
        kind="report", resource_id=1, is_private=True, client=client,
    )
    assert doc is None
    assert client.calls == []


async def test_a_refusal_is_audited(db_session):
    await fetch_document_bytes(
        db_session, viewer_id=uuid.uuid4(), owner_id=uuid.uuid4(),
        kind="report", resource_id=7, is_private=False, client=_ok_client(),
    )
    jobs = await _jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status == "refused"
    assert jobs[0].error == "consent"
    # The resource is recorded; the bytes and the content never are.
    assert "reports:7" in (jobs[0].input_hash or "")


# --------------------------------------------------------------------------- #
# Davi never mints a URL
# --------------------------------------------------------------------------- #
async def test_spring_is_asked_for_the_url_and_told_who_is_asking(db_session):
    user_id = uuid.uuid4()
    client = _ok_client()
    await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=42, is_private=False, client=client,
    )
    assert client.calls[0].endswith("/files/reports/42/url")
    # And then the SIGNED url, which Spring produced.
    assert client.calls[1] == "https://s3.example/signed"


async def test_no_url_from_spring_means_no_read(db_session):
    user_id = uuid.uuid4()
    client = _Client(url_resp=_Resp(status=403, payload=None), bytes_resp=None)
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert jobs[0].error == "no_url"


async def test_a_non_http_url_is_rejected(db_session):
    """A malformed or relative URL must not be followed."""
    user_id = uuid.uuid4()
    client = _Client(
        url_resp=_Resp(payload={"url": "file:///etc/passwd"}), bytes_resp=None
    )
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None


async def test_nothing_happens_when_spring_is_not_configured(
    db_session, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setenv("MHN_SPRING_BASE_URL", "")
    get_settings.cache_clear()

    user_id = uuid.uuid4()
    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    assert client.calls == []
    jobs = await _jobs(db_session)
    assert jobs[0].error == "not_configured"


# --------------------------------------------------------------------------- #
# Bounded reads
# --------------------------------------------------------------------------- #
async def test_a_disallowed_content_type_is_refused(db_session):
    """A PDF is deliberately not allowed — mhn-ai already extracts those, and
    re-reading the raw file duplicates its job with a worse tool."""
    user_id = uuid.uuid4()
    client = _ok_client(content_type="application/pdf")
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert "application/pdf" in (jobs[0].error or "")


async def test_an_oversized_document_is_refused(db_session):
    user_id = uuid.uuid4()
    client = _ok_client(body=b"x" * (MAX_BYTES + 1))
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert jobs[0].error == "too_large"


async def test_an_empty_body_is_a_failure_not_a_document(db_session):
    user_id = uuid.uuid4()
    client = _ok_client(body=b"")
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None


async def test_every_allowed_type_is_an_image():
    """If a document type ever needs adding, it should be a deliberate act."""
    assert all(t.startswith("image/") for t in ALLOWED_CONTENT_TYPES)


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
async def test_a_transport_failure_returns_none_rather_than_raising(db_session):
    class _Explodes:
        calls: list[str] = []

        async def get(self, url, **kwargs):
            raise RuntimeError("network down")

    user_id = uuid.uuid4()
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=_Explodes(),
    )
    assert doc is None


async def test_a_successful_read_is_audited(db_session):
    user_id = uuid.uuid4()
    await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=5, is_private=False, client=_ok_client(),
    )
    jobs = await _jobs(db_session)
    assert jobs[0].status == "success"
    assert jobs[0].error is None


async def test_the_bytes_are_never_written_anywhere(db_session):
    """They live for one turn, in memory. Nothing persists them."""
    user_id = uuid.uuid4()
    secret = b"\xff\xd8\xffSECRETIMAGEBYTES"
    await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=_ok_client(body=secret),
    )
    jobs = await _jobs(db_session)
    blob = " ".join(
        f"{j.status} {j.error or ''} {j.input_hash or ''}" for j in jobs
    )
    assert "SECRET" not in blob

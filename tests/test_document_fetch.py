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
    """A JSON response, for the Spring presigned-URL call."""

    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _StreamResp:
    """A streamed response, for the byte fetch.

    Yields the body in CHUNKS so the incremental size cap is genuinely
    exercised — handing over a pre-built body would reproduce the bug the cap
    exists to prevent rather than catch it.
    """

    def __init__(self, status=200, body=b"", headers=None, chunk=4096, raises=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._chunk = chunk
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        if self._raises is not None:
            raise self._raises
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i : i + self._chunk]


class _Client:
    """Stub client: GET returns the presigned URL, stream() returns the bytes."""

    def __init__(self, url_resp=None, bytes_resp=None):
        self.url_resp = url_resp
        self.bytes_resp = bytes_resp
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        return self.url_resp

    def stream(self, method, url, **kwargs):
        self.calls.append(url)
        if self.bytes_resp is None:
            raise RuntimeError("no byte response scripted")
        return self.bytes_resp


IMAGE_BYTES = bytes([0xFF, 0xD8, 0xFF]) + b"imagedata"


def _ok_client(body=IMAGE_BYTES, content_type="image/jpeg", declared_length=None):
    headers = {"content-type": content_type}
    if declared_length is not None:
        headers["content-length"] = str(declared_length)
    return _Client(
        url_resp=_Resp(payload={"url": "https://s3.example/signed"}),
        bytes_resp=_StreamResp(body=body, headers=headers),
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


async def test_a_non_http_url_is_never_followed(db_session):
    """A malformed, relative or non-http URL must not be FETCHED.

    Asserting only `doc is None` was not enough: with the guard removed the
    stub raises on the next call, the bare except catches it, and the function
    returns None anyway — the test passed against a broken implementation.
    Counting the calls is what actually pins the behaviour.
    """
    user_id = uuid.uuid4()
    for hostile in ("file:///etc/passwd", "httpfoo://evil", "/relative", "ftp://x"):
        client = _Client(
            url_resp=_Resp(payload={"url": hostile}), bytes_resp=None
        )
        doc = await fetch_document_bytes(
            db_session, viewer_id=user_id, owner_id=user_id, kind="report",
            resource_id=1, is_private=False, client=client,
        )
        assert doc is None, hostile
        assert len(client.calls) == 1, f"followed {hostile}"


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
async def test_a_failure_getting_the_url_returns_none(db_session):
    class _ExplodesImmediately:
        calls: list[str] = []

        async def get(self, url, **kwargs):
            raise RuntimeError("network down")

        def stream(self, method, url, **kwargs):  # pragma: no cover - unreached
            raise RuntimeError("network down")

    user_id = uuid.uuid4()
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=_ExplodesImmediately(),
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert jobs[0].error == "no_url"


async def test_a_failure_fetching_the_BYTES_returns_none(db_session):
    """The previous test raises on the FIRST call, so it exercises the URL
    handler. This one gets a URL successfully and then fails — the byte-fetch
    handler, which had no coverage at all."""

    class _ExplodesOnBytes:
        def __init__(self):
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            return _Resp(payload={"url": "https://s3.example/signed"})

        def stream(self, method, url, **kwargs):
            self.calls.append(url)
            # Valid headers, so it gets PAST the type and length checks and
            # fails where this test intends: mid-body.
            return _StreamResp(
                headers={"content-type": "image/jpeg"},
                raises=RuntimeError("connection reset mid-download"),
            )

    user_id = uuid.uuid4()
    client = _ExplodesOnBytes()
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    assert len(client.calls) == 2
    jobs = await _jobs(db_session)
    assert jobs[0].error == "transport"


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
    secret = bytes([0xFF, 0xD8, 0xFF]) + b"SECRETIMAGEBYTES"
    await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=_ok_client(body=secret),
    )
    jobs = await _jobs(db_session)
    blob = " ".join(
        f"{j.status} {j.error or ''} {j.input_hash or ''}" for j in jobs
    )
    assert "SECRET" not in blob


# --------------------------------------------------------------------------- #
# The FAMILY branch of the consent gate
# --------------------------------------------------------------------------- #
# Found in review: every test above uses viewer == owner, which short-circuits
# at the first line of can_view_document. The family branch — the four-condition
# gate that is the whole reason this module is careful — had ZERO coverage
# across the entire suite. These exercise it.


async def _connect(db_session, requester, acceptor, *, accepted=True,
                   req_read=None, acc_read=None):
    from app.models.coredata import FamilyConnect

    row = FamilyConnect(
        requester_id=requester,
        acceptor_id=acceptor,
        accepted=accepted,
        req_read=req_read,
        acc_read=acc_read,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_a_connected_member_can_read_when_the_owner_granted(db_session):
    """Owner ACCEPTED the connection, so the grant sits on acc_read."""
    owner, viewer = uuid.uuid4(), uuid.uuid4()
    await _connect(db_session, requester=viewer, acceptor=owner, acc_read=True)

    doc = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=1, is_private=False, client=_ok_client(),
    )
    assert doc is not None


async def test_the_grant_on_the_wrong_side_does_not_count(db_session):
    """req_read is the REQUESTER's grant. Here the owner accepted, so only
    acc_read is theirs — a true req_read is the viewer's own sharing setting
    and must not unlock the owner's files."""
    owner, viewer = uuid.uuid4(), uuid.uuid4()
    await _connect(
        db_session, requester=viewer, acceptor=owner, req_read=True, acc_read=False
    )

    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    assert client.calls == []


async def test_an_unaccepted_connection_grants_nothing(db_session):
    owner, viewer = uuid.uuid4(), uuid.uuid4()
    await _connect(
        db_session, requester=viewer, acceptor=owner, accepted=False, acc_read=True
    )

    doc = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=1, is_private=False, client=_ok_client(),
    )
    assert doc is None


async def test_a_private_document_is_refused_despite_a_valid_grant(db_session):
    owner, viewer = uuid.uuid4(), uuid.uuid4()
    await _connect(db_session, requester=viewer, acceptor=owner, acc_read=True)

    doc = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=1, is_private=True, client=_ok_client(),
    )
    assert doc is None


async def test_a_per_file_exclusion_overrides_the_grant(db_session):
    """The fourth condition: the owner shares generally but hid THIS file."""
    from app.models.coredata import FileAccessExclusion

    owner, viewer = uuid.uuid4(), uuid.uuid4()
    await _connect(db_session, requester=viewer, acceptor=owner, acc_read=True)
    db_session.add(
        FileAccessExclusion(
            user_id=viewer, resource_type="reports", resource_id=42
        )
    )
    await db_session.flush()

    client = _ok_client()
    doc = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=42, is_private=False, client=client,
    )
    assert doc is None
    assert client.calls == []

    # A different file under the same grant is still readable.
    other = await fetch_document_bytes(
        db_session, viewer_id=viewer, owner_id=owner, kind="report",
        resource_id=43, is_private=False, client=_ok_client(),
    )
    assert other is not None


async def test_an_oversized_content_length_is_refused_before_reading(db_session):
    """The cheap check: if the header already says it is too big, do not read
    a byte of it."""
    user_id = uuid.uuid4()
    client = _ok_client(body=b"small", declared_length=MAX_BYTES + 1)
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert jobs[0].error == "too_large"


async def test_a_lying_content_length_does_not_get_a_free_pass(db_session):
    """A header claiming 10 bytes while the body streams megabytes must still
    be caught — the incremental check is the real guard, and the reason the
    body is streamed rather than buffered."""
    user_id = uuid.uuid4()
    client = _ok_client(body=b"x" * (MAX_BYTES + 1), declared_length=10)
    doc = await fetch_document_bytes(
        db_session, viewer_id=user_id, owner_id=user_id, kind="report",
        resource_id=1, is_private=False, client=client,
    )
    assert doc is None
    jobs = await _jobs(db_session)
    assert jobs[0].error == "too_large"

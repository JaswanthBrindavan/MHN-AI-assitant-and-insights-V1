"""Request-id middleware end to end (audit R8/R10).

The unit-level check for the formatter and the contextvar plumbing itself
lives in ``app/observability.py``'s ``demo()`` (``python -m app.observability``);
this file checks the middleware is actually wired into the app.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import Response

from app.observability import REQUEST_ID_HEADER, request_id_middleware

logger = logging.getLogger("app.observability.contract_check")


def _fake_request(headers: dict[str, str]) -> Request:
    encoded = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": encoded, "method": "GET", "path": "/x"}
    return Request(scope)


async def test_a_request_id_is_generated_and_echoed_back(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    request_id = resp.headers.get(REQUEST_ID_HEADER)
    assert request_id  # non-empty
    assert len(request_id) == 32  # uuid4().hex, matching this service's own
    # outbound convention in app/documents/service.py


async def test_an_inbound_request_id_is_honoured_not_replaced(client):
    resp = await client.get("/health", headers={REQUEST_ID_HEADER: "caller-supplied-id"})
    assert resp.headers.get(REQUEST_ID_HEADER) == "caller-supplied-id"


async def test_log_records_made_during_the_request_carry_its_id(caplog):
    """The point of the middleware: a log line from deep inside a handler
    picks up the id without the handler passing it explicitly."""

    async def call_next(_request: Request) -> Response:
        logger.info("mid-request")
        return Response(status_code=200)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await request_id_middleware(
            _fake_request({REQUEST_ID_HEADER: "trace-me-123"}), call_next
        )
    (record,) = [r for r in caplog.records if r.message == "mid-request"]
    assert record.request_id == "trace-me-123"


async def test_the_id_does_not_leak_to_log_records_after_the_request(caplog):
    """The contextvar is reset in the middleware's finally block, so a log
    line emitted once the request has finished must not carry its id."""

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await request_id_middleware(
            _fake_request({REQUEST_ID_HEADER: "should-not-leak"}), call_next
        )
        logger.info("after the request")
    (record,) = [r for r in caplog.records if r.message == "after the request"]
    assert getattr(record, "request_id", None) is None

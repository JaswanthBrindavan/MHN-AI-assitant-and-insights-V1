"""Request-scoped logging plumbing.

Audit R7: nothing in ``app/`` ever called ``logging.basicConfig``/
``dictConfig``, so in production the root logger had no handler. Every
``logger.info`` was silently dropped and every ``logger.warning(...,
exc_info=True)`` — the pattern all ~113 ``except Exception`` sites use instead
of swallowing — fell through to ``logging.lastResort``, losing the level,
logger name and timestamp. ``configure_logging`` fixes that with one stdlib
handler; ``request_id_middleware`` stamps every log line made while handling
a request with an id, so a support ticket naming one request can be grepped
out of the noise.

Stdlib only — matching ``app/telemetry.py``'s rule about dependencies in a
codebase holding patient data. JSON lines because Railway (the deploy target;
see ``railway.toml``) reads and filters structured stdout, and it costs
nothing extra over a format string.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

# Matches this service's existing outbound convention (see
# app/documents/service.py's calls to mhn-ai) rather than inventing a new one.
REQUEST_ID_HEADER = "X-Request-Id"

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_configured = False


class _RequestIdFilter(logging.Filter):
    """Stamp the current request id (if any) onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Attach one JSON stdout handler to the root logger. Idempotent.

    ``create_app()`` is called once per test via ``tests/conftest.py``'s
    ``client`` fixture — thousands of times across the suite — so this must
    be safe to call repeatedly: it only ADDS a handler, and only the first
    time, rather than replacing ``root.handlers`` outright. Replacing the
    list would also rip out pytest's own log-capture handler whenever this
    runs during a test (``caplog`` in ``tests/test_chat_tools.py``).

    uvicorn configures its own ``uvicorn``/``uvicorn.access`` loggers (not the
    root logger) via its own dictConfig, so this coexists with it rather than
    double-configuring: uvicorn's access/error lines keep uvicorn's own
    format, and everything from ``app.*`` gets this JSON line.

    Sets the root level to INFO — the same default ``Settings.log_level``
    uses — rather than Python's own WARNING default. ``get_settings()``
    (the only caller) corrects the level from ``LOG_LEVEL`` right after this
    returns; INFO here just means the one-time startup lines ``get_settings()``
    itself logs (e.g. the auth-mode announcement in ``app/config.py``) aren't
    dropped by a level check that hasn't been set yet.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _configured = True


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Give every request an id, log-visible and echoed back.

    Honours an inbound ``X-Request-Id`` (a caller like mhn-spring may already
    have minted one); mints ``uuid4().hex`` otherwise, matching the format
    this service already uses when IT is the caller. The id is set on a
    contextvar before ``call_next`` runs, so every log record produced by the
    request — including deep inside the orchestrator — carries it via
    ``_RequestIdFilter``, and it is echoed back on the response so a caller
    can correlate its own logs with ours.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    token = _request_id.set(request_id)
    try:
        response = await call_next(request)
    finally:
        _request_id.reset(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def demo() -> None:
    """Self-check: request id set during a call is on the record; absent
    outside one. Run directly: ``python -m app.observability``."""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())
    logger = logging.getLogger("app.observability.demo")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("outside any request")
    line_without = json.loads(stream.getvalue().splitlines()[-1])
    assert "request_id" not in line_without
    assert line_without["message"] == "outside any request"

    token = _request_id.set("abc123")
    try:
        logger.info("inside a request")
    finally:
        _request_id.reset(token)
    line_with = json.loads(stream.getvalue().splitlines()[-1])
    assert line_with["request_id"] == "abc123"

    logger.info("after the request ends")
    line_after = json.loads(stream.getvalue().splitlines()[-1])
    assert "request_id" not in line_after  # contextvar reset, not leaked

    print("app.observability self-check OK")


if __name__ == "__main__":
    demo()

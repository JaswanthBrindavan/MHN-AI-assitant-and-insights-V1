"""The sweep trigger, and reading the memory document.

Both exist because of one measured fact: `SELECT ... FROM job_runs WHERE
name='nightly_sweep'` returned **0 rows** on a staging environment that had been
in use for weeks.

`memory_document.refresh()` has exactly one caller — `scripts/nightly_sweep.py`
— and `railway.toml` deploys one service, with the sweep described only in a
comment as a second service that was never created. So the document was never
built, `execute_due` never fired (no erasure ever completed), and retention
never ran. Nothing looked wrong: the chat read path falls back to assembling
memory per turn and records a fail-open.

`POST /admin/sweep` lets any external scheduler drive it without a second
Railway service. `GET /profile/memory` makes the document inspectable, which
`app/api/v1/profile.py` argues in its own docstring is not optional.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.jobs import JobRun

TOKEN = "s" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "service_token", TOKEN)


# --------------------------------------------------------------------------
# POST /api/v1/admin/sweep
# --------------------------------------------------------------------------

async def test_sweep_requires_the_service_token(client):
    assert (await client.post("/api/v1/admin/sweep")).status_code == 401


async def test_sweep_rejects_a_wrong_token(client):
    resp = await client.post(
        "/api/v1/admin/sweep", headers={"Authorization": "Bearer " + "x" * 40}
    )
    assert resp.status_code == 401


async def test_sweep_returns_202_without_waiting(client):
    """202, not 200.

    The first run on an environment that has never swept processes the whole
    backlog — every due erasure and every message/receipt past retention — so
    holding the request open would exceed any proxy timeout.
    """
    resp = await client.post("/api/v1/admin/sweep", headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["started"] is True
    assert "job_runs" in body["detail"]


async def test_sweep_actually_runs_and_records_a_job(
    client, sessionmaker, monkeypatch
):
    """The endpoint is only useful if it leaves the audit row behind.

    That row is the whole diagnostic: a caller polls `job_runs` instead of
    holding a socket, and its ABSENCE is what revealed the sweep had never run.
    """
    import asyncio

    import app.db as app_db

    # The endpoint deliberately builds its OWN session — the request session
    # closes long before a full sweep finishes — so it calls get_sessionmaker()
    # rather than taking the injected dependency. That is correct in
    # production and is exactly why the test has to redirect it here.
    monkeypatch.setattr(app_db, "get_sessionmaker", lambda: sessionmaker)

    resp = await client.post("/api/v1/admin/sweep", headers=HEADERS)
    assert resp.status_code == 202

    # Wait for the detached task, do not poll for its row.
    #
    # Every session in this suite shares ONE connection (in-memory SQLite on a
    # StaticPool), so a 10 ms polling loop and the sweep contend for the same
    # object: the loop starved the task, which sat at "running" until the
    # engine fixture disposed the pool underneath it. SQLAlchemy's pool
    # teardown then does `del self.__dict__["connection"]` on a slot the
    # live task had already taken, which surfaces as a KeyError teardown
    # ERROR -- not a failure -- and only on some orderings.
    #
    # Draining the task first removes both the contention and the race,
    # and lets this assert the status it actually cares about: the row
    # existing says the trigger fired, only 'succeeded' says the sweep ran.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 30)

    async with sessionmaker() as db:
        rows = (
            await db.execute(select(JobRun).where(JobRun.name == "nightly_sweep"))
        ).scalars().all()
    assert rows, "sweep did not record a job_runs row"

    assert rows[0].trigger == "cron"
    assert rows[0].status == "succeeded"
    # actor_user_id stays NULL: scheduled work has no actor, and NULL means
    # "the system", never "an unknown user".
    assert rows[0].actor_user_id is None


# --------------------------------------------------------------------------
# GET /api/v1/profile/memory
# --------------------------------------------------------------------------

async def test_memory_view_reports_not_built_rather_than_404(client):
    """`built: false` is a real state, not an error.

    It is exactly the state every environment is in when the sweep has never
    run, and a 404 would read as "this endpoint is broken" instead of "nothing
    has assembled one yet".
    """
    resp = await client.get("/api/v1/profile/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["built"] is False
    assert body["prompt_block"] is None
    assert "nightly sweep" in body["detail"]


async def test_memory_view_returns_the_exact_text_that_reaches_the_model(
    client, sessionmaker
):
    from app.auth import DEV_USER_ID
    from app.memory import document as memory_document

    user_id = DEV_USER_ID  # already a UUID
    async with sessionmaker() as db:
        row = await memory_document.refresh(db, user_id)
        assert row is not None, "refresh should build a document"
        await db.commit()
        expected_block = row.prompt_block
        expected_tokens = row.token_estimate

    resp = await client.get("/api/v1/profile/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["built"] is True
    assert body["fresh"] is True
    # Verbatim: the point of the endpoint is that the reader sees what the
    # model sees, not a summary of it.
    assert body["prompt_block"] == expected_block
    assert body["token_estimate"] == expected_tokens
    assert body["built_at"] is not None


async def test_memory_view_is_scoped_to_the_caller(client, sessionmaker):
    """A document built for someone else must not surface here."""
    from app.memory import document as memory_document

    other = uuid.uuid4()
    async with sessionmaker() as db:
        await memory_document.refresh(db, other)
        await db.commit()

    resp = await client.get("/api/v1/profile/memory")
    assert resp.status_code == 200
    assert resp.json()["built"] is False

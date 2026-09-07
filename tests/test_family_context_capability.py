"""The Family Connect AI-context switch, read INBOUND, now buys something.

`family_connect.req_ai_context_access` / `acc_ai_context_access` is an
OUTBOUND grant: the flag on MY side means "this connected member may use my
data for their own analysis". PR #93 removed the reading of it as consent for
the reader's OWN chat, which left nothing in Ink reading it at all -- a column
the Family Connect screen writes and that changed no behaviour anywhere.

Read from the other end it is a real permission, and this is what it buys: a
member who has granted THIS reader AI context has their own non-private
recorded conditions appended to the reader's [P] block, as family history from
the member's actual record rather than only what the reader typed about them.

Two gates, and both must say yes:

* **per-member consent** -- the INBOUND flag, from the shared database
  (`members_granting_ai_context`): `acc_*` when the reader sent the request,
  `req_*` when the reader accepted;
* **the plan** -- `family_context` from mhn-spring `GET /entitlements/{userId}`,
  which mhn-spring PR #78 defines as Care+ AND at least one such connection.

And one thing that must NOT change: a family *pull* -- the member's shared
documents, lab values and conditions read through `handle_family_record_query`
-- is governed by the FILE READ grant and `file_access_exclusions`, and works
identically with the AI-context switch on or off. The owner's ruling, verbatim:
"with the toggle on or off still it should be able to pull the thps/doc and
medical conditions." Every consent combination below re-checks the pull.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.context import build_patient_context, clear_patient_context_memo
from app.chat.data_handlers import handle_family_record_query
from app.config import get_settings
from app.models.coredata import (
    FamilyConnect,
    MedicalCondition,
    Relation,
)

READER = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
MOTHER = uuid.UUID("00000000-0000-0000-0000-0000000000f2")


# --------------------------------------------------------------------------- #
# Fixtures: a mother who shares her files, with one shared and one private
# condition. Whether she also granted AI context is the parameter under test.
# --------------------------------------------------------------------------- #
async def _connect(
    db,
    *,
    reader_is_requester: bool = True,
    req_ai: bool | None = None,
    acc_ai: bool | None = None,
    read_grant: bool = True,
):
    """One accepted link between READER and MOTHER.

    ``read_grant`` is the FILE grant, set on the OWNER's side (the mother's) so
    a pull works; the AI flags are passed BY COLUMN so a test that puts a grant
    on the wrong side reads as exactly that.
    """
    # ``relations.name`` is read from the REQUESTER's side and ``inverse`` from
    # the acceptor's, so the row flips with the direction of the link -- the
    # reader must see "Mother" either way.
    rel = (
        Relation(name="Mother", inverse="Child") if reader_is_requester
        else Relation(name="Child", inverse="Mother")
    )
    db.add(rel)
    await db.flush()
    db.add(FamilyConnect(
        requester_id=READER if reader_is_requester else MOTHER,
        acceptor_id=MOTHER if reader_is_requester else READER,
        accepted=True,
        relation_id=rel.id,
        # The owner is whichever side the MOTHER occupies.
        req_read=read_grant if not reader_is_requester else None,
        acc_read=read_grant if reader_is_requester else None,
        req_ai_context_access=req_ai,
        acc_ai_context_access=acc_ai,
    ))
    db.add(MedicalCondition(
        user_id=MOTHER, name="Hypothyroidism", type="condition",
        status="active", private=False,
    ))
    db.add(MedicalCondition(
        user_id=MOTHER, name="Depression", type="condition",
        status="active", private=True,
    ))
    await db.flush()


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class _Spring:
    """Stands in for mhn-spring. ``calls`` is what the round-trip budget is
    actually about, so it is counted rather than assumed."""

    def __init__(self, resp=None, boom: Exception | None = None):
        self.resp = resp
        self.boom = boom
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if self.boom is not None:
            raise self.boom
        return self.resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def spring(monkeypatch):
    """Configure a Spring base + a reader JWT, and hand back the stub client.

    Without BOTH of these the client cannot make a call at all -- which is
    itself one of the fail-closed cases below, tested separately.
    """
    from app import entitlements
    from app.auth import _current_user_jwt

    monkeypatch.setattr(get_settings(), "mhn_spring_base_url", "http://spring")
    token = _current_user_jwt.set("reader-jwt")
    stub = _Spring(_Resp(200, {"premium": True, "ai_context": True,
                               "family_context": True}))
    monkeypatch.setattr(
        entitlements.httpx, "AsyncClient", lambda *a, **k: stub
    )
    yield stub
    _current_user_jwt.reset(token)


async def _context(db) -> str:
    clear_patient_context_memo(db)
    text, _codes = await build_patient_context(db, READER)
    return text


async def _pull(db) -> dict:
    """The regression guard, run in every consent combination below."""
    r = await handle_family_record_query(
        db, READER, "what medical conditions does my mother have?"
    )
    assert r is not None
    return r


def _assert_pull_works(r: dict) -> None:
    assert r["provenance"]["resolved"] is True
    assert r["provenance"]["conditions"] == ["Hypothyroidism"]
    assert "Hypothyroidism" in r["reply"]
    assert "Depression" not in r["reply"]


# --------------------------------------------------------------------------- #
# 1. Granted + entitled -> the member's conditions reach the reader's context
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reader_is_requester", "req_ai", "acc_ai"),
    [
        # Reader sent the request -> the mother is the ACCEPTOR -> acc_*.
        (True, False, True),
        # Reader accepted -> the mother is the REQUESTER -> req_*.
        (False, True, False),
    ],
)
async def test_a_granting_members_conditions_reach_the_readers_context(
    db_session, spring, reader_is_requester, req_ai, acc_ai
):
    await _connect(db_session, reader_is_requester=reader_is_requester,
                   req_ai=req_ai, acc_ai=acc_ai)

    text = await _context(db_session)
    assert "Hypothyroidism" in text
    assert "your mother" in text
    assert len(spring.calls) == 1, "the plan must be asked exactly once"

    _assert_pull_works(await _pull(db_session))


async def test_the_readers_own_grant_is_not_the_members_consent(db_session, spring):
    """The reader sent the request, so ``req_ai_context_access`` is the READER
    granting the mother -- outbound. Reading it here would let a reader switch
    on their relative's data for themselves, which is the whole bug PR #93
    fixed, pointed the other way."""
    await _connect(db_session, reader_is_requester=True, req_ai=True, acc_ai=False)

    assert "Hypothyroidism" not in await _context(db_session)
    assert spring.calls == [], "no consent, so the plan is never asked"

    _assert_pull_works(await _pull(db_session))


# --------------------------------------------------------------------------- #
# 2. Care+ is the other gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"premium": False, "ai_context": False, "family_context": False},
        # On Care+ but with no qualifying connection, per mhn-spring PR #78.
        {"premium": True, "ai_context": True, "family_context": False},
        {},                       # a shape that says nothing
        {"family_context": "yes"},  # not a boolean true
    ],
)
async def test_a_granting_member_without_the_plan_is_withheld(
    db_session, spring, payload
):
    spring.resp = _Resp(200, payload)
    await _connect(db_session, req_ai=False, acc_ai=True)

    assert "Hypothyroidism" not in await _context(db_session)
    assert len(spring.calls) == 1

    _assert_pull_works(await _pull(db_session))


# --------------------------------------------------------------------------- #
# 3. No grant at all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("req_ai", "acc_ai"),
    [(False, False), (None, None), (True, False)],
)
async def test_no_grant_from_the_member_means_no_context(
    db_session, spring, req_ai, acc_ai
):
    """NULL included: a database predating mhn-spring V27 has nobody opted in."""
    await _connect(db_session, reader_is_requester=True,
                   req_ai=req_ai, acc_ai=acc_ai)

    assert "Hypothyroidism" not in await _context(db_session)
    assert spring.calls == []

    _assert_pull_works(await _pull(db_session))


async def test_a_pending_link_is_not_a_grant(db_session, spring):
    """Spring only lets the flag be edited on an accepted row, so a pending
    row carries the column default and no decision."""
    rel = Relation(name="Mother", inverse="Child")
    db_session.add(rel)
    await db_session.flush()
    db_session.add(FamilyConnect(
        requester_id=READER, acceptor_id=MOTHER, accepted=False,
        relation_id=rel.id, acc_read=True, acc_ai_context_access=True,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Hypothyroidism", type="condition", private=False,
    ))
    await db_session.flush()

    assert "Hypothyroidism" not in await _context(db_session)
    assert spring.calls == []


# --------------------------------------------------------------------------- #
# 4. Private is never shared, granted or not
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("granted", [True, False])
async def test_a_private_condition_never_appears(db_session, spring, granted):
    """``shared_only=True`` is mhn-spring's own getByUserIdAndIsPrivateFalse.
    A grant of AI context is not a grant over what the member marked private."""
    await _connect(db_session, req_ai=False, acc_ai=granted)

    text = await _context(db_session)
    assert "Depression" not in text
    assert ("Hypothyroidism" in text) is granted

    r = await _pull(db_session)
    _assert_pull_works(r)


async def test_a_member_with_only_private_conditions_adds_no_line(
    db_session, spring
):
    """Nothing shareable must produce no sentence at all -- not an empty
    "your mother — ." fragment in the prompt."""
    rel = Relation(name="Mother", inverse="Child")
    db_session.add(rel)
    await db_session.flush()
    db_session.add(FamilyConnect(
        requester_id=READER, acceptor_id=MOTHER, accepted=True,
        relation_id=rel.id, acc_read=True, acc_ai_context_access=True,
    ))
    db_session.add(MedicalCondition(
        user_id=MOTHER, name="Depression", type="condition", private=True,
    ))
    await db_session.flush()

    assert await _context(db_session) == ""


# --------------------------------------------------------------------------- #
# 5. Spring down / unconfigured / 500 -> capability off, turn unaffected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "failure",
    ["unreachable", "500", "403", "bad_json", "timeout"],
)
async def test_spring_failing_turns_the_capability_off_without_breaking_the_turn(
    db_session, spring, failure
):
    import httpx

    if failure == "unreachable":
        spring.boom = httpx.ConnectError("no route")
    elif failure == "timeout":
        spring.boom = httpx.ReadTimeout("too slow")
    elif failure == "bad_json":
        spring.boom = ValueError("not json")
    else:
        spring.resp = _Resp(int(failure))

    await _connect(db_session, req_ai=False, acc_ai=True)

    # The turn still produces a context (empty here -- the reader has no
    # pedigree of their own) and, crucially, does not raise.
    assert "Hypothyroidism" not in await _context(db_session)
    _assert_pull_works(await _pull(db_session))


async def test_an_unconfigured_spring_makes_no_call_and_grants_nothing(
    db_session, monkeypatch
):
    """Same shape as every other Spring client here: empty base URL means the
    call cannot be made. For an enrichment read that fails open; for a consent
    gate it must fail CLOSED, and it does."""
    monkeypatch.setattr(get_settings(), "mhn_spring_base_url", "")
    await _connect(db_session, req_ai=False, acc_ai=True)

    assert "Hypothyroidism" not in await _context(db_session)
    _assert_pull_works(await _pull(db_session))


async def test_no_reader_jwt_means_no_call(db_session, monkeypatch):
    """Spring's only auth filter parses a USER JWT; there is no service path.
    No token captured, no call, no grant."""
    from app import entitlements

    monkeypatch.setattr(get_settings(), "mhn_spring_base_url", "http://spring")
    stub = _Spring(_Resp(200, {"family_context": True}))
    monkeypatch.setattr(entitlements.httpx, "AsyncClient", lambda *a, **k: stub)

    await _connect(db_session, req_ai=False, acc_ai=True)

    assert "Hypothyroidism" not in await _context(db_session)
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# 6. Cost
# --------------------------------------------------------------------------- #
async def test_the_plan_is_asked_once_per_session_not_once_per_call(
    db_session, spring
):
    """`build_patient_context` is called up to twice a turn and memoises on
    `db.info`; the entitlements call rides that memo rather than adding a
    second HTTP round trip."""
    await _connect(db_session, req_ai=False, acc_ai=True)
    clear_patient_context_memo(db_session)

    for _ in range(4):
        assert "Hypothyroidism" in (await build_patient_context(db_session, READER))[0]
    assert len(spring.calls) == 1


async def test_camel_case_from_spring_is_accepted(db_session, spring):
    """A Jackson naming strategy must not silently switch the gate off."""
    spring.resp = _Resp(200, {"familyContext": True})
    await _connect(db_session, req_ai=False, acc_ai=True)

    assert "Hypothyroidism" in await _context(db_session)

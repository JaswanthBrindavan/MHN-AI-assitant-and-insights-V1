"""Family context in chat is gated on the Family Connect AI-context switch.

`build_patient_context` used to put the reader's family history into the
prompt on every turn; the only thing that could stop it was a pending erasure.
The Family Connect page has had a per-link "family context" switch in
production the whole time (`family_connect.req_ai_context_access` /
`acc_ai_context_access`, mhn-spring V27) and it governed nothing here.

Three things are pinned:

1. **Which side's flag is the reader's.** Each column is the grant over its
   OWN side's context — `req_*` is written by the requester about the
   requester, `acc_*` by the acceptor about the acceptor. So the reader's
   consent is the flag on the side they occupy, and a relative's flag is
   never a substitute for it.
2. **What a reader with no link gets.** Their history, unchanged: the switch
   is a grant over a relationship and does not exist for them to have set.
3. **The memo cannot serve a pre-consent answer.** The gate runs before the
   result is stored, and the memo dies with the session, so the next request
   after a switch moves sees the new state.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat.context import build_patient_context, clear_patient_context_memo
from app.chat.orchestrator import handle_chat
from app.config import get_settings
from app.llm.fake import FakeProvider
from app.llm.tools import join_system
from app.models.core import PedigreeCondition, PedigreeMember
from app.models.coredata import FamilyConnect
from app.models.rules import InsightArtifact

READER = uuid.UUID("00000000-0000-0000-0000-00000000c0de")
RELATIVE = uuid.UUID("00000000-0000-0000-0000-00000000da0d")


async def _seed_history(db, user_id=READER):
    """The reader's own record of a mother with type 2 diabetes, plus the
    insight recompute derived from it — both sources build_patient_context
    reads, so both must be withheld together."""
    db.add(PedigreeMember(user_id=user_id, slot="mother", vital_status="alive"))
    db.add(PedigreeCondition(
        user_id=user_id, slot="mother", condition_code="T2DM",
        condition_display="type 2 diabetes", onset_band="55_59",
        certainty="confirmed", provenance="self_report", soft_deleted=False,
    ))
    db.add(InsightArtifact(
        user_id=user_id, condition_code="T2DM", tier="elevated", title="t",
        body="b", template_key="k", template_version=1, pipeline_version=1,
        content_hash="c" * 64, status="active",
    ))
    await db.flush()


def _link(*, reader_is_requester: bool, accepted: bool = True,
          req_ai: bool | None = None, acc_ai: bool | None = None) -> FamilyConnect:
    """One family_connect row between READER and RELATIVE. The flags are passed
    by COLUMN, not by role, so a test that puts the grant on the wrong side
    reads as exactly that."""
    return FamilyConnect(
        requester_id=READER if reader_is_requester else RELATIVE,
        acceptor_id=RELATIVE if reader_is_requester else READER,
        accepted=accepted,
        req_ai_context_access=req_ai,
        acc_ai_context_access=acc_ai,
    )


# --------------------------------------------------------------------------- #
# 1. Which side's flag is the reader's
# --------------------------------------------------------------------------- #
async def test_the_requesters_own_switch_lets_their_history_through(db_session):
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=True, acc_ai=False))
    await db_session.flush()

    text, codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text
    assert "T2DM" in codes


async def test_the_acceptors_own_switch_lets_their_history_through(db_session):
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=False, req_ai=False, acc_ai=True))
    await db_session.flush()

    text, codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text
    assert "T2DM" in codes


async def test_a_relatives_grant_is_not_the_readers_consent(db_session):
    """The reader ACCEPTED the link, so req_ai_context_access is the relative
    saying "you may use MY context". It says nothing about the reader's own
    history, and reading it as consent would let a relative switch on the
    reader's personalisation for them."""
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=False, req_ai=True, acc_ai=False))
    await db_session.flush()

    text, codes = await build_patient_context(db_session, READER)
    assert text == ""
    assert codes == set()


async def test_the_switch_off_withholds_history_and_condition_codes(db_session):
    """Codes too: they scope retrieval, so leaving them would let the father's
    diabetes pick the profile the answer cites — the same history by a side
    door. This is the same shape as the erasure suppression."""
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=False, acc_ai=False))
    await db_session.flush()

    assert await build_patient_context(db_session, READER) == ("", set())


async def test_null_flags_read_as_off(db_session):
    """Production has the columns NOT NULL DEFAULT false; a NULL means a
    database without V27, where nobody has opted in. There is no legacy
    column to fall back to, unlike req_read/acc_read."""
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=None, acc_ai=None))
    await db_session.flush()

    assert await build_patient_context(db_session, READER) == ("", set())


async def test_one_granting_link_is_enough(db_session):
    """Withholding context from one relative is a decision about that
    relative, not about the reader's own chat."""
    await _seed_history(db_session)
    other = uuid.uuid4()
    db_session.add(_link(reader_is_requester=True, req_ai=False, acc_ai=True))
    db_session.add(FamilyConnect(
        requester_id=other, acceptor_id=READER, accepted=True,
        req_ai_context_access=False, acc_ai_context_access=True,
    ))
    await db_session.flush()

    text, _codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text


# --------------------------------------------------------------------------- #
# 2. A reader with no link
# --------------------------------------------------------------------------- #
async def test_a_reader_with_no_link_keeps_their_history(db_session):
    """No accepted link means no switch exists for the reader to have set
    either way. Their pedigree is their own record, and this is what every
    reader got before the gate — the gate must not take it from the people
    the toggle was never about."""
    await _seed_history(db_session)

    text, codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text
    assert "T2DM" in codes


async def test_a_pending_link_is_not_a_link(db_session):
    """Spring only lets the switch be edited on an accepted row, so whatever a
    pending row carries is the column default and not a decision. Until the
    other side accepts, the reader is a reader with no link."""
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, accepted=False, req_ai=False))
    await db_session.flush()

    text, _codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text


# --------------------------------------------------------------------------- #
# 3. The memo cannot serve a pre-consent answer
# --------------------------------------------------------------------------- #
async def test_the_memo_stores_the_gated_result(db_session):
    """If the gate ran on the way OUT of the memo instead of the way in, a
    cached hit would carry the ungated history. The stored value must already
    be the empty one."""
    from app.chat.context import _MEMO_KEY

    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=False))
    await db_session.flush()

    assert await build_patient_context(db_session, READER) == ("", set())
    assert db_session.info[_MEMO_KEY][READER] == ("", set())


async def test_switching_on_is_seen_by_the_next_session(sessionmaker):
    """The switch is written by mhn-spring in a different request; the memo
    lives in db.info and so dies with the session that computed the refusal."""
    async with sessionmaker() as first:
        await _seed_history(first)
        link = _link(reader_is_requester=True, req_ai=False)
        first.add(link)
        await first.commit()
        assert await build_patient_context(first, READER) == ("", set())

        link.req_ai_context_access = True
        await first.commit()
        # Same session: memoised refusal, exactly as a pending erasure would be.
        assert await build_patient_context(first, READER) == ("", set())

    async with sessionmaker() as second:
        text, _codes = await build_patient_context(second, READER)
        assert "type 2 diabetes" in text


async def test_switching_off_is_seen_by_the_next_session(sessionmaker):
    """The other direction matters more: a granted answer must not outlive
    the grant into the next request."""
    async with sessionmaker() as first:
        await _seed_history(first)
        link = _link(reader_is_requester=False, acc_ai=True)
        first.add(link)
        await first.commit()
        text, _codes = await build_patient_context(first, READER)
        assert "type 2 diabetes" in text

        link.acc_ai_context_access = False
        await first.commit()

    async with sessionmaker() as second:
        assert await build_patient_context(second, READER) == ("", set())


async def test_clearing_the_memo_re_asks_the_switch(db_session):
    """Belt and braces for a caller that flips the switch and chats on ONE
    session — the same path request_erasure relies on."""
    await _seed_history(db_session)
    link = _link(reader_is_requester=True, req_ai=False)
    db_session.add(link)
    await db_session.flush()
    assert await build_patient_context(db_session, READER) == ("", set())

    link.req_ai_context_access = True
    await db_session.flush()
    clear_patient_context_memo(db_session)
    text, _codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text


# --------------------------------------------------------------------------- #
# End to end: the prompt itself, on both engines
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
async def test_the_prompt_carries_history_only_with_the_switch_on(
    sessionmaker, monkeypatch, engine_name
):
    """What the unit tests above stand in for. The gate has to hold on both
    engines, because a guard one engine can route around is the recurring
    bug class here."""
    monkeypatch.setattr(get_settings(), "chat_engine", engine_name)

    captured: list[str] = []

    class Spy(FakeProvider):
        async def generate(self, *, system, user):
            captured.append(join_system(system))
            return "General information [GK]."

        async def generate_turn(self, *, system, messages, tools=()):
            from app.llm.tools import LLMTurn

            captured.append(join_system(system))
            return LLMTurn(text="General information [GK].")

    async with sessionmaker() as db:
        await _seed_history(db)
        link = _link(reader_is_requester=True, req_ai=False)
        db.add(link)
        await db.commit()
        await handle_chat(db, READER, "why am I so tired?", Spy(), uuid.uuid4())
        assert captured, "the provider was not called"
        assert "type 2 diabetes" not in captured[0], (
            "family history reaches the prompt with the switch off"
        )
        link.req_ai_context_access = True
        await db.commit()

    captured.clear()
    async with sessionmaker() as db:
        await handle_chat(db, READER, "why am I so tired?", Spy(), uuid.uuid4())
        assert captured, "the provider was not called"
        assert "type 2 diabetes" in captured[0], (
            "fixture problem: the family history never reached the prompt at all"
        )

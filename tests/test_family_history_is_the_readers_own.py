"""The reader's own family history reaches their own prompt regardless of
the Family Connect AI-context switch.

`family_connect.req_ai_context_access` / `acc_ai_context_access` is an
OUTBOUND grant: the flag on MY side means "a connected member may use my
data for their own analysis". It is not a switch about my own chat.
mhn-spring's `editAccessControls` writes the caller's own side under the
comment "the recipient's resulting grants on our files", and the Android
Family Permissions screen binds the same field under "<name> can".

PR #70 read that flag as INBOUND consent and withheld the reader's own
pedigree -- family history they entered themselves, the way any clinician
takes it -- whenever they had not granted AI context to some relative. What
personalises the reader's own chat is personalization consent, checked
elsewhere. Two things are pinned:

1. **The switch does not touch the reader's own history.** Off on every
   side, on the wrong side, NULL, no link at all: the history is theirs.
2. **A pending erasure still withholds it.** That was the other half of
   PR #70 and it closed a real bug: the turn after a "forget me" still
   carried the reader's family history into the prompt, because gating only
   `memory_assembly` left this path open.
"""

from __future__ import annotations

import uuid

import pytest

from app.chat import erasure
from app.chat.context import build_patient_context
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
    insight recompute derived from it -- both sources build_patient_context
    reads."""
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
    """One family_connect row between READER and RELATIVE, flags by COLUMN."""
    return FamilyConnect(
        requester_id=READER if reader_is_requester else RELATIVE,
        acceptor_id=RELATIVE if reader_is_requester else READER,
        accepted=accepted,
        req_ai_context_access=req_ai,
        acc_ai_context_access=acc_ai,
    )


# --------------------------------------------------------------------------- #
# 1. The switch does not touch the reader's own history
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reader_is_requester", "req_ai", "acc_ai"),
    [
        (True, False, False),   # the reader's own switch off
        (False, False, False),
        (True, None, None),     # a database without V27
        (False, True, False),   # only the RELATIVE granted (to the reader)
        (True, False, True),
    ],
)
async def test_history_reaches_the_prompt_with_no_grant_anywhere(
    db_session, reader_is_requester, req_ai, acc_ai
):
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=reader_is_requester,
                         req_ai=req_ai, acc_ai=acc_ai))
    await db_session.flush()

    text, codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text
    assert "T2DM" in codes


async def test_a_reader_with_no_link_keeps_their_history(db_session):
    await _seed_history(db_session)

    text, codes = await build_patient_context(db_session, READER)
    assert "type 2 diabetes" in text
    assert "T2DM" in codes


@pytest.mark.parametrize("engine_name", ["legacy", "agentic"])
async def test_the_prompt_carries_history_with_the_switch_off(
    db_session, monkeypatch, engine_name
):
    """End to end on both engines: the switch off everywhere, and the
    reader's own history still in the model's prompt."""
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

    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=False, acc_ai=False))
    await db_session.flush()
    await handle_chat(db_session, READER, "why am I so tired?", Spy(), uuid.uuid4())
    assert captured, "the provider was not called"
    assert "type 2 diabetes" in captured[0], (
        "the reader's own family history is withheld by their outbound grant"
    )


# --------------------------------------------------------------------------- #
# 2. A pending erasure still withholds it
# --------------------------------------------------------------------------- #
async def test_a_pending_erasure_withholds_history_and_codes(db_session):
    """PR #70's real fix. Codes too: they scope retrieval, so leaving them
    would let the mother's diabetes pick the profile the answer cites."""
    await _seed_history(db_session)
    db_session.add(_link(reader_is_requester=True, req_ai=True, acc_ai=True))
    await db_session.flush()
    assert "type 2 diabetes" in (await build_patient_context(db_session, READER))[0]

    await erasure.request_erasure(db_session, READER, grace_days=30)
    await db_session.flush()

    assert await build_patient_context(db_session, READER) == ("", set())

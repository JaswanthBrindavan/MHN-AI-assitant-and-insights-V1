"""The per-user memory document.

Read on every turn, so its size is a recurring bill rather than a one-off
storage cost — derived at ~$5,472/month per +50 tokens at 1M users. The budget
is therefore a property to test, not a guideline.

The safety properties are the other half: only the reader's own data, never a
family member's, and a missing or stale document costs latency rather than an
answer.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.chat import memory_assembly
from app.chat.profile import grant_personalization, update_profile
from app.memory import document as memory_document
from app.models.common import utcnow
from app.models.coredata import MedicalCondition
from app.models.memory_document import SCHEMA_VERSION, UserMemoryDocument
from app.rag.prompt import estimate_tokens

USER = uuid.UUID("00000000-0000-0000-0000-00000000d0c1")


async def _seed(db, user_id=USER):
    await grant_personalization(db, user_id)
    await update_profile(
        db,
        user_id,
        {
            "age_band": "45_59",
            "chronic_conditions": ["type 2 diabetes", "hypertension"],
            "current_medications": ["metformin 1000mg", "telmisartan 40mg"],
        },
    )
    db.add(
        MedicalCondition(
            user_id=user_id, name="Penicillin", type="allergy",
            category="medication", severity="severe", reaction="anaphylaxis",
            private=False,
        )
    )
    await db.flush()


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #
async def test_the_block_stays_within_budget(db_session):
    await _seed(db_session)
    built = await memory_document.build(db_session, USER)
    assert built.token_estimate <= memory_document.MAX_PROMPT_TOKENS


async def test_an_absurdly_full_record_is_trimmed_not_allowed_to_grow(db_session):
    """The ceiling must hold against a reader with everything on file.

    Left unbounded, the block eats the retrieved-knowledge budget and the
    assistant answers health questions with no sources.
    """
    await _seed(db_session)
    huge = {
        "schema_version": SCHEMA_VERSION,
        "safety_note": "The reader's record notes they are pregnant.",
        "medication_allergies": [
            {"name": "Penicillin", "severity": "severe", "reaction": "anaphylaxis"}
        ],
        "profile": {
            "conditions": [f"condition number {i}" for i in range(40)],
            "medications": [f"medicine number {i} 500mg" for i in range(40)],
        },
        "recent_labs": [
            {"test": f"Test {i}", "value": "9.9", "unit": "mg/dL", "on": "2026-08-01"}
            for i in range(40)
        ],
        "recent_documents": [
            {"title": f"Some Report {i}", "on": "2026-08-01"} for i in range(40)
        ],
        "habits_30d": {f"habit_{i}": 12.5 for i in range(20)},
    }
    trimmed, block, tokens = memory_document._fit(huge)
    assert tokens <= memory_document.MAX_PROMPT_TOKENS
    assert "trimmed" in trimmed


def test_safety_content_is_never_trimmed():
    """Whatever else goes, the allergy and the pregnancy note stay."""
    assert "safety_note" not in memory_document._TRIM_ORDER
    assert "medication_allergies" not in memory_document._TRIM_ORDER

    huge = {
        "safety_note": "The reader's record notes they are pregnant.",
        "medication_allergies": [{"name": "Penicillin", "severity": "severe"}],
        "habits_30d": {f"h{i}": 1.0 for i in range(200)},
        "recent_labs": [
            {"test": f"T{i}", "value": "1", "unit": "x"} for i in range(200)
        ],
    }
    _doc, block, tokens = memory_document._fit(huge)
    assert tokens <= memory_document.MAX_PROMPT_TOKENS
    assert "pregnant" in block
    assert "Penicillin" in block


def test_safety_content_comes_first():
    """A reader who is pregnant should not have that fact below their step
    count."""
    block = memory_document.render({
        "safety_note": "The reader's record notes they are pregnant.",
        "habits_30d": {"steps": 3000.0},
        "medication_allergies": [{"name": "Penicillin", "severity": "severe"}],
    })
    assert block.index("pregnant") < block.index("Penicillin") < block.index("steps")


# --------------------------------------------------------------------------- #
# Byte stability — what the cache breakpoint rests on
# --------------------------------------------------------------------------- #
def test_rendering_is_deterministic():
    """Text that varied between identical rebuilds would break the reader's
    cached prefix on every turn."""
    doc = {
        "profile": {"conditions": ["a", "b"], "medications": ["m1", "m2"]},
        "habits_30d": {"water": 2.0, "coffee": 3.0},
        "recent_labs": [{"test": "HbA1c", "value": "7.4", "unit": "%", "on": "2026-08-12"}],
    }
    assert memory_document.render(doc) == memory_document.render(dict(doc))


async def test_an_unchanged_record_does_not_rewrite_the_block(db_session):
    """Same inputs, same hash, same stored text — so the cache survives."""
    await _seed(db_session)
    first = await memory_document.refresh(db_session, USER)
    assert first is not None
    original_block, original_hash = first.prompt_block, first.source_hash

    second = await memory_document.refresh(db_session, USER)
    assert second is not None
    assert second.source_hash == original_hash
    assert second.prompt_block == original_block


async def test_a_changed_record_rebuilds_the_block(db_session):
    await _seed(db_session)
    await memory_document.refresh(db_session, USER)
    before = (await memory_document.get(db_session, USER)).prompt_block  # type: ignore[union-attr]

    await update_profile(db_session, USER, {"chronic_conditions": ["asthma"]})
    await db_session.flush()
    await memory_document.refresh(db_session, USER)

    after = (await memory_document.get(db_session, USER)).prompt_block  # type: ignore[union-attr]
    assert after != before
    assert "asthma" in after


# --------------------------------------------------------------------------- #
# Freshness, and falling back
# --------------------------------------------------------------------------- #
async def test_a_stale_document_is_not_used(db_session):
    await _seed(db_session)
    row = await memory_document.refresh(db_session, USER)
    assert row is not None
    row.built_at = utcnow() - memory_document.FRESHNESS - timedelta(minutes=1)
    await db_session.flush()

    assert memory_document.is_fresh(row) is False


async def test_an_older_schema_version_is_treated_as_stale(db_session):
    """A row whose shape this code does not understand is rebuilt, not read."""
    await _seed(db_session)
    row = await memory_document.refresh(db_session, USER)
    assert row is not None
    row.schema_version = SCHEMA_VERSION - 1
    await db_session.flush()

    assert memory_document.is_fresh(row) is False


async def test_a_missing_document_falls_back_to_live_assembly(db_session):
    """Falling back is ALWAYS safe — it is what ran before the document
    existed, and it is correct, just slower."""
    await _seed(db_session)
    assert await memory_document.get(db_session, USER) is None

    memory = await memory_assembly.assemble(db_session, USER)
    assert memory.from_document is False
    assert "metformin" in memory.profile_text.lower()


async def test_a_fresh_document_is_used(db_session):
    await _seed(db_session)
    await memory_document.refresh(db_session, USER)
    await db_session.flush()

    memory = await memory_assembly.assemble(db_session, USER)
    assert memory.from_document is True
    assert "Penicillin" in memory.profile_text


async def test_a_broken_document_read_still_answers(db_session, monkeypatch):
    """An optimisation must never be a new way for a turn to fail."""
    await _seed(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("document table unavailable")

    monkeypatch.setattr(memory_document, "get", _boom)

    memory = await memory_assembly.assemble(db_session, USER)
    assert memory.from_document is False
    assert memory.profile_text  # the live assembly still produced something


# --------------------------------------------------------------------------- #
# Only the reader's own data
# --------------------------------------------------------------------------- #
async def test_the_document_holds_only_the_readers_own_data(db_session):
    """Family permission is checked LIVE on every read. A document that had
    absorbed a relative's result would survive the revocation that should have
    removed it."""
    await _seed(db_session)
    relative = uuid.uuid4()
    await _seed(db_session, relative)
    db_session.add(
        MedicalCondition(
            user_id=relative, name="Sulfa", type="allergy",
            category="medication", severity="severe", private=False,
        )
    )
    await db_session.flush()

    built = await memory_document.build(db_session, USER)
    assert "Sulfa" not in built.prompt_block
    assert "Penicillin" in built.prompt_block


async def test_a_private_document_is_not_carried_into_every_prompt(db_session):
    """`include_private=False` — a document the reader marked private is not
    something to put in front of the model on every turn."""
    import inspect

    from app.memory.document import _gather

    source = inspect.getsource(_gather)
    assert "include_private=False" in source


async def test_erasure_destroys_the_document(db_session):
    """It is derived, but it is still a copy of the reader's data.

    Deleting it is also what stops a stale document being served after the
    sources behind it are gone.
    """
    from app.chat import erasure

    await _seed(db_session)
    await memory_document.refresh(db_session, USER)
    await db_session.commit()

    await erasure.purge_user(db_session, USER)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(UserMemoryDocument).where(UserMemoryDocument.user_id == USER)
        )
    ).scalars().all()
    assert rows == []


async def test_a_pending_erasure_suppresses_the_document(db_session):
    """The reader asked to be forgotten. A cached copy must not answer for
    the sources that are about to go."""
    from app.chat import erasure

    await _seed(db_session)
    await memory_document.refresh(db_session, USER)
    await erasure.request_erasure(db_session, USER, grace_days=30)
    await db_session.flush()

    memory = await memory_assembly.assemble(db_session, USER)
    assert memory.blocks() == []


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
async def test_lab_values_carry_their_date(db_session):
    """"HbA1c 7.4% (12 Aug)" is honest in a way the bare number is not — a
    stale value at least names when it was true."""
    block = memory_document.render({
        "recent_labs": [
            {"test": "HbA1c", "value": "7.4", "unit": "%", "on": "2026-08-12"}
        ]
    })
    assert "2026-08-12" in block


def test_the_block_says_it_is_not_a_diagnosis():
    block = memory_document.render({"profile": {"conditions": ["asthma"]}})
    assert "never a diagnosis" in block
    assert "anyone else" in block


def test_an_empty_record_produces_no_block():
    """A new reader costs no tokens at all."""
    assert memory_document.render({"schema_version": SCHEMA_VERSION}) == ""


async def test_the_token_estimate_matches_the_block(db_session):
    await _seed(db_session)
    built = await memory_document.build(db_session, USER)
    assert built.token_estimate == estimate_tokens(built.prompt_block)

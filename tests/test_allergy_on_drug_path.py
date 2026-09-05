"""A drug question must not ignore the reader's medication allergies.

The drug-information handler returns from the orchestrator BEFORE
`build_patient_context` runs, and it sits AFTER the engine branch — so it is
legacy-only, and `CHAT_ENGINE` defaults to legacy. (Deliberately not citing
line numbers: they moved once already, in the medicine_master merge.)

`build_drug_reply(drug)` took no user and no session, so the reader's allergies
were not merely unread there: they were unreachable.

This is the mirror image of the bug CLAUDE.md already records — a
drug-interaction refusal sitting inside the legacy branch that the agentic
engine bypassed. Same shape, opposite direction.
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat.orchestrator import handle_chat
from app.config import get_settings
from app.coredata.service import allergy_warning, medication_allergies
from app.llm.fake import FakeProvider
from app.models.coredata import MedicalCondition, MedicineMaster

USER = uuid.UUID("00000000-0000-0000-0000-00000000a11e")

BOTH_ENGINES = pytest.mark.parametrize("engine_name", ["legacy", "agentic"])


async def _seed_drug(db, name: str = "Amoxicillin 500mg Capsule"):
    db.add(
        MedicineMaster(
            name=name,
            # medicine_master's name_normalized trigger, applied by hand for
            # the sqlite fixture: lower(regexp_replace(name,'[^a-zA-Z0-9]+',' ')).
            name_normalized=re.sub(r"[^a-zA-Z0-9]+", " ", name).lower(),
            is_discontinued=False,
            status="approved",
            used_for=["bacterial infections"],
            # A ", "-joined TEXT column since V19, not a list.
            side_effects="Nausea, Rash",
        )
    )
    await db.flush()


async def _seed_allergy(db, *, severity: str = "severe", private: bool = False):
    db.add(
        MedicalCondition(
            user_id=USER,
            name="Penicillin",
            type="allergy",
            category="medication",
            severity=severity,
            reaction="anaphylaxis",
            private=private,
        )
    )
    await db.flush()


async def test_a_severe_medication_allergy_reaches_the_drug_reply(db_session):
    """The scenario: severely penicillin-allergic, asks about amoxicillin.

    Amoxicillin is a penicillin-class drug. The reply used to be a clean
    monograph with no mention of the allergy on record.
    """
    monkey_settings = get_settings()
    object.__setattr__(monkey_settings, "chat_engine", "legacy") if hasattr(
        monkey_settings, "__setattr__"
    ) else None

    await _seed_drug(db_session)
    await _seed_allergy(db_session)

    result = await handle_chat(
        db_session,
        USER,
        "side effects of amoxicillin 500mg capsule",
        FakeProvider(),
        uuid.uuid4(),
    )

    assert result.provenance.get("path") == "drug_query", (
        f"expected the deterministic drug path, got "
        f"{result.provenance.get('path')!r}"
    )
    assert "Penicillin" in result.response_message, (
        "the reader's severe medication allergy was not mentioned"
    )
    assert "pharmacist" in result.response_message.lower()


async def test_the_warning_comes_first(db_session):
    """A warning buried under a monograph is a warning nobody reads."""
    await _seed_drug(db_session)
    await _seed_allergy(db_session)

    result = await handle_chat(
        db_session, USER, "side effects of amoxicillin 500mg capsule",
        FakeProvider(), uuid.uuid4(),
    )

    body = result.response_message
    assert body.index("Penicillin") < body.index("Amoxicillin"), (
        "the allergy warning must precede the drug information"
    )


async def test_a_mild_allergy_does_not_warn(db_session):
    """A warning on every question trains readers to ignore the one that
    matters."""
    await _seed_drug(db_session)
    await _seed_allergy(db_session, severity="mild")

    result = await handle_chat(
        db_session, USER, "side effects of amoxicillin 500mg capsule",
        FakeProvider(), uuid.uuid4(),
    )
    assert "Penicillin" not in result.response_message


async def test_a_private_allergy_is_still_the_readers_own_allergy(db_session):
    """`medical_condition.private` is the FAMILY-sharing switch.

    This test used to assert the allergy was hidden, on the reading that "the
    owning app honours private". It does -- for connected relatives; Spring's
    own record list applies no such predicate. The app also defaults every new
    record to private, so the old rule silently switched the drug-path warning
    off for exactly the readers who had bothered to record an allergy.
    """
    await _seed_allergy(db_session, private=True)
    allergies = await medication_allergies(db_session, USER)
    assert [a.name for a in allergies] == ["Penicillin"]


async def test_a_food_allergy_is_not_a_medication_warning(db_session):
    """Only `category='medication'` belongs on a drug reply."""
    db_session.add(
        MedicalCondition(
            user_id=USER, name="Peanuts", type="allergy", category="food",
            severity="severe", private=False,
        )
    )
    await db_session.flush()

    assert await medication_allergies(db_session, USER) == []


async def test_another_users_allergy_is_never_read(db_session):
    await _seed_allergy(db_session)
    stranger = uuid.uuid4()
    assert await medication_allergies(db_session, stranger) == []


async def test_severe_allergies_are_listed_worst_first(db_session):
    for name, severity in (("Sulfa", "medium"), ("Penicillin", "severe")):
        db_session.add(
            MedicalCondition(
                user_id=USER, name=name, type="allergy", category="medication",
                severity=severity, private=False,
            )
        )
    await db_session.flush()

    allergies = await medication_allergies(db_session, USER)
    assert [a.severity for a in allergies] == ["severe", "medium"]


def test_the_warning_names_nothing_it_cannot_justify():
    """It states what is on record and routes to a pharmacist.

    It deliberately does NOT claim the drug asked about is in the class the
    reader reacts to — that is a clinical judgement Davi has no dataset for,
    and being wrong in either direction is worse than naming the record.
    """
    class _A:
        name = "Penicillin"
        reaction = "anaphylaxis"
        severity = "severe"

    text = allergy_warning([_A()])  # type: ignore[list-item]
    assert "Penicillin" in text
    assert "pharmacist" in text.lower()
    for overclaim in ("you are allergic to this", "do not take", "will cause"):
        assert overclaim not in text.lower()


def test_no_allergies_means_no_warning():
    assert allergy_warning([]) == ""


async def test_a_lookup_failure_does_not_cost_the_answer(db_session, monkeypatch):
    """Fail open. An allergy read that breaks must not break the reply."""
    import app.chat.orchestrator as orch

    await _seed_drug(db_session)

    async def _boom(*a, **k):
        raise RuntimeError("medical_condition unavailable")

    monkeypatch.setattr(orch, "medication_allergies", _boom)

    result = await handle_chat(
        db_session, USER, "side effects of amoxicillin 500mg capsule",
        FakeProvider(), uuid.uuid4(),
    )
    assert result.response_message
    assert "Amoxicillin" in result.response_message

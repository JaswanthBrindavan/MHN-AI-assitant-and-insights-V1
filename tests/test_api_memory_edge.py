"""Edge cases for the v1 API surface and conversation memory/compaction.

API: pedigree validation (422s), soft-delete/resurrect flows, IDOR deletes,
consent-ledger idempotency, artifact supersession/retraction, header auth.
Memory: adversarial medication extraction, cap vs sticky behaviour, two-pass
compaction with sticky survival, and fail-open on DB errors.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.chat.conversation as conversation_mod
from app.chat.conversation import (
    add_message,
    ensure_session,
    latest_summary,
    maybe_compact,
)
from app.chat.memory import (
    CAP,
    compact_messages,
    extract_medications,
    merge_summaries,
)
from app.models.chat import ConversationSummary
from app.models.core import ConsentLedger, PedigreeCondition
from app.models.rules import InsightArtifact
from scripts.seed_rules_templates import seed_rules_and_templates

USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
HDR_A = {"X-User-Id": USER_A}
HDR_B = {"X-User-Id": USER_B}
MEM_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _t2dm_payload(onset: str = "55_59", certainty: str = "confirmed") -> dict:
    return {
        "members": [
            {
                "slot": "mother",
                "vital_status": "alive",
                "conditions": [
                    {
                        "condition_code": "T2DM",
                        "condition_display": "type 2 diabetes",
                        "onset_band": onset,
                        "certainty": certainty,
                        "provenance": "self_report",
                    }
                ],
            }
        ]
    }


# --------------------------------------------------------------------------- #
# PUT /pedigree — payload validation (422)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_put_invalid_slot_422(client):
    payload = _t2dm_payload()
    payload["members"][0]["slot"] = "sister"
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_invalid_onset_band_422(client):
    # Hyphenated instead of the underscore literal.
    payload = _t2dm_payload(onset="55-59")
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_invalid_certainty_422(client):
    payload = _t2dm_payload(certainty="definitely")
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_invalid_provenance_422(client):
    payload = _t2dm_payload()
    payload["members"][0]["conditions"][0]["provenance"] = "hearsay"
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_condition_code_length_boundary(client):
    ok = _t2dm_payload()
    ok["members"][0]["conditions"][0]["condition_code"] = "C" * 32
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=ok)
    assert resp.status_code == 200

    too_long = _t2dm_payload()
    too_long["members"][0]["conditions"][0]["condition_code"] = "C" * 33
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=too_long)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_condition_display_too_long_422(client):
    payload = _t2dm_payload()
    payload["members"][0]["conditions"][0]["condition_display"] = "d" * 129
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_empty_members_200_and_no_insights(client):
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json={"members": []})
    assert resp.status_code == 200
    assert resp.json()["insights_created"] == 0
    insights = (await client.get("/api/v1/insights", headers=HDR_A)).json()
    assert insights == []


@pytest.mark.asyncio
async def test_put_unicode_condition_display_roundtrip(client):
    display = "मधुमेह प्रकार २ (type 2)"
    payload = _t2dm_payload()
    payload["members"][0]["conditions"][0]["condition_display"] = display
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 200
    ped = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert ped["conditions"][0]["condition_display"] == display


# --------------------------------------------------------------------------- #
# POST /chat — message field boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chat_empty_message_422(client):
    resp = await client.post("/api/v1/chat", headers=HDR_A, json={"message": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_missing_message_422(client):
    resp = await client.post("/api/v1/chat", headers=HDR_A, json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_message_at_4000_chars_200(client):
    base = "what helps blood pressure "
    message = base + "a" * (4000 - len(base))
    assert len(message) == 4000
    resp = await client.post("/api/v1/chat", headers=HDR_A, json={"message": message})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_message"]
    assert body["session_id"]


@pytest.mark.asyncio
async def test_chat_message_4001_chars_422(client):
    resp = await client.post(
        "/api/v1/chat", headers=HDR_A, json={"message": "a" * 4001}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_unicode_message_200(client):
    resp = await client.post(
        "/api/v1/chat",
        headers=HDR_A,
        json={"message": "मुझे मधुमेह के बारे में बताएं 🙏"},
    )
    assert resp.status_code == 200
    assert resp.json()["response_message"]


# --------------------------------------------------------------------------- #
# GET /pedigree + DELETE — member fields, soft delete, 404/403
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_pedigree_member_fields_and_soft_delete_exclusion(client):
    payload = _t2dm_payload()
    payload["members"][0]["vital_status"] = "deceased"
    payload["members"][0]["cause_of_death"] = "stroke"
    payload["members"].append({"slot": "father", "vital_status": "alive"})
    resp = await client.put("/api/v1/pedigree", headers=HDR_A, json=payload)
    assert resp.status_code == 200

    ped = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    by_slot = {m["slot"]: m for m in ped["members"]}
    assert by_slot["mother"]["vital_status"] == "deceased"
    assert by_slot["mother"]["cause_of_death"] == "stroke"
    assert by_slot["father"]["vital_status"] == "alive"
    assert by_slot["father"]["cause_of_death"] is None
    assert len(ped["conditions"]) == 1
    cond = ped["conditions"][0]
    assert cond["slot"] == "mother"
    assert cond["condition_code"] == "T2DM"
    assert cond["onset_band"] == "55_59"
    assert cond["certainty"] == "confirmed"
    assert cond["provenance"] == "self_report"

    dele = await client.delete(
        f"/api/v1/pedigree/conditions/{cond['id']}", headers=HDR_A
    )
    assert dele.status_code == 200

    ped2 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert ped2["conditions"] == []
    # Members are untouched by a condition soft delete.
    assert {m["slot"] for m in ped2["members"]} == {"mother", "father"}


@pytest.mark.asyncio
async def test_delete_nonexistent_condition_404(client):
    missing = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/pedigree/conditions/{missing}", headers=HDR_A
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_other_users_condition_403(client):
    await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    ped = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    cond_id = ped["conditions"][0]["id"]

    resp = await client.delete(
        f"/api/v1/pedigree/conditions/{cond_id}", headers=HDR_B
    )
    assert resp.status_code == 403

    # The condition is still visible to its owner (delete did not happen).
    ped2 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert len(ped2["conditions"]) == 1


# --------------------------------------------------------------------------- #
# Upsert semantics — double PUT is a single row; resurrect after delete
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_double_put_same_condition_upserts_single_row(client):
    await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    ped1 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert len(ped1["conditions"]) == 1
    first_id = ped1["conditions"][0]["id"]

    await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    ped2 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert len(ped2["conditions"]) == 1
    assert ped2["conditions"][0]["id"] == first_id

    # A changed field updates the same row in place.
    await client.put(
        "/api/v1/pedigree",
        headers=HDR_A,
        json=_t2dm_payload(certainty="as_far_as_i_know"),
    )
    ped3 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert len(ped3["conditions"]) == 1
    assert ped3["conditions"][0]["id"] == first_id
    assert ped3["conditions"][0]["certainty"] == "as_far_as_i_know"


@pytest.mark.asyncio
async def test_readd_soft_deleted_condition_resurrects(client, db_session):
    await seed_rules_and_templates(db_session)
    await db_session.commit()

    put1 = await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    assert put1.json()["insights_created"] == 1
    ped = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    cond_id = ped["conditions"][0]["id"]

    dele = await client.delete(
        f"/api/v1/pedigree/conditions/{cond_id}", headers=HDR_A
    )
    assert dele.status_code == 200
    assert (await client.get("/api/v1/insights", headers=HDR_A)).json() == []

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.id == uuid.UUID(cond_id)
            )
        )
    ).scalars().one()
    assert row.soft_deleted is True
    assert row.soft_deleted_at is not None

    # Re-adding the same condition resurrects the SAME row.
    put2 = await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    assert put2.json()["insights_created"] == 1

    ped2 = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    assert len(ped2["conditions"]) == 1
    assert ped2["conditions"][0]["id"] == cond_id

    db_session.expire_all()
    row2 = (
        await db_session.execute(
            select(PedigreeCondition).where(
                PedigreeCondition.id == uuid.UUID(cond_id)
            )
        )
    ).scalars().one()
    assert row2.soft_deleted is False
    assert row2.soft_deleted_at is None

    insights = (await client.get("/api/v1/insights", headers=HDR_A)).json()
    assert len(insights) == 1
    assert insights[0]["condition_code"] == "T2DM"


# --------------------------------------------------------------------------- #
# Header auth edge cases
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_garbage_x_user_id_400(client):
    resp = await client.get(
        "/api/v1/pedigree", headers={"X-User-Id": "not-a-uuid"}
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/api/v1/chat",
        headers={"X-User-Id": "definitely garbage"},
        json={"message": "hello"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_empty_x_user_id_falls_back_to_dev_user(client):
    # Empty header value is falsy → the fixed dev identity is used.
    resp = await client.get("/api/v1/pedigree", headers={"X-User-Id": ""})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------- #
# Consent ledger — append-only, one grant per user
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_two_puts_create_exactly_one_consent_grant(client, db_session):
    await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    await client.put(
        "/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload(onset="60_64")
    )

    db_session.expire_all()
    grants = (
        await db_session.execute(
            select(ConsentLedger).where(
                ConsentLedger.user_id == uuid.UUID(USER_A)
            )
        )
    ).scalars().all()
    assert len(grants) == 1
    assert grants[0].purpose == "family_risk_analysis"
    assert grants[0].action == "granted"
    assert grants[0].source == "api_put_pedigree"


# --------------------------------------------------------------------------- #
# Artifact supersession chain and retraction
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_artifact_supersede_chain_on_changed_onset(client, db_session):
    await seed_rules_and_templates(db_session)
    await db_session.commit()

    put1 = await client.put(
        "/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload(onset="55_59")
    )
    assert put1.json()["insights_created"] == 1
    ins1 = (await client.get("/api/v1/insights", headers=HDR_A)).json()
    assert len(ins1) == 1
    hash1 = ins1[0]["content_hash"]

    put2 = await client.put(
        "/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload(onset="40_44")
    )
    assert put2.json()["insights_created"] == 1
    ins2 = (await client.get("/api/v1/insights", headers=HDR_A)).json()
    assert len(ins2) == 1
    hash2 = ins2[0]["content_hash"]
    assert hash2 != hash1

    db_session.expire_all()
    artifacts = (
        await db_session.execute(
            select(InsightArtifact).where(
                InsightArtifact.user_id == uuid.UUID(USER_A)
            )
        )
    ).scalars().all()
    assert len(artifacts) == 2
    by_status = {a.status: a for a in artifacts}
    assert set(by_status) == {"active", "superseded"}
    old, new = by_status["superseded"], by_status["active"]
    assert old.content_hash == hash1
    assert new.content_hash == hash2
    assert old.superseded_by == new.id
    assert new.superseded_by is None


@pytest.mark.asyncio
async def test_retraction_leaves_no_active_artifacts(client, db_session):
    await seed_rules_and_templates(db_session)
    await db_session.commit()

    await client.put("/api/v1/pedigree", headers=HDR_A, json=_t2dm_payload())
    ped = (await client.get("/api/v1/pedigree", headers=HDR_A)).json()
    cond_id = ped["conditions"][0]["id"]

    dele = await client.delete(
        f"/api/v1/pedigree/conditions/{cond_id}", headers=HDR_A
    )
    assert dele.status_code == 200
    assert (await client.get("/api/v1/insights", headers=HDR_A)).json() == []

    db_session.expire_all()
    artifacts = (
        await db_session.execute(
            select(InsightArtifact).where(
                InsightArtifact.user_id == uuid.UUID(USER_A)
            )
        )
    ).scalars().all()
    assert len(artifacts) == 1
    assert artifacts[0].status == "superseded"
    # Retraction supersedes without a replacement.
    assert artifacts[0].superseded_by is None


# --------------------------------------------------------------------------- #
# Memory — adversarial medication extraction
# --------------------------------------------------------------------------- #
def test_walked_500_m_is_not_a_medication():
    assert extract_medications("i walked 500 m") == []


def test_take_2_tablets_is_not_a_medication():
    assert extract_medications("take 2 tablets") == []


def test_stopword_names_are_rejected():
    assert extract_medications("took 5 mg this morning") == []
    assert extract_medications("roughly 10 mg every day") == []


def test_no_space_dose_is_still_captured():
    # The unit separator is optional (\s?), so "500mg" matches and is
    # normalized with a space.
    assert extract_medications("metformin 500mg") == ["metformin 500 mg"]


def test_case_is_normalized_to_lowercase():
    assert extract_medications("Metformin 500 MG") == ["metformin 500 mg"]


def test_insulin_units_and_singular_unit():
    assert extract_medications("insulin 10 units") == ["insulin 10 units"]
    assert extract_medications("insulin 10 unit") == ["insulin 10 unit"]


def test_hyphenated_name_and_decimal_dose():
    assert extract_medications("co-amoxiclav 625 mg") == ["co-amoxiclav 625 mg"]
    assert extract_medications("levothyroxine 0.5 mg") == ["levothyroxine 0.5 mg"]


def test_name_length_boundary():
    # The drug-name slot needs at least four characters.
    assert extract_medications("abc 5 mg") == []
    assert extract_medications("abcd 5 mg") == ["abcd 5 mg"]


def test_non_ascii_drug_name_is_not_matched():
    # [a-z] does not match accented letters; no partial match either.
    assert extract_medications("metformín 500 mg") == []


def test_dedup_across_repeated_mentions():
    text = "metformin 500 mg in the morning and metformin 500 mg at night"
    assert extract_medications(text) == ["metformin 500 mg"]


def test_distinct_doses_are_distinct_entries():
    text = "metformin 500 mg then metformin 850 mg"
    assert extract_medications(text) == ["metformin 500 mg", "metformin 850 mg"]


# --------------------------------------------------------------------------- #
# Memory — compact_messages role rules and caps
# --------------------------------------------------------------------------- #
def test_open_questions_only_from_user_questions():
    messages = [
        {"role": "assistant", "message": "did you sleep well?"},
        {"role": "user", "message": "yes I slept fine."},
        {"role": "user", "message": "what about my sugar level?"},
    ]
    s = compact_messages(messages)
    assert s["open_questions"] == ["what about my sugar level?"]


def test_boundaries_only_from_assistant():
    messages = [
        {"role": "user", "message": "I'm not a doctor but I have a question"},
        {"role": "assistant", "message": "I'm not a doctor and I don't diagnose."},
    ]
    s = compact_messages(messages)
    assert s["boundaries"] == ["I'm not a doctor and I don't diagnose."]


def test_open_question_snippet_truncated_to_120():
    q = "why " + "x" * 200 + "?"
    s = compact_messages([{"role": "user", "message": q}])
    assert len(s["open_questions"]) == 1
    assert len(s["open_questions"][0]) == 120


def test_boundary_snippet_truncated_to_120():
    text = "I can only help with health " + "y" * 200
    s = compact_messages([{"role": "assistant", "message": text}])
    assert len(s["boundaries"]) == 1
    assert len(s["boundaries"][0]) == 120


# Fifteen distinct real phrases from the triage vocabulary, one per message.
_FLAG_SENTENCES: dict[str, str] = {
    "passed out": "my father passed out",
    "not breathing": "he is not breathing",
    "choking": "she is choking",
    "gasping": "he was gasping",
    "seizure": "he had a seizure",
    "convulsion": "a convulsion happened",
    "cardiac arrest": "cardiac arrest suspected",
    "no pulse": "there is no pulse",
    "face drooping": "his face drooping now",
    "slurred speech": "slurred speech since noon",
    "vomiting blood": "he is vomiting blood",
    "coughing up blood": "coughing up blood today",
    "blood in stool": "there is blood in stool",
    "severe confusion": "severe confusion tonight",
    "blue lips": "he has blue lips",
}


def test_flags_are_unbounded_beyond_cap():
    messages = [
        {"role": "user", "message": text} for text in _FLAG_SENTENCES.values()
    ]
    s = compact_messages(messages)
    assert len(_FLAG_SENTENCES) == 15
    assert set(s["flags"]) == set(_FLAG_SENTENCES)
    assert len(s["flags"]) == 15 > CAP


def test_medications_are_unbounded_beyond_cap():
    names = [f"drug{c}{c}" for c in "abcdefghijklmno"]  # 15 distinct names
    messages = [
        {"role": "user", "message": f"i take {n} 5 mg"} for n in names
    ]
    s = compact_messages(messages)
    assert s["medications"] == [f"{n} 5 mg" for n in names]
    assert len(s["medications"]) == 15 > CAP


def test_open_questions_capped_at_12():
    messages = [
        {"role": "user", "message": f"question number {i}, is that ok?"}
        for i in range(15)
    ]
    s = compact_messages(messages)
    assert len(s["open_questions"]) == CAP
    assert s["open_questions"][0] == "question number 0, is that ok?"
    assert "question number 14, is that ok?" not in s["open_questions"]


def test_merge_caps_topics_at_12():
    old = {"topics": [f"t{i}" for i in range(10)]}
    new = {"topics": [f"t{i}" for i in range(10, 20)]}
    merged = merge_summaries(old, new)
    assert merged["topics"] == [f"t{i}" for i in range(CAP)]


def test_merge_sticky_union_preserves_first_mention_order():
    old = {
        "flags": ["z", "a"],
        "medications": ["m2", "m1"],
        "boundaries": ["b1"],
        "timeline": ["z", "m2"],
    }
    new = {
        "flags": ["a", "b"],
        "medications": ["m1", "m3"],
        "boundaries": ["b2", "b1"],
        "timeline": ["m2", "q"],
    }
    merged = merge_summaries(old, new)
    assert merged["flags"] == ["z", "a", "b"]
    assert merged["medications"] == ["m2", "m1", "m3"]
    assert merged["boundaries"] == ["b1", "b2"]
    assert merged["timeline"] == ["z", "m2", "q"]


# --------------------------------------------------------------------------- #
# Compaction integration — thresholds, two passes, fail-open
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_compaction_threshold_boundary(db_session):
    session_id = await ensure_session(db_session, MEM_USER, None)
    msgs = []
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(
            await add_message(db_session, session_id, role, f"boundary msg {i}.")
        )
    # Exactly at the threshold: no compaction yet.
    assert await maybe_compact(db_session, session_id) is None

    msgs.append(await add_message(db_session, session_id, "user", "one more."))
    merged = await maybe_compact(db_session, session_id)
    assert merged is not None
    row = await latest_summary(db_session, session_id)
    assert row is not None
    assert row.version == 1
    # 21 uncompacted − 8 verbatim = the first 13 messages were folded.
    assert row.covers_through_message_id == msgs[12].id


@pytest.mark.asyncio
async def test_two_compaction_passes_sticky_flag_survives(db_session):
    session_id = await ensure_session(db_session, MEM_USER, None)
    await add_message(db_session, session_id, "user", "I passed out yesterday")
    for i in range(44):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(
            db_session, session_id, role, f"routine filler number {i}."
        )

    merged1 = await maybe_compact(db_session, session_id)
    await db_session.commit()
    assert merged1 is not None
    assert "passed out" in merged1["flags"]

    row1 = await latest_summary(db_session, session_id)
    assert row1 is not None
    assert row1.version == 1
    covers1 = row1.covers_through_message_id
    assert covers1 is not None
    assert row1.token_estimate > 0

    # Second batch: a new flag lands inside the region the next pass folds.
    new_msgs = [
        await add_message(db_session, session_id, "user", "now he is vomiting blood")
    ]
    for i in range(14):
        role = "assistant" if i % 2 == 0 else "user"
        new_msgs.append(
            await add_message(
                db_session, session_id, role, f"second batch filler {i}."
            )
        )

    merged2 = await maybe_compact(db_session, session_id)
    await db_session.commit()
    assert merged2 is not None
    # The early flag sticks across passes; the new one is merged in.
    assert "passed out" in merged2["flags"]
    assert "vomiting blood" in merged2["flags"]

    row2 = await latest_summary(db_session, session_id)
    assert row2 is not None
    assert row2.version == 2
    # 8 old verbatim + first 7 new messages were folded this pass.
    assert row2.covers_through_message_id == new_msgs[6].id
    assert row2.covers_through_message_id != covers1

    all_rows = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().all()
    assert len(all_rows) == 2


@pytest.mark.asyncio
async def test_maybe_compact_returns_none_on_db_error(db_session, monkeypatch):
    session_id = await ensure_session(db_session, MEM_USER, None)
    for i in range(25):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(db_session, session_id, role, f"msg {i}")

    async def boom(db, sid):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(conversation_mod, "latest_summary", boom)
    # Fail-open: compaction must never raise.
    assert await maybe_compact(db_session, session_id) is None

    rows = (
        await db_session.execute(
            select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
        )
    ).scalars().all()
    assert rows == []

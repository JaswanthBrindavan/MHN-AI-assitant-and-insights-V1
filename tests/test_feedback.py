"""Feedback capture, and the path from a down-vote to a regression case."""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from app.auth import DEV_USER_ID
from app.models.chat import ConversationMessage, ConversationSession
from app.models.feedback import TurnFeedback
from app.telemetry import feedback_received, reset_all


async def _turn(
    sessionmaker, user_id: uuid.UUID = DEV_USER_ID, question: str = "why am I so tired?"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A user question followed by an assistant reply. Returns (session, reply)."""
    async with sessionmaker() as db:
        session = ConversationSession(user_id=user_id)
        db.add(session)
        await db.flush()
        db.add(
            ConversationMessage(session_id=session.id, role="user", message=question)
        )
        await db.flush()
        reply = ConversationMessage(
            session_id=session.id,
            role="assistant",
            message="Tiredness has many causes.",
        )
        db.add(reply)
        await db.flush()
        await db.commit()
        return session.id, reply.id


async def test_down_vote_is_recorded(client, sessionmaker):
    _, reply_id = await _turn(sessionmaker)

    response = await client.post(
        "/api/v1/feedback",
        json={"message_id": str(reply_id), "rating": "down", "reason": "unhelpful"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rating"] == "down"

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(TurnFeedback))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == "unhelpful"
    assert rows[0].message_id == reply_id


async def test_second_vote_corrects_rather_than_duplicates(client, sessionmaker):
    """A reader who changes their mind must not be counted twice.

    Double-counting would skew the very numbers this exists to produce.
    """
    _, reply_id = await _turn(sessionmaker)

    first = await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "down"}
    )
    second = await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "up"}
    )
    assert first.status_code == 200 and second.status_code == 200

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(TurnFeedback))).scalars().all()
    assert len(rows) == 1, "a correction must not create a second row"
    assert rows[0].rating == "up"


async def test_feedback_on_someone_elses_turn_is_refused(client, sessionmaker):
    """Only the session's owner may judge its turns."""
    _, reply_id = await _turn(sessionmaker, user_id=uuid.uuid4())

    response = await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "down"}
    )
    assert response.status_code == 403

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(TurnFeedback))).scalars().all()
    assert rows == []


async def test_feedback_on_a_user_message_is_refused(client, sessionmaker):
    """You judge the assistant, not yourself."""
    session_id, _ = await _turn(sessionmaker)
    async with sessionmaker() as db:
        question = (
            await db.execute(
                sa.select(ConversationMessage).where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.role == "user",
                )
            )
        ).scalars().first()
    assert question is not None

    response = await client.post(
        "/api/v1/feedback", json={"message_id": str(question.id), "rating": "down"}
    )
    assert response.status_code == 400


async def test_unknown_message_is_not_silently_accepted(client):
    response = await client.post(
        "/api/v1/feedback", json={"message_id": str(uuid.uuid4()), "rating": "down"}
    )
    assert response.status_code == 404


async def test_closed_sets_are_enforced(client, sessionmaker):
    """rating and reason are bounded so the counters stay countable."""
    _, reply_id = await _turn(sessionmaker)

    bad_rating = await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "sideways"}
    )
    bad_reason = await client.post(
        "/api/v1/feedback",
        json={"message_id": str(reply_id), "rating": "down", "reason": "vibes"},
    )
    assert bad_rating.status_code == 422
    assert bad_reason.status_code == 422

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(TurnFeedback))).scalars().all()
    assert rows == []


async def test_review_queue_carries_the_question_and_the_reply(client, sessionmaker):
    """A down-vote without the turn beside it cannot be acted on."""
    _, reply_id = await _turn(sessionmaker)
    await client.post(
        "/api/v1/feedback",
        json={
            "message_id": str(reply_id),
            "rating": "down",
            "reason": "wrong",
            "comment": "that did not answer what I asked",
        },
    )

    response = await client.get("/api/v1/feedback/review")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["question"] == "why am I so tired?"
    assert items[0]["reply"] == "Tiredness has many causes."
    assert items[0]["comment"] == "that did not answer what I asked"
    assert items[0]["triaged"] is False


async def test_up_votes_stay_out_of_the_review_queue(client, sessionmaker):
    _, reply_id = await _turn(sessionmaker)
    await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "up"}
    )
    assert (await client.get("/api/v1/feedback/review")).json() == []


async def test_triage_removes_it_from_the_queue(client, sessionmaker):
    _, reply_id = await _turn(sessionmaker)
    created = await client.post(
        "/api/v1/feedback", json={"message_id": str(reply_id), "rating": "down"}
    )
    feedback_id = created.json()["id"]

    assert len((await client.get("/api/v1/feedback/review")).json()) == 1
    triaged = await client.post(f"/api/v1/feedback/{feedback_id}/triage")
    assert triaged.status_code == 200
    assert (await client.get("/api/v1/feedback/review")).json() == []

    # Triaged, not erased — still there when explicitly asked for.
    all_items = (await client.get("/api/v1/feedback/review?untriaged_only=false")).json()
    assert len(all_items) == 1 and all_items[0]["triaged"] is True


async def test_triaging_someone_elses_feedback_is_refused(client, sessionmaker):
    """Knowing the row id must not be enough to touch it."""
    stranger = uuid.uuid4()
    _, reply_id = await _turn(sessionmaker, user_id=stranger)
    async with sessionmaker() as db:
        row = TurnFeedback(user_id=stranger, message_id=reply_id, rating="down")
        db.add(row)
        await db.commit()
        row_id = row.id

    response = await client.post(f"/api/v1/feedback/{row_id}/triage")
    assert response.status_code == 403

    async with sessionmaker() as db:
        refreshed = (
            await db.execute(sa.select(TurnFeedback).where(TurnFeedback.id == row_id))
        ).scalars().first()
    assert refreshed is not None and refreshed.triaged_at is None


async def test_review_queue_shows_only_the_callers_own_feedback(client, sessionmaker):
    """One reader's complaints are not another's to read."""
    stranger = uuid.uuid4()
    _, stranger_reply = await _turn(sessionmaker, user_id=stranger)
    async with sessionmaker() as db:
        db.add(
            TurnFeedback(
                user_id=stranger,
                message_id=stranger_reply,
                rating="down",
                comment="private complaint",
            )
        )
        await db.commit()

    items = (await client.get("/api/v1/feedback/review")).json()
    assert items == []


async def test_summary_counts_by_rating_and_reason(client, sessionmaker):
    async with sessionmaker() as db:
        session = ConversationSession(user_id=DEV_USER_ID)
        db.add(session)
        await db.flush()
        replies = []
        for index in range(3):
            reply = ConversationMessage(
                session_id=session.id, role="assistant", message=f"reply {index}"
            )
            db.add(reply)
            await db.flush()
            replies.append(reply.id)
        await db.commit()

    for index, reply_id in enumerate(replies):
        await client.post(
            "/api/v1/feedback",
            json={
                "message_id": str(reply_id),
                "rating": "down" if index else "up",
                "reason": "wrong" if index else None,
            },
        )

    summary = (await client.get("/api/v1/feedback/summary")).json()
    assert summary["by_rating"] == {"up": 1, "down": 2}
    assert summary["by_reason"] == {"wrong": 2}


async def test_the_counter_is_registered_so_metrics_actually_show_it(
    client, sessionmaker
):
    """A metric declared outside telemetry._ALL renders NOWHERE.

    That is exactly how this counter was first written — the registry is a
    hand-maintained tuple, so a new metric is invisible until added to it.
    """
    reset_all()
    _, reply_id = await _turn(sessionmaker)
    await client.post(
        "/api/v1/feedback",
        json={"message_id": str(reply_id), "rating": "down", "reason": "unsafe"},
    )

    assert feedback_received.values, "the counter recorded nothing"
    body = (await client.get("/metrics")).text
    assert "davi_feedback_total" in body
    assert 'rating="down"' in body and 'reason="unsafe"' in body


def test_promotion_writes_a_case_without_the_bad_reply(tmp_path, monkeypatch):
    """Promoting must NOT freeze the down-voted answer as the expectation.

    The reply was judged wrong. Writing it into `scripted` would enshrine the
    defect as the very thing the suite protects.
    """
    from scripts import promote_feedback

    cases = tmp_path / "quality_cases.json"
    cases.write_text(
        json.dumps({"description": "d", "cases": [{"name": "existing"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(promote_feedback, "CASES_PATH", cases)

    case = promote_feedback.add_case(
        "why does my head hurt every morning?", "head_hurt", reason="wrong"
    )
    written = json.loads(cases.read_text(encoding="utf-8"))
    assert len(written["cases"]) == 2
    assert case["message"] == "why does my head hurt every morning?"
    assert "scripted" not in case
    assert "head" in case["addresses"]


def test_promotion_never_collides_a_case_name(tmp_path, monkeypatch):
    """quality_eval keys its report by name — a duplicate would hide a result."""
    from scripts import promote_feedback

    cases = tmp_path / "quality_cases.json"
    cases.write_text(
        json.dumps({"description": "d", "cases": [{"name": "tired"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(promote_feedback, "CASES_PATH", cases)

    first = promote_feedback.add_case("q one about sleeping", "tired", reason=None)
    second = promote_feedback.add_case("q two about walking", "tired", reason=None)
    assert first["name"] == "tired_2"
    assert second["name"] == "tired_3"

    names = [c["name"] for c in json.loads(cases.read_text(encoding="utf-8"))["cases"]]
    assert len(names) == len(set(names))


def test_a_promoted_case_is_one_the_quality_harness_can_actually_score(
    tmp_path, monkeypatch
):
    """The promoter's output must satisfy the harness's case contract.

    Writing a case the harness then KeyErrors on would make the loop look
    closed while breaking the suite it feeds.
    """
    from types import SimpleNamespace

    from scripts import promote_feedback, quality_eval

    cases = tmp_path / "quality_cases.json"
    cases.write_text(json.dumps({"description": "d", "cases": []}), encoding="utf-8")
    monkeypatch.setattr(promote_feedback, "CASES_PATH", cases)
    case = promote_feedback.add_case("is 140 over 90 high?", "bp_case", reason="wrong")

    result = SimpleNamespace(
        response_message="Readings around 140 over 90 are worth reviewing with a "
        "clinician who can see the trend rather than one reading.",
        provenance={},
    )
    score = quality_eval.score_case(case, result, model_chooses=True)
    assert score.name == "bp_case"
    assert score.answered is True


def test_the_slug_survives_a_question_of_pure_stopwords(tmp_path, monkeypatch):
    """`_slug` must never return an empty name — the case would be unaddressable."""
    from scripts import promote_feedback

    assert promote_feedback._slug("is it?") == "feedback_case"
    assert promote_feedback._addresses("is it?") == ""

    cases = tmp_path / "quality_cases.json"
    cases.write_text(json.dumps({"description": "d", "cases": []}), encoding="utf-8")
    monkeypatch.setattr(promote_feedback, "CASES_PATH", cases)
    case = promote_feedback.add_case("is it?", promote_feedback._slug("is it?"), reason=None)
    assert case["name"] == "feedback_case"

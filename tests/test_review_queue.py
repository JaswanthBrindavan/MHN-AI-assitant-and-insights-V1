"""Clinician review of held insights.

This is the only surface in the repo where one person reads ANOTHER person's
health information, so the tests here are adversarial by design: most of them
try to get at data without standing, and fail.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.auth import DEV_USER_ID
from app.insights.engine import LIVE_STATUSES
from app.models.review import ClinicianReviewer, InsightReviewAudit
from app.models.rules import InsightArtifact

PATIENT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


async def _held_artifact(sessionmaker, user_id: uuid.UUID = PATIENT) -> uuid.UUID:
    async with sessionmaker() as db:
        artifact = InsightArtifact(
            user_id=user_id,
            condition_code="T2DM",
            tier="elevated",
            title="Family history of type 2 diabetes",
            body="Two first-degree relatives with early onset. Not a diagnosis.",
            facts_used={"affected_first_degree": 2},
            fired_rules=["two_first_degree_early"],
            template_key="fh_t2dm",
            template_version=1,
            pipeline_version=1,
            content_hash="a" * 64,
            status="held_for_review",
        )
        db.add(artifact)
        await db.commit()
        return artifact.id


async def _make_reviewer(sessionmaker, user_id: uuid.UUID = DEV_USER_ID, **kwargs):
    async with sessionmaker() as db:
        db.add(
            ClinicianReviewer(
                user_id=user_id, display_name="Dr Test", **kwargs
            )
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Standing
# --------------------------------------------------------------------------- #
async def test_a_non_reviewer_cannot_see_the_queue(client, sessionmaker):
    """The default is NO access. Holding a valid token is not standing."""
    await _held_artifact(sessionmaker)
    response = await client.get("/api/v1/review/queue")
    assert response.status_code == 403


async def test_a_revoked_reviewer_loses_access_immediately(client, sessionmaker):
    """Revocation must bite on the next request, not on token expiry."""
    await _make_reviewer(sessionmaker)
    assert (await client.get("/api/v1/review/queue")).status_code == 200

    async with sessionmaker() as db:
        row = (
            await db.execute(
                sa.select(ClinicianReviewer).where(
                    ClinicianReviewer.user_id == DEV_USER_ID
                )
            )
        ).scalars().first()
        assert row is not None
        row.active = False
        await db.commit()

    assert (await client.get("/api/v1/review/queue")).status_code == 403


async def test_a_non_reviewer_cannot_open_an_artifact_by_id(client, sessionmaker):
    """Knowing the id must not be enough. This is the whole point."""
    artifact_id = await _held_artifact(sessionmaker)
    response = await client.get(f"/api/v1/review/artifacts/{artifact_id}")
    assert response.status_code == 403


async def test_a_non_reviewer_cannot_decide(client, sessionmaker):
    artifact_id = await _held_artifact(sessionmaker)
    response = await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/release", json={"note": "fine"}
    )
    assert response.status_code == 403

    async with sessionmaker() as db:
        artifact = await db.get(InsightArtifact, artifact_id)
        assert artifact is not None and artifact.status == "held_for_review"


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #
async def test_the_queue_does_not_disclose_the_body(client, sessionmaker):
    """A listing that includes every body makes the audited `view` pointless."""
    await _make_reviewer(sessionmaker)
    await _held_artifact(sessionmaker)

    items = (await client.get("/api/v1/review/queue")).json()
    assert len(items) == 1
    assert "body" not in items[0]
    assert "Two first-degree relatives" not in str(items[0])


async def test_only_held_artifacts_are_queued(client, sessionmaker):
    """An active insight is the patient's; it is not review material."""
    await _make_reviewer(sessionmaker)
    async with sessionmaker() as db:
        db.add(
            InsightArtifact(
                user_id=PATIENT, condition_code="HTN", tier="general",
                title="Active one", body="Already visible to the patient.",
                template_key="fh_htn", template_version=1, pipeline_version=1,
                content_hash="b" * 64, status="active",
            )
        )
        await db.commit()
    await _held_artifact(sessionmaker)

    items = (await client.get("/api/v1/review/queue")).json()
    assert [i["condition_code"] for i in items] == ["T2DM"]


async def test_the_queue_is_oldest_first(client, sessionmaker):
    """Newest-first buries the item that has waited longest."""
    await _make_reviewer(sessionmaker)
    first = await _held_artifact(sessionmaker)
    async with sessionmaker() as db:
        db.add(
            InsightArtifact(
                user_id=PATIENT, condition_code="CAD", tier="elevated",
                title="Later", body="Body.", template_key="fh_cad",
                template_version=1, pipeline_version=1, content_hash="c" * 64,
                status="held_for_review",
            )
        )
        await db.commit()

    items = (await client.get("/api/v1/review/queue")).json()
    assert items[0]["id"] == str(first)


# --------------------------------------------------------------------------- #
# Viewing
# --------------------------------------------------------------------------- #
async def test_viewing_returns_the_basis_for_the_decision(client, sessionmaker):
    """A reviewer cannot judge an insight without seeing what produced it."""
    await _make_reviewer(sessionmaker)
    artifact_id = await _held_artifact(sessionmaker)

    detail = (await client.get(f"/api/v1/review/artifacts/{artifact_id}")).json()
    assert "Two first-degree relatives" in detail["body"]
    assert detail["facts_used"] == {"affected_first_degree": 2}
    assert detail["fired_rules"] == ["two_first_degree_early"]


async def test_review_standing_is_not_a_licence_to_read_any_insight(
    client, sessionmaker
):
    """Being a reviewer grants access to HELD artifacts, and nothing more."""
    await _make_reviewer(sessionmaker)
    async with sessionmaker() as db:
        active = InsightArtifact(
            user_id=PATIENT, condition_code="HTN", tier="general",
            title="Private", body="The patient's own active insight.",
            template_key="fh_htn", template_version=1, pipeline_version=1,
            content_hash="d" * 64, status="active",
        )
        db.add(active)
        await db.commit()
        active_id = active.id

    response = await client.get(f"/api/v1/review/artifacts/{active_id}")
    assert response.status_code == 403


async def test_every_view_is_audited_with_the_subject(client, sessionmaker):
    """Seeing the content IS the disclosure. It must be on the record."""
    await _make_reviewer(sessionmaker)
    artifact_id = await _held_artifact(sessionmaker)
    await client.get(f"/api/v1/review/artifacts/{artifact_id}")

    async with sessionmaker() as db:
        rows = (
            await db.execute(
                sa.select(InsightReviewAudit).where(
                    InsightReviewAudit.action == "view"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].reviewer_user_id == DEV_USER_ID
    assert rows[0].subject_user_id == PATIENT
    assert rows[0].artifact_id == artifact_id
    assert rows[0].content_hash == "a" * 64


async def test_a_refused_view_leaves_no_audit_row(client, sessionmaker):
    """Nothing was disclosed, so nothing is recorded as disclosed."""
    artifact_id = await _held_artifact(sessionmaker)
    await client.get(f"/api/v1/review/artifacts/{artifact_id}")

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(InsightReviewAudit))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
# The reviewer and the patient are DIFFERENT people in these tests, which is
# the only realistic arrangement -- a clinician deciding on their own insight
# is refused (see test_a_reviewer_cannot_decide_on_their_own_insight).
CLINICIAN = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
AS_CLINICIAN = {"X-User-Id": str(CLINICIAN)}


async def test_release_makes_the_insight_reach_the_patient(client, sessionmaker):
    """The whole point: an insight that nobody could see now reaches someone."""
    await _make_reviewer(sessionmaker, user_id=CLINICIAN)
    artifact_id = await _held_artifact(sessionmaker, user_id=DEV_USER_ID)

    # Before: the patient's own endpoint shows nothing.
    assert (await client.get("/api/v1/insights")).json() == []

    decision = await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/release",
        json={"note": "Consistent with the family history; safe to show."},
        headers=AS_CLINICIAN,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "active"

    served = (await client.get("/api/v1/insights")).json()
    assert len(served) == 1 and served[0]["condition_code"] == "T2DM"


async def test_suppress_keeps_it_away_from_the_patient(client, sessionmaker):
    await _make_reviewer(sessionmaker, user_id=CLINICIAN)
    artifact_id = await _held_artifact(sessionmaker, user_id=DEV_USER_ID)

    decision = await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/suppress",
        json={"note": "Too easily misread as a diagnosis."},
        headers=AS_CLINICIAN,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "suppressed"
    assert (await client.get("/api/v1/insights")).json() == []


async def test_a_reviewer_cannot_decide_on_their_own_insight(client, sessionmaker):
    """Independent review is the entire premise of the roster.

    Without this, a clinician who is also a patient can self-approve, and the
    audit row shows reviewer and subject as the same person -- a record of the
    control not working.
    """
    await _make_reviewer(sessionmaker)  # DEV_USER_ID is the reviewer
    own = await _held_artifact(sessionmaker, user_id=DEV_USER_ID)

    for action in ("release", "suppress"):
        response = await client.post(
            f"/api/v1/review/artifacts/{own}/{action}", json={"note": "mine"}
        )
        assert response.status_code == 403, action

    async with sessionmaker() as db:
        artifact = await db.get(InsightArtifact, own)
    assert artifact is not None and artifact.status == "held_for_review"


async def test_a_decision_requires_a_stated_reason(client, sessionmaker):
    """'It was approved' is not a record of why it was approved."""
    await _make_reviewer(sessionmaker)
    artifact_id = await _held_artifact(sessionmaker)

    for payload in ({}, {"note": ""}):
        response = await client.post(
            f"/api/v1/review/artifacts/{artifact_id}/release", json=payload
        )
        assert response.status_code == 422


async def test_an_artifact_cannot_be_decided_twice(client, sessionmaker):
    """The second decision would overwrite the first with no trace."""
    await _make_reviewer(sessionmaker)
    artifact_id = await _held_artifact(sessionmaker)

    first = await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/release", json={"note": "ok"}
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/suppress", json={"note": "changed"}
    )
    assert second.status_code == 409


async def test_a_decision_records_the_hash_of_what_was_decided(client, sessionmaker):
    """An insight can be recomputed. Without the hash, a release cannot be
    tied to the text that was actually released."""
    await _make_reviewer(sessionmaker)
    artifact_id = await _held_artifact(sessionmaker)
    await client.post(
        f"/api/v1/review/artifacts/{artifact_id}/release", json={"note": "fine"}
    )

    async with sessionmaker() as db:
        row = (
            await db.execute(
                sa.select(InsightReviewAudit).where(
                    InsightReviewAudit.action == "release"
                )
            )
        ).scalars().first()
    assert row is not None
    assert row.content_hash == "a" * 64
    assert row.note == "fine"


# --------------------------------------------------------------------------- #
# The audit trail
# --------------------------------------------------------------------------- #
async def test_a_patient_can_ask_who_looked_at_their_own_records(
    client, sessionmaker
):
    """The half that makes the audit mean something to the person it protects."""
    await _make_reviewer(sessionmaker, user_id=uuid.uuid4())
    artifact_id = await _held_artifact(sessionmaker, user_id=DEV_USER_ID)

    # A reviewer (somebody else) views it.
    async with sessionmaker() as db:
        reviewer = (
            await db.execute(sa.select(ClinicianReviewer))
        ).scalars().first()
        assert reviewer is not None
        db.add(
            InsightReviewAudit(
                reviewer_user_id=reviewer.user_id,
                subject_user_id=DEV_USER_ID,
                artifact_id=artifact_id,
                action="view",
            )
        )
        await db.commit()

    trail = (
        await client.get(f"/api/v1/review/audit?subject_user_id={DEV_USER_ID}")
    ).json()
    assert len(trail) == 1
    assert trail[0]["action"] == "view"


async def test_a_patient_cannot_read_somebody_elses_trail(client, sessionmaker):
    """Own records only — anything wider needs reviewer standing."""
    response = await client.get(f"/api/v1/review/audit?subject_user_id={PATIENT}")
    assert response.status_code == 403


async def test_an_unscoped_trail_request_needs_reviewer_standing(client):
    assert (await client.get("/api/v1/review/audit")).status_code == 403


# --------------------------------------------------------------------------- #
# The engine contract
# --------------------------------------------------------------------------- #
def test_suppressed_is_a_live_status():
    """Otherwise every recompute re-queues an insight already declined.

    The hash-supersede check only compares against LIVE statuses. Drop
    "suppressed" from that tuple and the nightly sweep raises the same
    declined insight for the same reviewer to decline again, forever.
    """
    assert "suppressed" in LIVE_STATUSES


@pytest.mark.parametrize("status_value", ["active", "held_for_review", "suppressed"])
def test_the_live_statuses_are_exactly_the_undecided_and_decided_ones(status_value):
    assert status_value in LIVE_STATUSES
    assert "superseded" not in LIVE_STATUSES

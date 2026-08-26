"""Profile endpoints.

The read and the erase are as important as the write: a store of self-reported
health details a reader cannot inspect or delete is not something this product
should have.
"""

from __future__ import annotations

HDR = {"X-User-Id": "44444444-4444-4444-4444-444444444444"}
OTHER = {"X-User-Id": "55555555-5555-5555-5555-555555555555"}


async def test_a_fresh_user_sees_an_empty_profile_and_no_consent(client):
    resp = await client.get("/api/v1/profile", headers=HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_consent"] is False
    assert body["chronic_conditions"] is None


async def test_writing_without_consent_is_403(client):
    resp = await client.patch(
        "/api/v1/profile", headers=HDR, json={"age_band": "30_44"}
    )
    assert resp.status_code == 403


async def test_the_full_consent_write_read_cycle(client):
    assert (await client.post("/api/v1/profile/consent", headers=HDR)).status_code == 200

    resp = await client.patch(
        "/api/v1/profile",
        headers=HDR,
        json={
            "age_band": "30_44",
            "chronic_conditions": ["asthma"],
            "communication_style": "plain",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["chronic_conditions"] == ["asthma"]

    read = await client.get("/api/v1/profile", headers=HDR)
    assert read.json()["age_band"] == "30_44"
    assert read.json()["has_consent"] is True


async def test_an_invalid_age_band_is_rejected_not_stored(client):
    """A bad enum must be a 422, not a string nobody notices until it reaches
    a prompt."""
    await client.post("/api/v1/profile/consent", headers=HDR)
    resp = await client.patch(
        "/api/v1/profile", headers=HDR, json={"age_band": "middle-aged-ish"}
    )
    assert resp.status_code == 422

    read = await client.get("/api/v1/profile", headers=HDR)
    assert read.json()["age_band"] is None


async def test_an_invalid_communication_style_is_rejected(client):
    await client.post("/api/v1/profile/consent", headers=HDR)
    resp = await client.patch(
        "/api/v1/profile", headers=HDR, json={"communication_style": "shouty"}
    )
    assert resp.status_code == 422


async def test_a_patch_does_not_clear_unmentioned_fields(client):
    await client.post("/api/v1/profile/consent", headers=HDR)
    await client.patch("/api/v1/profile", headers=HDR, json={"age_band": "45_59"})
    resp = await client.patch("/api/v1/profile", headers=HDR, json={"sex": "female"})
    body = resp.json()
    assert body["age_band"] == "45_59"
    assert body["sex"] == "female"


async def test_forget_me_schedules_an_erasure_and_keeps_consent(client):
    """Erasure is now SCHEDULED, not immediate.

    The rows survive the grace period so an accidental or coerced deletion can
    be withdrawn — but the assistant stops USING them at once, which is what
    makes the deferral honest rather than a delay. See
    tests/test_erasure.py for the suppression and the purge.
    """
    await client.post("/api/v1/profile/consent", headers=HDR)
    await client.patch(
        "/api/v1/profile", headers=HDR, json={"allergies": ["penicillin"]}
    )

    resp = await client.request("DELETE", "/api/v1/profile", headers=HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["scheduled_for"] > body["requested_at"]
    assert body["grace_days"] > 0

    # Still readable through the profile API during the grace period — that is
    # the point of a window you can cancel within — but the CHAT no longer
    # uses it (tests/test_erasure.py).
    read = await client.get("/api/v1/profile", headers=HDR)
    # "Forget what you know" is not "stop remembering".
    assert read.json()["has_consent"] is True


async def test_a_scheduled_erasure_can_be_cancelled(client):
    """The grace period is worthless if it cannot be used."""
    await client.post("/api/v1/profile/consent", headers=HDR)
    await client.post("/api/v1/profile/forget-me", headers=HDR)

    status_before = (await client.get("/api/v1/profile/forget-me", headers=HDR)).json()
    assert status_before["status"] == "pending"

    cancelled = await client.post("/api/v1/profile/forget-me/cancel", headers=HDR)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    after = (await client.get("/api/v1/profile/forget-me", headers=HDR)).json()
    assert after["status"] == "none"


async def test_cancelling_nothing_is_a_404(client):
    resp = await client.post("/api/v1/profile/forget-me/cancel", headers=HDR)
    assert resp.status_code == 404


async def test_requesting_twice_does_not_extend_the_deadline(client):
    """Someone anxious enough to ask twice must not get a longer window."""
    first = (await client.post("/api/v1/profile/forget-me", headers=HDR)).json()
    second = (await client.post("/api/v1/profile/forget-me", headers=HDR)).json()
    assert first["scheduled_for"] == second["scheduled_for"]


async def test_withdrawing_consent_erases_and_revokes(client):
    await client.post("/api/v1/profile/consent", headers=HDR)
    await client.patch(
        "/api/v1/profile", headers=HDR, json={"allergies": ["penicillin"]}
    )

    resp = await client.request("DELETE", "/api/v1/profile/consent", headers=HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_consent"] is False
    assert body["allergies"] is None

    # And writes are refused again afterwards.
    again = await client.patch(
        "/api/v1/profile", headers=HDR, json={"age_band": "18_29"}
    )
    assert again.status_code == 403


async def test_one_users_profile_is_invisible_to_another(client):
    await client.post("/api/v1/profile/consent", headers=HDR)
    await client.patch(
        "/api/v1/profile", headers=HDR, json={"allergies": ["penicillin"]}
    )

    other = await client.get("/api/v1/profile", headers=OTHER)
    assert other.json()["allergies"] is None
    assert other.json()["has_consent"] is False

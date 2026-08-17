"""Production-alignment tests (mhn-spring / mhn-ai / mhn-react contracts).

Pins the three integration contracts verified verbatim in the production
repos: HS512 session JWTs with a Base64-decoded shared secret, the mhn-ai
content.ai extraction envelope, and the family-consent semantics
(req_read/acc_read on the owner's side + per-file exclusions).
"""

from __future__ import annotations

import base64
import uuid

import pytest
from jose import jwt

from app.chat.data_handlers import _search_content_for_param
from app.config import get_settings
from app.coredata.service import (
    _collect_params,
    latest_documents,
    resolve_family_member,
)
from app.models.common import utcnow
from app.models.coredata import (
    FamilyConnect,
    FileAccessExclusion,
    Relation,
    Report,
)

VIEWER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OWNER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# The verbatim production envelope shape (mhn-ai assembly.build_content +
# tests/support/ai.py extraction_payload, enriched by normalization).
PROD_CONTENT = {
    "ai": {
        "schema_version": "2.1",
        "state": "complete",
        "document_id": 9302,
        "classification": {
            "section": "reports", "title": "Full Body Checkup", "confidence": 0.97,
        },
        "extraction": {
            "results": [
                {
                    "test_name": "Fasting Glucose", "value": "126",
                    "unit": "mg/dL", "reference_range": "70-99",
                    "observed_date": "2026-07-20",
                    "source_context": "Glucose, Fasting",
                    "value_numeric": 126.0, "abnormal_flag": "high",
                    "range_source": "report_range", "flagged_against": "70 - 99",
                },
                {
                    "test_name": "HbA1c (Glycated Hemoglobin)", "value": "6.1",
                    "unit": "%", "reference_range": "< 5.7",
                    "observed_date": "2026-07-20", "source_context": "HbA1c",
                    "value_numeric": 6.1, "abnormal_flag": "high",
                    "range_source": "report_range", "flagged_against": "<= 5.7",
                },
            ],
            "report_date": "2026-07-20",
            "patient_age": "45",
            "patient_gender": "Female",
        },
        "insights": None,
        "generated_at": "2026-07-20T10:00:00Z",
    }
}


# --------------------------------------------------------------------------- #
# Auth: HS512 + Base64-decoded secret (Spring JwtService semantics)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hs512_base64_secret_token_accepted(monkeypatch):
    from app.auth import get_current_user_id

    raw_key = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    b64_secret = base64.b64encode(raw_key).decode()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET", b64_secret)
    monkeypatch.setenv("JWT_ALGORITHM", "HS512")
    monkeypatch.setenv("JWT_SECRET_BASE64", "true")
    get_settings.cache_clear()

    user = uuid.uuid4()
    # Sign the way Spring does: HS512 over the base64-DECODED key bytes.
    token = jwt.encode({"sub": str(user)}, raw_key, algorithm="HS512")
    resolved = await get_current_user_id(
        authorization=f"Bearer {token}", x_user_id=None
    )
    assert resolved == user
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_service_token_path(monkeypatch):
    from app.auth import get_current_user_id

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SERVICE_TOKEN", "s" * 40)
    get_settings.cache_clear()

    user = uuid.uuid4()
    resolved = await get_current_user_id(
        authorization=f"Bearer {'s' * 40}", x_user_id=str(user)
    )
    assert resolved == user
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_wrong_service_token_rejected(monkeypatch):
    from fastapi import HTTPException

    from app.auth import get_current_user_id

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SERVICE_TOKEN", "s" * 40)
    get_settings.cache_clear()
    with pytest.raises(HTTPException):
        await get_current_user_id(
            authorization=f"Bearer {'x' * 40}", x_user_id=str(uuid.uuid4())
        )
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Extraction envelope: content.ai.extraction.results[] (test_name / numeric)
# --------------------------------------------------------------------------- #
def test_search_param_finds_prod_test_name():
    found = _search_content_for_param(
        PROD_CONTENT, ("hba1c", "glycated hemoglobin", "glycated haemoglobin")
    )
    assert found is not None
    value, unit = found
    assert value == 6.1 and unit == "%"


def test_search_param_prefers_value_numeric():
    found = _search_content_for_param(PROD_CONTENT, ("fasting glucose",))
    assert found == (126.0, "mg/dL")


def test_collect_params_carries_abnormal_flag():
    out: list = []
    _collect_params(PROD_CONTENT, out)
    names = {n for n, _v, _u in out}
    assert "Fasting Glucose" in names
    glucose = next(v for n, v, _u in out if n == "Fasting Glucose")
    assert "(high)" in glucose  # production's Python-computed flag surfaces


def test_legacy_demo_shape_still_works():
    legacy = {"tests": [{"name": "HbA1c", "value": "6.2", "unit": "%"}]}
    assert _search_content_for_param(legacy, ("hba1c",)) == (6.2, "%")


# --------------------------------------------------------------------------- #
# Consent: req_read/acc_read on the owner's side + per-file exclusions
# --------------------------------------------------------------------------- #
async def _link(db, *, viewer_is_requester: bool, **grants):
    db.add(Relation(id=77, name="Father", inverse="Child"))
    fc = FamilyConnect(
        requester_id=VIEWER if viewer_is_requester else OWNER,
        acceptor_id=OWNER if viewer_is_requester else VIEWER,
        accepted=True, relation_id=77, **grants,
    )
    db.add(fc)
    await db.flush()


@pytest.mark.asyncio
async def test_new_read_grant_columns_used(db_session):
    # Viewer sent the request; owner (acceptor) grants via acc_read.
    await _link(db_session, viewer_is_requester=True,
                acc_read=True, acc_file_share=False)
    assert await resolve_family_member(db_session, VIEWER, "father") == OWNER


@pytest.mark.asyncio
async def test_new_read_grant_denies(db_session):
    await _link(db_session, viewer_is_requester=True,
                acc_read=False, acc_file_share=True)  # new column wins
    assert await resolve_family_member(db_session, VIEWER, "father") is None


@pytest.mark.asyncio
async def test_legacy_fallback_when_new_columns_null(db_session):
    await _link(db_session, viewer_is_requester=True,
                acc_read=None, acc_file_share=True)
    assert await resolve_family_member(db_session, VIEWER, "father") == OWNER


@pytest.mark.asyncio
async def test_per_file_exclusion_hides_document(db_session):
    await _link(db_session, viewer_is_requester=True, acc_read=True)
    now = utcnow()
    db_session.add(Report(
        user_id=OWNER, filepath="demo/shared.pdf", private=False, created_at=now,
    ))
    db_session.add(Report(
        user_id=OWNER, filepath="demo/excluded.pdf", private=False, created_at=now,
    ))
    await db_session.flush()
    excluded_id = (
        await db_session.execute(
            __import__("sqlalchemy").select(Report.id).where(
                Report.filepath == "demo/excluded.pdf")
        )
    ).scalar_one()
    db_session.add(FileAccessExclusion(
        user_id=VIEWER, resource_type="reports", resource_id=excluded_id,
    ))
    await db_session.flush()

    hits = await latest_documents(
        db_session, OWNER, ["report"],
        owner_label="your father", include_private=False, viewer_id=VIEWER,
    )
    paths = {h.filepath for h in hits}
    assert "demo/shared.pdf" in paths
    assert "demo/excluded.pdf" not in paths


@pytest.mark.asyncio
async def test_ai_classification_title_used(db_session):
    db_session.add(Report(
        user_id=OWNER, filepath="s3/opaque-key-123.pdf", private=False,
        created_at=utcnow(), content=PROD_CONTENT,
    ))
    await db_session.flush()
    hits = await latest_documents(db_session, OWNER, ["report"])
    assert hits[0].title == "Full Body Checkup"


# --------------------------------------------------------------------------- #
# Document cards + dev-only consent-gated preview
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_document_reply_carries_cards(db_session):
    from app.chat.orchestrator import handle_chat
    from app.llm.fake import FakeProvider

    db_session.add(Report(
        user_id=VIEWER, filepath="uploads/reports/x1.pdf", private=False,
        created_at=utcnow(), content=PROD_CONTENT,
    ))
    await db_session.flush()
    r = await handle_chat(
        db_session, VIEWER, "find my latest blood report", FakeProvider()
    )
    assert r.documents and r.documents[0]["title"] == "Full Body Checkup"
    assert r.documents[0]["resource_type"] == "reports"
    assert isinstance(r.documents[0]["id"], int)


@pytest.mark.asyncio
async def test_preview_consent_gate(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.documents import document_preview

    db_session.add(Report(
        user_id=OWNER, filepath="uploads/reports/x2.pdf", private=False,
        created_at=utcnow(), content=PROD_CONTENT,
    ))
    await db_session.flush()
    rid = (
        await db_session.execute(
            __import__("sqlalchemy").select(Report.id).where(
                Report.filepath == "uploads/reports/x2.pdf")
        )
    ).scalar_one()

    # Owner can view; renders the AI title + extraction table.
    html = bytes(
        (await document_preview("reports", rid, OWNER, db_session)).body
    ).decode()
    assert "Full Body Checkup" in html and "Fasting Glucose" in html

    # A stranger (no connection) is refused.
    with pytest.raises(HTTPException) as e:
        await document_preview("reports", rid, VIEWER, db_session)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_preview_absent_outside_dev(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.documents import document_preview

    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as e:
        await document_preview("reports", 1, VIEWER, db_session)
    assert e.value.status_code == 404
    get_settings.cache_clear()

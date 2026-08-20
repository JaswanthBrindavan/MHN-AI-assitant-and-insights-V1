"""Pydantic request/response schemas for the v1 API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Slot = Literal[
    "mother",
    "father",
    "grandmother_maternal",
    "grandfather_maternal",
    "grandmother_paternal",
    "grandfather_paternal",
]
OnsetBand = Literal[
    "under_30",
    "30_34",
    "35_39",
    "40_44",
    "45_49",
    "50_54",
    "55_59",
    "60_64",
    "65_69",
    "70_plus",
    "unknown",
]
Certainty = Literal["verified", "confirmed", "as_far_as_i_know"]
Provenance = Literal["connected_verified", "self_report"]


class ConditionIn(BaseModel):
    condition_code: str = Field(max_length=32)
    condition_display: str = Field(max_length=128)
    onset_band: OnsetBand
    certainty: Certainty
    provenance: Provenance = "self_report"


class MemberIn(BaseModel):
    slot: Slot
    vital_status: str | None = None
    cause_of_death: str | None = None
    conditions: list[ConditionIn] = Field(default_factory=list)


class PedigreePut(BaseModel):
    # Optional; when present it is authorized against the token identity.
    user_id: uuid.UUID | None = None
    members: list[MemberIn] = Field(default_factory=list)


class ConditionOut(BaseModel):
    id: uuid.UUID
    slot: str
    condition_code: str
    condition_display: str
    onset_band: str
    certainty: str
    provenance: str


class MemberOut(BaseModel):
    slot: str
    vital_status: str | None
    cause_of_death: str | None


class PedigreeOut(BaseModel):
    user_id: uuid.UUID
    members: list[MemberOut]
    conditions: list[ConditionOut]


class InsightOut(BaseModel):
    id: uuid.UUID
    condition_code: str
    tier: str
    title: str
    body: str
    status: str
    template_key: str
    template_version: int
    pipeline_version: int
    content_hash: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class ChatResponse(BaseModel):
    response_message: str
    risk_level: str
    recommended_action: str
    provenance: dict
    grounding: dict | None = None
    session_id: uuid.UUID | None = None
    # Structured citations for cited sources ([n]/[P]/[GK] markers).
    citations: list[dict] | None = None
    # Optional visual: declarative chart spec + self-contained SVG.
    visual: dict | None = None
    # Detected user language (BCP-47ish; "hi-Latn" = romanized Hindi).
    language: str = "en"
    # Truthful pipeline decision trace (rendered as the "thinking" chain).
    trace: list[dict] = Field(default_factory=list)
    # Document cards for replies that reference stored files:
    # [{kind, resource_type, id, title, date, owner}] — the client opens the
    # file via the EXISTING app flow (Spring GET /files/{type}/{id}/url or the
    # health-wallet detail routes); Davi never mints URLs or touches S3.
    documents: list[dict] | None = None


class ChatUploadRequest(BaseModel):
    # An existing unclassified_files id — the file itself reached S3 + that
    # row via Spring's upload flow; Davi only submits the processing run.
    document_id: int
    message: str = Field(default="", max_length=4000)
    session_id: uuid.UUID | None = None


class UploadedDocumentInfo(BaseModel):
    resource_type: str  # "unclassified_files" — the unit mhn-ai runs process
    doc_id: int
    # "pending" until mhn-ai's pipeline classifies, files, and extracts.
    state: str
    # Whether mhn-ai accepted the processing-run submission (202).
    triggered: bool
    # From mhn-ai's CreateRunResponse, when accepted.
    run_id: str | None = None
    item_status: str | None = None


class ChatUploadResponse(BaseModel):
    response_message: str
    session_id: uuid.UUID
    document: UploadedDocumentInfo


class ChatSessionInfo(BaseModel):
    session_id: uuid.UUID
    created_at: datetime | None = None
    last_message_at: datetime | None = None
    message_count: int
    preview: str = ""


class ChatMessageInfo(BaseModel):
    id: uuid.UUID
    role: str
    message: str
    created_at: datetime | None = None

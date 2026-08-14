"""Pydantic request/response schemas for the v1 API."""

from __future__ import annotations

import uuid
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

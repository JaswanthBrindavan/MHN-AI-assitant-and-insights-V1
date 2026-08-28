"""The brutal corpus: 160 adversarial medication messages, run against the
REAL flow, asserting ZERO misinterpretations.

Nine adversaries authored labeled traps (false positives, emergency/self-harm
collisions, dosage questions, Indian brands + dosing notation, confirmation
corrections, typos/unicode/voice, romanized Hindi/Telugu); every label was
verified by executing the code, and four were adjudicated where the code's
behavior is strictly safer than the label (noted inline in the golden file).

The contract this suite pins:
- despair/overdose language NEVER enters the flow, at any stage;
- a QUESTION never fires a write (a question-shaped pure list ask may read);
- a correction at the confirm step is never swallowed by a leading "yes";
- non-medication "stop/remove/add X" never hijacks the turn;
- Indian dosing notation (1-0-1, TID/BID/OD/SOS, every-8-hours, meals) parses;
- "3 times a week" and other non-daily frequencies are NEVER a daily pattern.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat import medication_flow as mf
from app.medicines import service as med

_USER = uuid.UUID("33333333-3333-3333-3333-333333333333")
# The flow only touches the db inside the write, which the sentinel prevents.
_NO_DB = cast(AsyncSession, None)

_G = pathlib.Path(__file__).parent / "golden"
# Round 1 (shaped the fixes) + round 2 (held out, fresh angles: third-party
# meds, dose changes, conditional futures, insulin forms, rambles).
CORPUS = (json.loads((_G / "medication_brutal.json").read_text())
          + json.loads((_G / "medication_brutal_r2.json").read_text()))

_COURSES = (
    med.Course(tracking_id=1, name="Dolo 650"),
    med.Course(tracking_id=2, name="Ecosprin 75"),
    med.Course(tracking_id=3, name="Vitamin D3"),
    med.Course(tracking_id=4, name="Metoprolol 25mg"),
    med.Course(tracking_id=5, name="Thyronorm 50mcg"),
)


class _Sentinel:
    """Records whether the LLM layer was consulted; extraction itself fails so
    the deterministic outcome is what we observe."""

    model_name = "sentinel"

    def __init__(self):
        self.called = False

    async def generate(self, system, user):
        self.called = True
        return "unparseable"


@pytest.fixture(autouse=True)
def _courses(monkeypatch):
    async def _fake_list(user_id, *, active_only=True, client=None):
        return med.MedResult(ok=True, courses=_COURSES)

    monkeypatch.setattr(med, "list_courses", _fake_list)


async def _flow_intent(msg: str) -> str:
    s = _Sentinel()
    r = await mf.handle_medication_turn(_NO_DB, _USER, msg, None, s)
    if r is None:
        return "llm" if s.called else "none"
    det = mf.detect_intent(msg)
    return det["action"] if det else "llm"


_CASES = [(g["category"], c) for g in CORPUS for c in g["cases"]]


@pytest.mark.parametrize(
    ("category", "case"),
    _CASES,
    ids=[f"{cat}:{c['input'][:40]}" for cat, c in _CASES],
)
async def test_brutal_case(category, case):
    msg = case["input"]
    kind = case["kind"]
    if kind == "intent":
        got = await _flow_intent(msg)
        exp = case["expect"]
        # none and llm are EQUIVALENT outcomes: neither fires a deterministic
        # action — the turn reaches the model either way (main engine or the
        # extractor, which declines non-commands), so no wrong write can occur.
        if {got, exp} == {"none", "llm"}:
            return
        assert got == exp, f"{msg!r}: expected {exp}, got {got} — {case['why']}"
        if got in ("add", "stop", "remove") and case.get("name_contains"):
            intent = mf.detect_intent(msg)
            if intent is not None:
                assert case["name_contains"].lower() in intent["name"].lower()
    elif kind == "schedule":
        r = mf.parse_schedule(msg)
        got = "none" if r is None else ("prn" if r[1] else (r[0] or "none"))
        exp = case.get("expect_schedule", case["expect"])
        assert got.lower() == exp.lower(), (
            f"{msg!r}: expected {exp}, got {got} — {case['why']}")
    else:  # yesno
        r = mf.parse_yes_no(msg)
        got = {True: "yes", False: "no", None: "none"}[r]
        exp = case.get("expect_yesno", case["expect"])
        assert got == exp, f"{msg!r}: expected {exp}, got {got} — {case['why']}"

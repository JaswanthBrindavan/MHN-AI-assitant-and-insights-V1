"""Phase 6 — pure structured-compaction extractors and merging."""

from __future__ import annotations

import json

from app.chat.memory import (
    CAP,
    compact_messages,
    extract_flags,
    extract_medications,
    is_boundary,
    merge_summaries,
)


def test_extract_flags_uses_triage_vocabulary():
    flags = extract_flags("suddenly I can't breathe and there's chest pain")
    assert "can't breathe" in flags


def test_extract_medications_drug_and_dose():
    meds = extract_medications("I take metformin 500 mg every morning")
    assert meds == ["metformin 500 mg"]


def test_extract_medications_ignores_bare_dose():
    # "take 500 mg" has no real drug name → not captured.
    assert extract_medications("take 500 mg with food") == []


def test_extract_medications_multiple():
    meds = extract_medications("metformin 500 mg and amlodipine 5 mg")
    assert "metformin 500 mg" in meds
    assert "amlodipine 5 mg" in meds


def test_is_boundary_detects_declines():
    assert is_boundary("I can only help with health questions.")
    assert is_boundary("Sorry, I'm not a doctor and I don't diagnose.")
    assert not is_boundary("Here is some general information about diabetes.")


def test_compact_messages_collects_sticky_and_capped():
    messages = [
        {"role": "user", "message": "I can't breathe suddenly"},
        {"role": "user", "message": "I take metformin 500 mg"},
        {"role": "assistant", "message": "I can only help with health questions."},
        {"role": "user", "message": "what about my blood sugar?"},
    ]
    s = compact_messages(messages)
    assert "can't breathe" in s["flags"]
    assert "metformin 500 mg" in s["medications"]
    assert s["boundaries"]  # the decline was captured
    assert "T2DM" in s["topics"]
    assert s["open_questions"]  # the question was captured
    # timeline preserves first-mention order across flags/meds/topics.
    # "can't breathe" now also trips the breathing-difficulty PATTERN label
    # (sorted alongside the phrase), so the first entry is one of the two
    # vocabulary rows for that same utterance.
    assert s["timeline"][0] in ("can't breathe",
                                "breathing difficulty (pattern)")
    assert "can't breathe" in s["timeline"]


def test_merge_sticky_unions_without_truncation():
    old = {"flags": ["a", "b"], "medications": ["m1"], "boundaries": [], "timeline": ["a"]}
    new = {"flags": ["b", "c"], "medications": ["m2"], "boundaries": ["x"], "timeline": ["c"]}
    merged = merge_summaries(old, new)
    assert merged["flags"] == ["a", "b", "c"]  # dedup, no truncation
    assert merged["medications"] == ["m1", "m2"]
    assert merged["boundaries"] == ["x"]
    assert merged["timeline"] == ["a", "c"]


def test_merge_caps_topics_and_open_questions():
    old = {"topics": [f"t{i}" for i in range(10)], "open_questions": []}
    new = {"topics": [f"t{i}" for i in range(10, 20)], "open_questions": []}
    merged = merge_summaries(old, new)
    assert len(merged["topics"]) == CAP  # capped at 12


async def test_recovery_report_resolves_the_episode_instead_of_extending_it(
    db_session,
):
    """"my chest pain is better now" must CLOSE the chest-pain episode.
    resolve() had no caller (audit high): recovery reports re-touched the
    episode, so the [P] block kept asserting the symptom for two more weeks."""
    import uuid as _uuid

    from app.chat import memory_assembly
    from app.chat.episodes import open_episodes, open_or_touch

    user = _uuid.uuid4()
    await open_or_touch(db_session, user, "chest pain", "high")
    assert len(await open_episodes(db_session, user)) == 1

    await memory_assembly.record(
        db_session, user, codes=(), flags=["chest pain"], risk="high",
        message="my chest pain is much better now",
    )
    assert await open_episodes(db_session, user) == []


async def test_bare_feeling_better_closes_the_only_open_episode(db_session):
    import uuid as _uuid

    from app.chat import memory_assembly
    from app.chat.episodes import open_episodes, open_or_touch

    user = _uuid.uuid4()
    await open_or_touch(db_session, user, "vomiting blood", "high")
    await memory_assembly.record(
        db_session, user, codes=(), flags=[], risk="none",
        message="feeling better now, thanks",
    )
    assert await open_episodes(db_session, user) == []


# --------------------------------------------------------------------------- #
# Every key is bounded (audit M13)
#
# Sticky keys used to merge "without truncation" forever. Retention purges
# only the SUPERSEDED versions of a summary, so the surviving row of a long
# session grew without limit — unbounded PHI in a JSON column, re-serialised
# into the prompt on every turn.
# --------------------------------------------------------------------------- #
def test_sticky_keys_hold_their_bound_after_many_merges():
    from app.chat.memory import STICKY_CAPS, STICKY_KEYS, empty_summary

    merged = empty_summary()
    for i in range(200):
        part = empty_summary()
        part["flags"] = [f"flag {i}"]
        part["medications"] = [f"drug{i} 5 mg"]
        part["boundaries"] = [f"I can't help with that ({i}) " + "x" * 90]
        part["timeline"] = [f"flag {i}", f"drug{i} 5 mg"]
        merged = merge_summaries(merged, part)

    for k in STICKY_KEYS:
        assert len(merged[k]) <= STICKY_CAPS[k], (k, len(merged[k]))
    # And the whole dict stays a bounded prompt cost, not a transcript.
    assert len(json.dumps(merged)) < 4000


def test_an_early_red_flag_still_survives_every_later_pass():
    """What sticky exists for: message 1's flag stands at message 200."""
    from app.chat.memory import empty_summary

    merged = empty_summary()
    merged["flags"] = ["chest pain"]
    for i in range(100):
        part = empty_summary()
        part["flags"] = [f"other {i}"]
        merged = merge_summaries(merged, part)
    assert merged["flags"][0] == "chest pain"


def test_the_newest_medication_is_the_one_kept():
    """A dose change matters more than the dose it replaced: over the cap,
    it is the OLDEST medication that rolls off, not the newest."""
    from app.chat.memory import STICKY_CAPS, empty_summary

    merged = empty_summary()
    merged["medications"] = ["metformin 500 mg"]
    for i in range(STICKY_CAPS["medications"]):
        part = empty_summary()
        part["medications"] = [f"drug{i} 5 mg"]
        merged = merge_summaries(merged, part)
    assert len(merged["medications"]) == STICKY_CAPS["medications"]
    assert "metformin 500 mg" not in merged["medications"]  # the oldest rolled off
    assert merged["medications"][-1] == f"drug{STICKY_CAPS['medications'] - 1} 5 mg"


def test_a_single_compaction_batch_is_bounded_too():
    from app.chat.memory import STICKY_CAPS

    messages = [
        {"role": "assistant", "message": f"I can't help with that, number {i}."}
        for i in range(20)
    ]
    assert len(compact_messages(messages)["boundaries"]) == STICKY_CAPS["boundaries"]

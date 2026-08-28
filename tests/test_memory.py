"""Phase 6 — pure structured-compaction extractors and merging."""

from __future__ import annotations

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

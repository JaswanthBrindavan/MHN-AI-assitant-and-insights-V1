"""Streamed replies must be as safe as buffered ones.

The invariant: nothing reaches the client that the whole-answer guards could
reject, and the answer the pipeline finally settles on always wins.
"""

from __future__ import annotations

from app.chat.replies import HIGH_ESCALATION
from app.chat.streaming import AnswerSink, split_complete_sentences
from app.triage.red_flags import HIGH, NONE

SAFE = "I'd rather keep this general — please speak with a clinician."


def _sink(events: list[dict], **arm) -> AnswerSink:
    sink = AnswerSink(events.append)
    arm.setdefault("risk", NONE)
    arm.setdefault("sources", [])
    sink.arm(**arm)
    return sink


def _shown(events: list[dict]) -> str:
    """What the client would actually be showing at the end."""
    shown = ""
    for event in events:
        if event["type"] == "delta":
            shown += event["text"]
        elif event["type"] == "replace":
            shown = event["text"]
    return shown


def _deltas(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


# --------------------------------------------------------------------------- #
# Sentence splitting
# --------------------------------------------------------------------------- #
def test_an_incomplete_sentence_is_held_back():
    complete, remainder = split_complete_sentences("This is fine. And this is inc")
    assert complete == ["This is fine. "]
    assert remainder == "And this is inc"


def test_nothing_is_released_without_a_terminator():
    complete, remainder = split_complete_sentences("no terminator yet")
    assert complete == []
    assert remainder == "no terminator yet"


def test_multiple_sentences_release_together():
    complete, remainder = split_complete_sentences("One. Two! Three? Four")
    assert len(complete) == 3
    assert remainder == "Four"


def test_paragraph_breaks_survive_the_split():
    complete, _ = split_complete_sentences("One.\n\nTwo. ")
    assert complete == ["One.\n\n", "Two. "]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_clean_answer_streams_sentence_by_sentence():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("Sleep matters. Try a ")
    assert _deltas(events) == "Sleep matters. "
    sink.feed("regular bedtime.")
    sink.finish("Sleep matters. Try a regular bedtime.")
    assert all(e["type"] == "delta" for e in events)
    assert _shown(events) == "Sleep matters. Try a regular bedtime."


def test_the_trailing_fragment_is_released_on_flush():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("A complete one. an unterminated tail")
    sink.flush()
    assert "an unterminated tail" in _deltas(events)


def test_an_unarmed_sink_shows_nothing_until_finish():
    events: list[dict] = []
    sink = AnswerSink(events.append)
    sink.feed("Should be ignored. ")
    assert events == []
    sink.finish("The canned reply.")
    assert events == [{"type": "delta", "text": "The canned reply."}]


def test_citation_markers_never_reach_the_client():
    events: list[dict] = []
    sink = _sink(events, sources=["HbA1c 6.1%"])
    sink.feed("Your HbA1c was 6.1% [1]. Keep going [P]. ")
    assert "[" not in _deltas(events)
    assert "6.1%." in _deltas(events)


# --------------------------------------------------------------------------- #
# Mid-stream blocking: the sentence is never shown, nor anything after it
# --------------------------------------------------------------------------- #
def test_a_banned_sentence_is_never_released():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("Here is some context. You probably have diabetes. More text. ")
    sink.finish(SAFE)
    assert "probably have" not in _deltas(events)
    assert "More text" not in _deltas(events)
    assert events[-1]["type"] == "replace"
    assert _shown(events) == SAFE


def test_a_banned_tail_is_caught_too():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("Fine. You probably have diabetes")
    sink.flush()
    assert "probably" not in _deltas(events)


def test_a_provider_leak_is_blocked():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("I am powered by GPT-4. Anyway. ")
    assert "gpt" not in _deltas(events).lower()


def test_a_cross_sentence_rule_sees_the_released_prefix():
    """Wearable grading through a back-reference: the second sentence alone
    is harmless, the pair is a grade. The check runs on the cumulative text."""
    events: list[dict] = []
    sink = _sink(events, sources=["sleep averaged 5.1 hours a night"])
    sink.feed(
        "Your sleep averaged 5.1 hours a night. That is below what most adults need. "
    )
    assert "5.1 hours" in _deltas(events)
    assert "below" not in _deltas(events)


# --------------------------------------------------------------------------- #
# Numeric fidelity: a value the guard would reject is never shown
# --------------------------------------------------------------------------- #
def test_a_traceable_value_streams_and_an_untraceable_one_does_not():
    events: list[dict] = []
    sink = _sink(events, sources=["HbA1c 6.1% on 2 March"])
    sink.feed("Your HbA1c was 6.1%. Your fasting glucose was 140 mg/dL. Discuss it. ")
    assert "6.1%" in _deltas(events)
    assert "140" not in _deltas(events)
    assert "Discuss it" not in _deltas(events)


def test_a_value_with_no_sources_behind_it_is_held():
    events: list[dict] = []
    sink = _sink(events, sources=[])
    sink.feed("Take 500 mg twice a day. ")
    assert events == []


def test_tool_results_become_sources_round_by_round():
    events: list[dict] = []
    sink = _sink(events, sources=[])
    sink.new_round(['{"hba1c": "6.1%"}'])
    sink.feed("Your HbA1c was 6.1%. ")
    assert "6.1%" in _deltas(events)


# --------------------------------------------------------------------------- #
# HIGH risk: the banner leads, and the validator's escalation rule holds
# --------------------------------------------------------------------------- #
def test_the_escalation_banner_leads_a_high_risk_answer():
    events: list[dict] = []
    sink = _sink(events, risk=HIGH, lead=f"{HIGH_ESCALATION} ")
    sink.feed("Rest and drink fluids. ")
    assert events[0]["text"].startswith(HIGH_ESCALATION)
    assert "Rest and drink fluids." in events[0]["text"]


def test_care_discouraging_text_is_blocked_at_high():
    events: list[dict] = []
    sink = _sink(events, risk=HIGH, lead=f"{HIGH_ESCALATION} ")
    sink.feed("This is not an emergency. ")
    assert events == []


# --------------------------------------------------------------------------- #
# Tool rounds and reconciliation
# --------------------------------------------------------------------------- #
def test_a_tool_round_preamble_is_retracted():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("Let me check your records. ")
    sink.new_round([])
    assert events[-1] == {"type": "replace", "text": "", "reason": "tool_round"}
    sink.feed("Nothing on record. ")
    sink.finish("Nothing on record.")
    assert _shown(events).strip() == "Nothing on record."
    # Whitespace is the only difference, so nothing was retracted twice.
    assert [e["type"] for e in events] == ["delta", "replace", "delta"]


def test_a_new_round_reopens_a_closed_sink():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("You probably have diabetes. ")
    sink.new_round([])
    sink.feed("A clean answer. ")
    assert _deltas(events) == "A clean answer. "


def test_finish_is_silent_when_the_stream_matches():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("One.\n\nTwo. Three")
    sink.finish("One.\n\nTwo. Three")
    assert not any(e["type"] == "replace" for e in events)


def test_finish_replaces_when_the_pipeline_rewrote_the_answer():
    events: list[dict] = []
    sink = _sink(events)
    sink.feed("Sleep matters. ")
    sink.finish("Sleep matters, and so does a routine.")
    assert events[-1] == {
        "type": "replace",
        "text": "Sleep matters, and so does a routine.",
        "reason": "final_check",
    }


def test_an_extra_check_sees_the_raw_markers():
    seen: list[str] = []

    def _grounded(raw: str) -> bool:
        seen.append(raw)
        # Every sentence of the prefix must carry a marker.
        return all("[" in s for s in raw.strip().split(". ") if s)

    events: list[dict] = []
    sink = _sink(events, sources=["6.1%"], extra_check=_grounded)
    sink.feed("Your HbA1c was 6.1% [1]. Uncited claim. ")
    assert "6.1%" in _deltas(events)
    assert "Uncited" not in _deltas(events)
    assert seen[0] == "Your HbA1c was 6.1% [1]. "

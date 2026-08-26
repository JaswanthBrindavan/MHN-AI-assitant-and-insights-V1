"""Streamed replies must be as safe as buffered ones.

The invariant: no unvalidated sentence ever reaches the client, and anything a
whole-answer guard rejects can still be retracted with a `replace` event.
"""

from __future__ import annotations

from app.chat.streaming import split_complete_sentences, validated_stream

SAFE = "I'd rather keep this general — please speak with a clinician."


async def _collect(chunks, **kwargs):
    events = []
    async for event in validated_stream(
        chunks, risk_level="none", safe_fallback=SAFE, **kwargs
    ):
        events.append(event)
    return events


def _text_of(events) -> str:
    """What the client would actually be showing at the end."""
    shown = ""
    for event in events:
        if event["type"] == "delta":
            shown += event["text"]
        elif event["type"] == "replace":
            shown = event["text"]
    return shown


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


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_a_clean_answer_streams_then_finishes():
    events = await _collect(["Sleep matters. ", "Try a regular bedtime."])
    assert all(e["type"] == "delta" for e in events)
    assert "Sleep matters." in _text_of(events)
    assert "regular bedtime" in _text_of(events)


async def test_the_trailing_fragment_is_still_delivered():
    """A final sentence with no terminator must not be silently dropped."""
    events = await _collect(["A complete one. ", "an unterminated tail"])
    assert "an unterminated tail" in _text_of(events)


# --------------------------------------------------------------------------- #
# Mid-stream blocking
# --------------------------------------------------------------------------- #
async def test_a_banned_sentence_aborts_the_stream():
    events = await _collect(
        ["Here is some context. ", "You probably have diabetes. ", "More text."]
    )
    assert events[-1]["type"] == "replace"
    assert _text_of(events) == SAFE


async def test_nothing_after_the_block_is_emitted():
    events = await _collect(
        ["Fine. ", "You probably have diabetes. ", "Should never appear."]
    )
    assert not any("never appear" in e["text"] for e in events)


async def test_a_banned_tail_is_caught_too():
    """The unterminated remainder gets checked, not waved through."""
    events = await _collect(["Fine. ", "You probably have diabetes"])
    assert events[-1]["type"] == "replace"


async def test_a_provider_leak_is_blocked_mid_stream():
    events = await _collect(["I am powered by GPT-4. ", "Anyway."])
    assert events[-1]["type"] == "replace"
    assert "gpt" not in _text_of(events).lower()


# --------------------------------------------------------------------------- #
# Whole-answer guards
# --------------------------------------------------------------------------- #
async def test_a_final_check_can_retract_what_was_already_streamed():
    """This is why `replace` exists — the fidelity guard needs the full text,
    by which time deltas are already on the reader's screen."""

    def _reject(_text: str) -> str:
        return SAFE

    events = await _collect(
        ["Your HbA1c was 6.5%. ", "That is worth discussing."],
        final_check=_reject,
    )
    assert events[-1]["type"] == "replace"
    assert _text_of(events) == SAFE


async def test_a_passing_final_check_leaves_the_stream_alone():
    events = await _collect(
        ["All good here. "], final_check=lambda _text: None
    )
    assert not any(e["type"] == "replace" for e in events)


async def test_the_final_check_sees_the_whole_released_text():
    seen: list[str] = []

    def _capture(text: str):
        seen.append(text)
        return None

    await _collect(["One. ", "Two. ", "Three"], final_check=_capture)
    assert "One." in seen[0] and "Two." in seen[0] and "Three" in seen[0]


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
async def test_a_provider_error_mid_stream_becomes_a_replace():
    async def _explodes():
        yield "Starting fine. "
        raise RuntimeError("provider died")

    events = []
    async for event in validated_stream(
        _explodes(), risk_level="none", safe_fallback=SAFE
    ):
        events.append(event)

    assert events[-1]["type"] == "replace"
    assert events[-1]["reason"] == "stream_error"
    assert _text_of(events) == SAFE


async def test_an_empty_stream_produces_no_events():
    assert await _collect([]) == []


async def test_empty_chunks_are_skipped():
    events = await _collect(["", "Real text. ", ""])
    assert _text_of(events).strip() == "Real text."

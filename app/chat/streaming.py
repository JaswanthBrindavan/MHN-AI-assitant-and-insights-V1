"""Incremental release of a model answer, under the same guards as a buffered one.

The problem
-----------
Every safety guarantee in this service runs AFTER generation: the numeric
fidelity guard, the ungrounded-value rule, ``validate_reply``, and (in enforce
mode) claim grounding. A naive stream puts text on the reader's screen that
those checks would have blocked, and a ``replace`` event cannot un-show a
drifted lab value.

What may be shown before the whole answer exists
------------------------------------------------
Exactly the text that the whole-answer checks are *already guaranteed* to
accept. That is decidable one sentence at a time, because every post-check
this service applies is prefix-monotone:

* ``values_traceable`` and ``unit_values`` look at each unit-bearing value in
  isolation, so a value that is untraceable in the whole answer is untraceable
  in the first sentence-ended prefix that contains it. Sentences are cut at
  ``[.!?]`` followed by whitespace, which no clinical value spans.
* ``validate_reply`` is banned-substring and regex matching (plus the
  escalation requirement at HIGH, which the constant banner in ``lead``
  satisfies). A match in the whole answer is a match in the prefix that
  completes it. Its one cross-sentence rule — wearable grading via a
  back-reference to the previous sentence — is why the check runs on the
  CUMULATIVE text, never on the sentence alone.
* ``analyze_grounding`` (enforce mode only) judges sentence by sentence.

So the sink accumulates deltas, and each time a sentence completes it runs
the real guard functions over *everything released so far plus that sentence*,
with the same sources the buffered path will use. Pass: the sentence goes out.
Fail: the sink closes and releases nothing further — the buffered path then
does what it always did (one corrective retry, then the safe reply), and the
final answer is reconciled with a ``replace``.

What this cannot leak: a clinical value the post-checks would reject. What it
can do, at worst, is show a validated prefix that a later sentence caused the
buffered path to rewrite; the reader then sees the rewrite in full.

Tool rounds
-----------
The agentic engine offers tools on every round, and a round's text is only
known to be the answer once the round ends without a tool call. Text ahead
of a tool call ("let me check your records") is narration the buffered path
never displays, so it is released under the same rules — it carries no value
the guards would reject — and retracted with an empty ``replace`` when the
next round begins.

The client contract is unchanged:

    delta    {"text": "..."}   append this
    replace  {"text": "..."}   discard everything shown so far, show this
    done     {...}             final metadata
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence

from app.chat.validation import validate_reply
from app.grounding.claims import MARKER_RE
from app.grounding.fidelity import unit_values, values_traceable
from app.triage.red_flags import NONE

logger = logging.getLogger("davi.streaming")

# A sentence is releasable once it ends in terminal punctuation followed by
# whitespace. The whitespace is CAPTURED so paragraph breaks survive the trip:
# the reader's copy must read like the persisted one. Deliberately simple:
# holding a fragment slightly too long costs a small delay, releasing one too
# early puts unvalidated text on a patient's screen.
# ponytail: a bulleted answer with no terminators is held to the end; add
# "\n\n" as a boundary if that measurably hurts.
_SENTENCE_SPLIT_RE = re.compile(r"((?<=[.!?])\s+)")
_WS_RE = re.compile(r"\s+")


def split_complete_sentences(buffer: str) -> tuple[list[str], str]:
    """Split a buffer into (complete sentences, trailing remainder).

    Each complete sentence keeps its own trailing whitespace. The remainder is
    whatever has not yet been terminated, and is held back until it is.
    """
    if not buffer:
        return [], ""
    pieces = _SENTENCE_SPLIT_RE.split(buffer)
    if len(pieces) == 1:
        return [], buffer
    complete = [
        pieces[i] + pieces[i + 1] for i in range(0, len(pieces) - 1, 2) if pieces[i]
    ]
    return complete, pieces[-1]


def _for_display(piece: str) -> str:
    """``strip_markers`` minus the outer strip that would eat a trailing space."""
    piece = MARKER_RE.sub("", piece)
    piece = re.sub(r"\s+([.,;:!?])", r"\1", piece)
    return re.sub(r"[ \t]{2,}", " ", piece)


def _same_text(a: str, b: str) -> bool:
    return _WS_RE.sub(" ", a).strip() == _WS_RE.sub(" ", b).strip()


class AnswerSink:
    """Releases model text to a client only once the guards are sure of it.

    ``emit`` receives stream events. The sink is inert until ``arm`` is called
    by the engine that knows the risk level and the sources, so a path that
    never generates (a canned reply, an extractive answer) streams nothing and
    ``finish`` delivers its text whole.
    """

    def __init__(self, emit: Callable[[dict], None]) -> None:
        self._emit = emit
        # What the client is showing, marker-free, lead included.
        self.released = ""
        # The same text WITH citation markers: enforce-mode grounding reads
        # them, while fidelity and validation see the display form — exactly
        # the two forms the buffered path checks.
        self._raw = ""
        self._buffer = ""
        self._armed = False
        self._closed = False
        self._risk = NONE
        self._base_sources: list[str] = []
        self._sources: list[str] = []
        self._extra: tuple[str, ...] | None = None
        self._lead = ""
        # Tool results of the current round, handed to ``extra_check``: on the
        # agentic engine they are what a [P] citation may rest on.
        self._tool_sources: list[str] = []
        self._extra_check: Callable[[str, Sequence[str]], bool] | None = None

    def arm(
        self,
        *,
        risk: str,
        sources: Iterable[str],
        extra_conditions: tuple[str, ...] | None = None,
        lead: str = "",
        extra_check: Callable[[str, Sequence[str]], bool] | None = None,
    ) -> None:
        """Start accepting text. ``lead`` is a constant prefix (the HIGH
        banner) shown with the first sentence; ``extra_check`` sees the raw,
        marker-bearing prefix plus this round's tool results, and must return
        True to allow release."""
        self._armed = True
        self._risk = risk
        self._base_sources = list(sources)
        self._sources = list(self._base_sources)
        self._extra = extra_conditions
        self._lead = lead
        self._extra_check = extra_check

    def new_round(self, tool_sources: Iterable[str] = ()) -> None:
        """A new model call begins. Whatever the previous call produced was a
        tool-round preamble, not the answer: retract it."""
        self._tool_sources = list(tool_sources)
        self._sources = [*self._base_sources, *self._tool_sources]
        self._buffer = ""
        self._closed = False
        if self.released:
            self._emit({"type": "replace", "text": "", "reason": "tool_round"})
            self.released = ""
            self._raw = ""

    def feed(self, text: str) -> None:
        if not self._armed or self._closed or not text:
            return
        self._buffer += text
        sentences, self._buffer = split_complete_sentences(self._buffer)
        for sentence in sentences:
            if not self._release(sentence):
                return

    def flush(self) -> None:
        """Release the unterminated tail. Same rules as a sentence."""
        tail, self._buffer = self._buffer, ""
        if tail.strip() and self._armed and not self._closed:
            self._release(tail)

    def finish(self, final: str) -> None:
        """Reconcile with the answer the pipeline actually settled on.

        Nothing streamed: the text goes out whole, as a single delta. Streamed
        text that matches: nothing more to say. Anything else — a guard closed
        the sink, a corrective retry rewrote the answer, a notice was
        appended — is a ``replace``, so the client always ends on exactly the
        persisted reply.
        """
        self.flush()
        if not self.released:
            if final:
                self._emit({"type": "delta", "text": final})
        elif not _same_text(self.released, final):
            self._emit({"type": "replace", "text": final, "reason": "final_check"})
        self.released = final
        self._armed = False

    # -- internals ----------------------------------------------------------
    def _release(self, piece: str) -> bool:
        raw = piece
        piece = _for_display(piece)
        if self.released.endswith((" ", "\n")):
            piece = piece.lstrip(" ")
        if not piece.strip():
            return True
        first = not self.released
        candidate = (self._lead if first else self.released) + piece
        if not self._passes(candidate, self._raw + raw):
            logger.info("stream held back; the buffered path decides")
            self._closed = True
            return False
        self._emit({"type": "delta", "text": (self._lead if first else "") + piece})
        self.released = candidate
        self._raw += raw
        return True

    def _passes(self, text: str, raw: str) -> bool:
        """The buffered ladder, on the released prefix. Same functions, same
        order, same sources — see the module docstring for why a prefix
        verdict is the whole-answer verdict."""
        ok, _stray = values_traceable(text, self._sources)
        if not ok:
            return False
        if not self._sources and unit_values(text):
            return False
        if not validate_reply(text, self._risk, self._extra).ok:
            return False
        return self._extra_check is None or self._extra_check(raw, self._tool_sources)

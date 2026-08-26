"""Incremental validation for streamed replies.

You cannot verify a whole answer and stream it at the same time, so the
compromise is per-sentence: text accumulates, and a sentence is released only
once it has passed the banned-phrase check. Anything that needs the FULL answer
— numeric fidelity, the escalation requirement — runs at the end and can still
retract what was shown.

The client contract is three event types:

    delta    {"text": "..."}   append this
    replace  {"text": "..."}   discard everything shown so far, show this
    done     {...}             final metadata

``replace`` is what makes streaming safe here. Without it, a guard that can only
fire on the complete answer would have no way to act on text already on the
reader's screen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterable

from app.chat.validation import find_banned

logger = logging.getLogger("davi.streaming")

# A sentence is releasable once it ends in terminal punctuation followed by
# whitespace. Deliberately simple: holding a fragment slightly too long costs a
# small delay, releasing one too early puts unvalidated text on a patient's
# screen.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_complete_sentences(buffer: str) -> tuple[list[str], str]:
    """Split a buffer into (complete sentences, trailing remainder).

    The remainder is whatever has not yet been terminated, and is held back
    until it is.
    """
    if not buffer:
        return [], ""
    pieces = _SENTENCE_SPLIT_RE.split(buffer)
    if len(pieces) == 1:
        return [], buffer
    # Everything except the last piece is complete. Rebuild with a single
    # trailing space so the reassembled text reads normally.
    complete = [p + " " for p in pieces[:-1] if p]
    return complete, pieces[-1]


async def validated_stream(
    chunks: AsyncIterator[str] | Iterable[str],
    *,
    risk_level: str,
    safe_fallback: str,
    extra_conditions: tuple[str, ...] | None = None,
    final_check=None,
) -> AsyncIterator[dict]:
    """Yield validated stream events from a stream of raw text chunks.

    ``final_check`` runs on the complete text and returns None when it passes,
    or a replacement string when it does not — that is where the whole-answer
    guards (numeric fidelity, escalation) live.

    Never raises: a failure anywhere degrades to a single ``replace`` carrying
    the safe fallback, because a half-streamed reply is worse than a canned one.
    """
    buffer = ""
    released = ""
    aborted = False

    try:
        iterator = (
            chunks
            if hasattr(chunks, "__anext__")
            else _as_async(chunks)  # type: ignore[arg-type]
        )
        async for piece in iterator:  # type: ignore[union-attr]
            if not piece:
                continue
            buffer += piece
            sentences, buffer = split_complete_sentences(buffer)
            for sentence in sentences:
                banned = find_banned(sentence, extra_conditions)
                if banned is not None:
                    logger.warning("streamed sentence blocked: %s", banned)
                    yield {"type": "replace", "text": safe_fallback,
                           "reason": "banned"}
                    aborted = True
                    break
                released += sentence
                yield {"type": "delta", "text": sentence}
            if aborted:
                return

        # Whatever is left in the buffer never got its terminator.
        if buffer.strip():
            banned = find_banned(buffer, extra_conditions)
            if banned is not None:
                logger.warning("streamed tail blocked: %s", banned)
                yield {"type": "replace", "text": safe_fallback,
                       "reason": "banned"}
                return
            released += buffer
            yield {"type": "delta", "text": buffer}

        # Whole-answer guards. These are why `replace` exists.
        if final_check is not None:
            replacement = final_check(released)
            if replacement is not None:
                yield {"type": "replace", "text": replacement,
                       "reason": "final_check"}
                return

    except Exception:  # noqa: BLE001 — a stream must never crash the endpoint
        logger.warning("stream failed mid-flight; replacing", exc_info=True)
        yield {"type": "replace", "text": safe_fallback, "reason": "stream_error"}


async def _as_async(items: Iterable[str]) -> AsyncIterator[str]:
    for item in items:
        yield item

"""The one place per-user memory is read and written, for BOTH engines.

Davi keeps five kinds of long-lived per-user memory, and before this module
each engine used only about half of them:

| Memory                          | legacy | agentic |
|---------------------------------|--------|---------|
| `user_profiles` (consent-gated) | never  | read    |
| open symptom episodes           | never  | read    |
| recording a symptom episode     | never  | written |
| discussed-topic recall          | read   | never   |
| recording discussed topics      | written| never   |

`CHAT_ENGINE` defaults to `legacy`, so in production today the consent-gated
profile a reader filled in is **never read into the prompt**, and no symptom
episode is ever recorded. Under `agentic`, long-term memory never accumulates
at all. Two shipped features that do not reach anyone.

This is the same failure the drug-interaction refusal had in Phase 4: a
capability sitting inside one engine's branch instead of above the fork. The
fix is the same, and CLAUDE.md now states the rule — anything deterministic
that both engines need belongs in shared code, not duplicated into each
branch, because duplicated code drifts and this is what drift looks like.

Everything here FAILS OPEN. Memory is enrichment; no reader should lose an
answer because a recall query failed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.episodes import (
    is_recovery_message,
    log_symptom,
    open_episodes,
    open_or_touch,
    resolve,
)
from app.chat.episodes import render_for_prompt as render_episodes
from app.chat.erasure import is_pending
from app.chat.long_term import recall, record_topics
from app.chat.profile import get_profile
from app.chat.profile import render_for_prompt as render_profile
from app.knowledge.registry import load_condition_index
from app.memory import document as memory_document
from app.telemetry import record_fail_open
from app.triage.red_flags import symptom_mentions

logger = logging.getLogger("davi.chat")

# At most this many red-flag terms become episodes in one turn. A message
# listing many symptoms should not open many rows.
MAX_EPISODES_PER_TURN = 3


def _episode_terms(flags: list[str] | None) -> list[str]:
    """Real symptom phrases from triage's matched terms.

    Triage reports rule LABELS next to the phrases the reader actually used —
    "chest pain (pattern)" names the ACS co-occurrence rule. A label is not a
    symptom and must not become an episode: it inflates one incident into
    several rows, and the reader can never name it to close it.
    """
    return [t for t in (flags or []) if "(" not in t]



@dataclass(frozen=True)
class UserMemory:
    """What is known about this reader, rendered for the [P] block."""

    profile_text: str = ""
    episode_text: str = ""
    recall_text: str = ""
    episodes: list = field(default_factory=list)
    # True when this came from the stored document rather than a live
    # assembly. Recorded so the hit rate is observable rather than assumed.
    from_document: bool = False

    def blocks(self) -> list[str]:
        """The non-empty blocks, in the order they should appear."""
        return [b for b in (self.profile_text, self.episode_text, self.recall_text) if b]

    def append_to(self, patient_text: str) -> str:
        """Add this memory to an existing [P] block."""
        parts = [p for p in (patient_text, *self.blocks()) if p]
        return "\n\n".join(parts)


async def assemble(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    episodes_hint: list | None = None,
) -> UserMemory:
    """Read every per-user memory store. Each part fails open independently.

    Independently matters: a failure reading episodes must not also cost the
    reader their profile.

    ``episodes_hint`` — open episodes the caller has ALREADY read this turn.
    The orchestrator reads them in the prologue to compute the red-flag floor
    (an unresolved emergency episode must raise this turn's risk), and every
    round trip here is a network hop in production, so it hands them down
    rather than paying for them twice.
    """
    # A pending erasure stops the data being USED immediately, even though the
    # rows are destroyed later. Without this, "we have deleted your data" is
    # false for the whole grace period — the assistant would keep greeting the
    # reader with everything it knows about them.
    if await is_pending(db, user_id):
        return UserMemory()

    # The assembled document, when there is a fresh one. One primary-key read
    # instead of the twenty-odd this function otherwise issues.
    #
    # Falling back is ALWAYS safe: the live assembly below is what ran before
    # the document existed, and it is correct — just slower. So a missing,
    # stale or unreadable document costs latency, never an answer.
    try:
        row = await memory_document.get(db, user_id)
        if memory_document.is_fresh(row) and row is not None and row.prompt_block:
            return UserMemory(profile_text=row.prompt_block, from_document=True)
    except Exception:  # noqa: BLE001 — an optimisation must never break a turn
        logger.warning("memory document read failed; assembling live", exc_info=True)
        record_fail_open("memory_document")

    profile_text = ""
    try:
        profile_text = render_profile(await get_profile(db, user_id))
    except Exception:  # noqa: BLE001 — enrichment must never break a reply
        logger.warning("profile context failed; continuing", exc_info=True)
        record_fail_open("profile")

    episodes: list = []
    episode_text = ""
    try:
        episodes = (
            episodes_hint if episodes_hint is not None
            else await open_episodes(db, user_id)
        )
        episode_text = render_episodes(episodes)
    except Exception:  # noqa: BLE001
        logger.warning("episode context failed; continuing", exc_info=True)
        record_fail_open("episodes")

    recall_text = ""
    try:
        recall_text = await recall(db, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("long-term recall failed; continuing", exc_info=True)
        record_fail_open("long_term_recall")

    return UserMemory(
        profile_text=profile_text,
        episode_text=episode_text,
        recall_text=recall_text,
        episodes=episodes,
    )


async def _display_names(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, str]:
    """Condition code -> human-readable name, falling back to the code.

    Resolved HERE rather than by each caller. `recall()` renders the stored
    value verbatim, so a caller that stored the raw code would have the
    assistant tell a reader "you previously asked about: MC001". The registry
    index is process-cached, so this is not a per-turn query.
    """
    codes = list(codes)
    if not codes:
        return {}
    try:
        index = await load_condition_index(db)
    except Exception:  # noqa: BLE001 — a display name is not worth a failure
        logger.warning("condition index unavailable; using codes", exc_info=True)
        index = None
    return {
        code: (
            index.by_code[code].display_name
            if index and code in index.by_code
            else code
        )
        for code in codes
    }


async def record(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    codes: Iterable[str] = (),
    flags: list[str] | None = None,
    risk: str = "none",
    message: str = "",
) -> None:
    """Write what this turn should be remembered for. Fails open.

    ``risk`` is the level the deterministic TRIAGE FLOOR decided, never one the
    model inferred — an episode's severity must not be something a reply talked
    its way into.
    """
    # Nothing new is remembered about someone who has asked to be forgotten.
    if await is_pending(db, user_id):
        return

    topics = await _display_names(db, codes)
    if topics:
        try:
            await record_topics(db, user_id, topics, flags=flags or [])
        except Exception:  # noqa: BLE001
            logger.warning("topic recording failed; continuing", exc_info=True)
            record_fail_open("record_topics")

    # Close-out before open: "my chest pain is better now" mentions the
    # symptom AND says it improved. resolve() had NO caller (audit high), so
    # reporting recovery only re-touched the episode — the [P] block kept
    # asserting an unresolved symptom for two more weeks, and telling Davi
    # you were better could only EXTEND that.
    if message and is_recovery_message(message, has_red_flag=bool(flags)):
        resolved_any = False
        for term in (flags or []):
            try:
                if await resolve(db, user_id, term):
                    resolved_any = True
            except Exception:  # noqa: BLE001
                logger.warning("episode resolve failed; continuing",
                               exc_info=True)
        if not resolved_any and not flags:
            # "I'm feeling better" with no symptom named closes EVERY open
            # episode.
            #
            # This used to close one only when exactly one was open, on the
            # reasoning that guessing among several would be wrong more often
            # than right. That reasoning changed when open episodes started
            # raising the risk floor: one message routinely opens several rows
            # for a SINGLE incident ("chest pain and left arm discomfort" ->
            # chest pain + left arm), so the guard meant the reader could never
            # close it. Measured in staging: "its gone.. i am feeling better"
            # resolved nothing and every later turn — including "what is
            # diabetes?" — kept the seek-care banner for fourteen days.
            #
            # Closing too eagerly costs a floor the reader explicitly asked to
            # drop, and they can re-report in one sentence. Not closing at all
            # costs an escalation they cannot escape, which is how a real
            # warning gets trained into wallpaper.
            try:
                episodes = await open_episodes(db, user_id)
                for episode in episodes:
                    await resolve(db, user_id, episode.symptom)
                if episodes:
                    logger.info(
                        "recovery message closed %d open episode(s)",
                        len(episodes),
                    )
            except Exception:  # noqa: BLE001
                logger.warning("episode resolve failed; continuing",
                               exc_info=True)
        return  # a recovery report never opens or extends an episode

    # Triage returns rule LABELS alongside real phrases — "chest pain
    # (pattern)" is the name of the ACS co-occurrence rule, not something the
    # reader said. Opening an episode for it created a third row for one
    # incident, which is what pushed the count past the close-out guard below.
    # The whole turn's matched terms ride along on each row, so the history
    # keeps the CO-OCCURRENCE: "chest pain" logged beside "left arm" is a
    # different report from "chest pain" alone, and the pair is the thing a
    # clinician would want to see. Labels are kept here (unlike the episode
    # terms) because a rule that fired is part of what happened.
    turn_terms = list(flags or [])
    episode_terms = _episode_terms(flags)[:MAX_EPISODES_PER_TURN]
    for term in episode_terms:
        try:
            await open_or_touch(db, user_id, term, risk, turn_terms)
        except Exception:  # noqa: BLE001
            logger.warning("episode recording failed; continuing", exc_info=True)
            record_fail_open("record_episode")

    # Ordinary symptoms — history ONLY, no episode, no floor. Triage has three
    # levels and every one of its tables escalates, so `flags` holds red flags
    # and nothing else: a headache, a cough or three days of nausea matched
    # nothing and went unrecorded. `symptom_mentions` is the non-escalating
    # table that fixes that, and routing it here rather than into `flags` is
    # what keeps a headache from raising anyone's floor.
    #
    # Deduped against the episode terms so one complaint is not recorded twice
    # under two spellings of itself.
    already = {t.lower() for t in episode_terms}
    for term in symptom_mentions(message)[:MAX_EPISODES_PER_TURN]:
        if term.lower() in already:
            continue
        try:
            await log_symptom(db, user_id, term, turn_terms or None)
        except Exception:  # noqa: BLE001
            logger.warning("symptom logging failed; continuing", exc_info=True)
            record_fail_open("log_symptom")

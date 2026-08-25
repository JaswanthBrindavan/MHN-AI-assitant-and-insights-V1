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

from app.chat.episodes import open_episodes, open_or_touch
from app.chat.episodes import render_for_prompt as render_episodes
from app.chat.long_term import recall, record_topics
from app.chat.profile import get_profile
from app.chat.profile import render_for_prompt as render_profile
from app.knowledge.registry import load_condition_index
from app.telemetry import record_fail_open

logger = logging.getLogger("davi.chat")

# At most this many red-flag terms become episodes in one turn. A message
# listing many symptoms should not open many rows.
MAX_EPISODES_PER_TURN = 3


@dataclass(frozen=True)
class UserMemory:
    """What is known about this reader, rendered for the [P] block."""

    profile_text: str = ""
    episode_text: str = ""
    recall_text: str = ""
    episodes: list = field(default_factory=list)

    def blocks(self) -> list[str]:
        """The non-empty blocks, in the order they should appear."""
        return [b for b in (self.profile_text, self.episode_text, self.recall_text) if b]

    def append_to(self, patient_text: str) -> str:
        """Add this memory to an existing [P] block."""
        parts = [p for p in (patient_text, *self.blocks()) if p]
        return "\n\n".join(parts)


async def assemble(db: AsyncSession, user_id: uuid.UUID) -> UserMemory:
    """Read every per-user memory store. Each part fails open independently.

    Independently matters: a failure reading episodes must not also cost the
    reader their profile.
    """
    profile_text = ""
    try:
        profile_text = render_profile(await get_profile(db, user_id))
    except Exception:  # noqa: BLE001 — enrichment must never break a reply
        logger.warning("profile context failed; continuing", exc_info=True)
        record_fail_open("profile")

    episodes: list = []
    episode_text = ""
    try:
        episodes = await open_episodes(db, user_id)
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
) -> None:
    """Write what this turn should be remembered for. Fails open.

    ``risk`` is the level the deterministic TRIAGE FLOOR decided, never one the
    model inferred — an episode's severity must not be something a reply talked
    its way into.
    """
    topics = await _display_names(db, codes)
    if topics:
        try:
            await record_topics(db, user_id, topics, flags=flags or [])
        except Exception:  # noqa: BLE001
            logger.warning("topic recording failed; continuing", exc_info=True)
            record_fail_open("record_topics")

    for term in (flags or [])[:MAX_EPISODES_PER_TURN]:
        try:
            await open_or_touch(db, user_id, term, risk)
        except Exception:  # noqa: BLE001
            logger.warning("episode recording failed; continuing", exc_info=True)
            record_fail_open("record_episode")

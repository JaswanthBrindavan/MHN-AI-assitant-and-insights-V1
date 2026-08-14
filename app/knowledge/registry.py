"""Condition-registry lookup: data-driven scoping and code mapping.

Loads the clinically-validated condition registry (display names + AKA
aliases) once per process and builds a compiled keyword index used to scope
retrieval. Falls back to the legacy static keyword map when the registry table
is empty (e.g. unit tests that never ingest the corpus). Fail-open: any
registry error degrades to the static fallback, never breaks a reply.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import ConditionRegistry

logger = logging.getLogger("davi.knowledge")

# Keywords shorter than this are ignored — they over-match plain English.
MIN_KEYWORD_LEN = 4

# A few corpus aliases are common English words that would over-match.
_ALIAS_STOPLIST = {
    "cold", "flu", "pain", "fever", "cough", "sugar", "pressure", "stress",
    "sleep", "weight", "heart", "blood", "skin", "hair", "eye", "ear",
    "gas", "acid", "piles", "worm", "mole", "wart", "corn", "stone",
}


@dataclass(frozen=True)
class RegistryEntry:
    condition_code: str
    display_name: str
    aliases: tuple[str, ...]
    engine_codes: tuple[str, ...]


class ConditionIndex:
    """Compiled keyword → condition-code index."""

    def __init__(self, entries: list[RegistryEntry]):
        self.entries = entries
        self.by_code = {e.condition_code: e for e in entries}
        self.engine_map: dict[str, str] = {}
        for e in entries:
            for legacy in e.engine_codes:
                self.engine_map[legacy.upper()] = e.condition_code
        self._patterns: list[tuple[re.Pattern[str], str]] = []
        seen: set[tuple[str, str]] = set()
        for e in entries:
            # Display name, its parenthetical ABBREVIATIONS ("(GERD)"), aliases.
            # Only uppercase-dominated tokens count — qualifiers like
            # "(child)" or "(pediatric)" must not become keywords.
            keywords = [e.display_name, *e.aliases]
            for inner in re.findall(r"\(([^)]{2,40})\)", e.display_name):
                inner = inner.strip()
                if re.fullmatch(r"[A-Z][A-Za-z0-9\-]{1,11}", inner) and sum(
                    ch.isupper() for ch in inner
                ) >= 2:
                    keywords.append(inner)
            for kw in keywords:
                kw_stripped = kw.strip()
                kw_clean = kw_stripped.lower()
                # Drop parenthetical qualifiers for matching purposes.
                kw_clean = re.sub(r"\s*\([^)]*\)", "", kw_clean).strip()
                # All-caps medical abbreviations (PMS, CIN, UTI) may be 3 chars;
                # everything else needs MIN_KEYWORD_LEN.
                is_abbrev = kw_stripped.isupper() and len(kw_clean) >= 3
                if len(kw_clean) < MIN_KEYWORD_LEN and not is_abbrev:
                    continue
                if kw_clean in _ALIAS_STOPLIST:
                    continue
                key = (kw_clean, e.condition_code)
                if key in seen:
                    continue
                seen.add(key)
                # Tolerate a simple English plural on the final word
                # ("migraines" → "migraine", "ulcers" → "ulcer").
                # 3-char abbreviations match CASE-SENSITIVELY: "ARM" (age-
                # related maculopathy) must not fire on the word "arm".
                if is_abbrev and len(kw_clean) == 3:
                    pattern = re.compile(r"\b" + re.escape(kw_stripped) + r"\b")
                else:
                    pattern = re.compile(
                        r"\b" + re.escape(kw_clean) + r"(?:e?s)?\b", re.IGNORECASE
                    )
                self._patterns.append((pattern, e.condition_code))

    def match_message(self, message: str) -> set[str]:
        """Condition codes whose display name or an alias appears in the text."""
        found: set[str] = set()
        for pattern, code in self._patterns:
            if code in found:
                continue
            if pattern.search(message):
                found.add(code)
        return found

    def map_engine_codes(self, codes: set[str]) -> set[str]:
        """Translate legacy engine codes (T2DM…) to registry codes; keep both."""
        out = set(codes)
        for code in codes:
            mapped = self.engine_map.get(code.upper())
            if mapped:
                out.add(mapped)
        return out


# Process-level cache. Reset with reset_index_cache() (tests, post-ingest).
_index_cache: ConditionIndex | None = None
_cache_loaded = False


def reset_index_cache() -> None:
    global _index_cache, _cache_loaded
    _index_cache = None
    _cache_loaded = False


async def load_condition_index(db: AsyncSession) -> ConditionIndex | None:
    """Return the cached index, loading once. None → registry empty/unavailable."""
    global _index_cache, _cache_loaded
    if _cache_loaded:
        return _index_cache
    try:
        rows = (
            await db.execute(
                select(ConditionRegistry).where(ConditionRegistry.active.is_(True))
            )
        ).scalars().all()
        if rows:
            entries = [
                RegistryEntry(
                    condition_code=r.condition_code,
                    display_name=r.display_name,
                    aliases=tuple(r.aliases or []),
                    engine_codes=tuple(r.engine_codes or []),
                )
                for r in rows
            ]
            _index_cache = ConditionIndex(entries)
        else:
            _index_cache = None
        _cache_loaded = True
    except Exception:  # noqa: BLE001 — registry must never break a reply
        logger.warning("condition registry load failed; static fallback", exc_info=True)
        _index_cache = None
        # Leave _cache_loaded False so a transient error can recover later.
    return _index_cache

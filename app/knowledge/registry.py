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
# Single-word aliases (not display names, not abbreviations) face a stricter
# minimum: 4–5-char English words ("safe", "fast", "tight") hijack retrieval.
MIN_SINGLE_WORD_ALIAS_LEN = 6
# A keyword mapping to this many distinct conditions is enumeration debris
# ("hindi" appeared as an alias of 22 conditions) — drop it entirely.
AMBIGUITY_LIMIT = 3

# Corpus aliases that are common English words and would over-match everyday
# messages (verified against the live 511-condition registry). DRAFT.
_ALIAS_STOPLIST = {
    "cold", "flu", "pain", "fever", "cough", "sugar", "pressure", "stress",
    "sleep", "weight", "heart", "blood", "skin", "hair", "eye", "ear",
    "gas", "acid", "piles", "worm", "mole", "wart", "corn", "stone",
    "safe", "unsafe", "primary", "secondary", "tertiary", "complete",
    "incomplete", "inevitable", "habitual", "drinking", "smoking", "eating",
    "fast", "slow", "tight", "loose", "wrist", "spine", "children", "child",
    "adults", "adult", "male", "female", "battle", "goddess", "eggs",
    "common", "misnomer", "atrophic", "chronic", "acute", "severe", "mild",
    "moderate", "benign", "malignant", "silent", "hidden", "simple",
    "partial", "total", "early", "late", "false", "true", "wet", "dry",
    "burning", "itching", "swelling", "growth", "spots", "patches",
    "attack", "failure", "disease", "disorder", "syndrome", "infection",
    "deficiency", "excess",
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
        self._diagnostic_terms: tuple[str, ...] | None = None
        self.engine_map: dict[str, str] = {}
        for e in entries:
            for legacy in e.engine_codes:
                self.engine_map[legacy.upper()] = e.condition_code
        # First pass: collect candidate (keyword, code, flags) then apply the
        # ambiguity filter (a keyword naming many conditions is debris).
        candidates: list[tuple[str, str, str, bool]] = []  # clean, orig, code, from_display
        seen: set[tuple[str, str]] = set()
        for e in entries:
            # Display name, its parenthetical ABBREVIATIONS ("(GERD)"), aliases.
            # Only uppercase-dominated tokens count — qualifiers like
            # "(child)" or "(pediatric)" must not become keywords.
            keywords: list[tuple[str, bool]] = [(e.display_name, True)]
            for inner in re.findall(r"\(([^)]{2,40})\)", e.display_name):
                inner = inner.strip()
                if re.fullmatch(r"[A-Z][A-Za-z0-9\-]{1,11}", inner) and sum(
                    ch.isupper() for ch in inner
                ) >= 2:
                    keywords.append((inner, True))
            keywords += [(a, False) for a in e.aliases]

            for kw, from_display in keywords:
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
                # Single-word ALIASES (not display names / abbreviations) are
                # the main hijack vector — require a longer minimum.
                if (
                    not from_display
                    and not is_abbrev
                    and " " not in kw_clean
                    and len(kw_clean) < MIN_SINGLE_WORD_ALIAS_LEN
                ):
                    continue
                key = (kw_clean, e.condition_code)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((kw_clean, kw_stripped, e.condition_code, is_abbrev))

        # Ambiguity filter: a keyword claimed by ≥AMBIGUITY_LIMIT conditions
        # is enumeration debris, never a usable name.
        counts: dict[str, set[str]] = {}
        for kw_clean, _orig, code, _abbr in candidates:
            counts.setdefault(kw_clean, set()).add(code)

        self._patterns = []
        for kw_clean, kw_stripped, code, is_abbrev in candidates:
            if len(counts[kw_clean]) >= AMBIGUITY_LIMIT:
                continue
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
            self._patterns.append((pattern, code))

    def diagnostic_terms(self) -> tuple[str, ...]:
        """Cleaned condition names for the output validator's dynamic lexicon.

        Display names are emitted both verbatim-minus-parentheticals and as
        their base form ("Heart Failure (HFrEF HFpEF)" → "heart failure"), and
        aliases are included — so "you have pertussis" is caught even though
        the display name is "Pertussis (whooping cough)".
        """
        if self._diagnostic_terms is None:
            terms: set[str] = set()
            for e in self.entries:
                for raw in (e.display_name, *e.aliases):
                    base = re.sub(r"\s*\([^)]*\)", "", raw).strip().lower()
                    if len(base) >= 4:
                        terms.add(base)
                    for inner in re.findall(r"\(([^)]{2,40})\)", raw):
                        inner_clean = inner.strip().lower()
                        if len(inner_clean) >= 4 and "/" not in inner_clean:
                            terms.add(inner_clean)
            self._diagnostic_terms = tuple(sorted(terms))
        return self._diagnostic_terms

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

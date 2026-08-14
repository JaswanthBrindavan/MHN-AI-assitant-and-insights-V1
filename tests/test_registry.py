"""Condition-registry index + data-driven retrieval scoping.

Covers app/knowledge/registry.py (ConditionIndex, load_condition_index,
reset_index_cache) and the resolve_scope path in app/rag/retrieval.py.
Index behaviour is tested against hand-built RegistryEntry lists; the loader
and resolve_scope are tested against real ConditionRegistry rows in sqlite.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.registry import (
    MIN_KEYWORD_LEN,
    ConditionIndex,
    RegistryEntry,
    load_condition_index,
    reset_index_cache,
)
from app.models.knowledge import ConditionRegistry
from app.rag.retrieval import resolve_scope, scope_codes


def entry(
    code: str,
    display: str,
    aliases: tuple[str, ...] = (),
    engine: tuple[str, ...] = (),
) -> RegistryEntry:
    return RegistryEntry(
        condition_code=code,
        display_name=display,
        aliases=tuple(aliases),
        engine_codes=tuple(engine),
    )


GOUT = entry("MC-GOUT", "Gout")
TYPHOID = entry("MC-TYPHOID", "Typhoid Fever", aliases=("Typhoid", "Enteric Fever"))
FLU = entry("MC-FLU", "Influenza", aliases=("Flu",))
TB = entry("MC-TB", "Tuberculosis", aliases=("TB", "  Kshaya  "))
COLD = entry("MC-COLD", "Common Cold", aliases=("Cold",))
PILES = entry("MC-PILES", "Haemorrhoids", aliases=("Piles (Haemorrhoids)",))
UTI = entry("MC-UTI", "Urinary Tract Infection", aliases=("UTI",))
THAL = entry("MC-THAL", "Beta-Thalassemia")
OA = entry("MC-OA", "Osteoarthritis", aliases=("Sandhivata",))
RA = entry("MC-RA", "Rheumatoid Arthritis", aliases=("Sandhivata",))
DM = entry(
    "MC-DM",
    "Type 2 Diabetes Mellitus",
    aliases=("Madhumeha (Sugar Urine Disease)", "मधुमेह"),
    engine=("T2DM",),
)
HTN_ENTRY = entry("MC-HTN", "Hypertension", engine=("HTN", "htn2"))

INDEX = ConditionIndex(
    [GOUT, TYPHOID, FLU, TB, COLD, PILES, UTI, THAL, OA, RA, DM, HTN_ENTRY]
)


# --------------------------------------------------------------------------- #
# Constructor: keyword filtering and compiled structures
# --------------------------------------------------------------------------- #
def test_min_keyword_len_constant():
    assert MIN_KEYWORD_LEN == 4


def test_by_code_maps_every_entry():
    assert INDEX.by_code["MC-DM"] is DM
    assert INDEX.by_code["MC-GOUT"] is GOUT
    assert set(INDEX.by_code) == {e.condition_code for e in INDEX.entries}


def test_engine_map_keys_are_uppercased():
    assert INDEX.engine_map["T2DM"] == "MC-DM"
    assert INDEX.engine_map["HTN"] == "MC-HTN"
    # Registry stored the code lowercase; the map key is uppercased.
    assert INDEX.engine_map["HTN2"] == "MC-HTN"
    assert "htn2" not in INDEX.engine_map


def test_duplicate_keywords_deduped_per_condition():
    idx = ConditionIndex(
        [entry("MC-X", "Dengue", aliases=("Dengue", " DENGUE ", "dengue"))]
    )
    assert len(idx._patterns) == 1
    assert idx.match_message("dengue fever") == {"MC-X"}


def test_blank_and_paren_only_aliases_are_skipped():
    idx = ConditionIndex([entry("MC-Z", "Malaria", aliases=("", "   ", "(P. vivax)"))])
    # Only the display name survives filtering.
    assert len(idx._patterns) == 1
    assert idx.match_message("malaria prophylaxis") == {"MC-Z"}
    assert idx.match_message("p. vivax") == set()


def test_empty_index_matches_nothing_and_passes_codes_through():
    idx = ConditionIndex([])
    assert idx.match_message("typhoid gout diabetes") == set()
    assert idx.map_engine_codes({"T2DM"}) == {"T2DM"}
    assert idx.by_code == {}
    assert idx.engine_map == {}


def test_entry_with_all_keywords_filtered_still_maps_engine_codes():
    # "Flu" is stoplisted, "TB" is too short: no patterns, but the engine
    # mapping and by_code entry must still exist.
    idx = ConditionIndex([entry("MC-Y", "Flu", aliases=("TB",), engine=("FLUX",))])
    assert idx._patterns == []
    assert idx.match_message("flu and tb") == set()
    assert "MC-Y" in idx.by_code
    assert idx.map_engine_codes({"flux"}) == {"flux", "MC-Y"}


# --------------------------------------------------------------------------- #
# match_message: word boundaries
# --------------------------------------------------------------------------- #
def test_alias_matches_inside_longer_phrase():
    # \btyphoid\b matches within "typhoid fever ...".
    assert "MC-TYPHOID" in INDEX.match_message("typhoid fever outbreak nearby")


def test_alias_matches_exact_word():
    assert INDEX.match_message("typhoid") == {"MC-TYPHOID"}


def test_gout_does_not_match_gouty():
    # No word boundary between "gout" and the trailing "y".
    assert INDEX.match_message("a gouty toe") == set()
    assert INDEX.match_message("gouty arthritis flare") == set()


def test_gout_matches_with_adjacent_punctuation():
    assert INDEX.match_message("gout") == {"MC-GOUT"}
    assert INDEX.match_message("gout!") == {"MC-GOUT"}
    assert INDEX.match_message("(gout)") == {"MC-GOUT"}
    assert INDEX.match_message("is it gout, doctor?") == {"MC-GOUT"}


def test_keyword_not_matched_as_substring_of_longer_word():
    assert INDEX.match_message("typhoidal illness") == set()


def test_hyphenated_display_name_is_regex_escaped():
    assert INDEX.match_message("beta-thalassemia screening") == {"MC-THAL"}
    # The hyphen is literal: a space variant or fragment must not match.
    assert INDEX.match_message("beta thalassemia") == set()
    assert INDEX.match_message("thalassemia") == set()


# --------------------------------------------------------------------------- #
# match_message: stoplist and length filtering
# --------------------------------------------------------------------------- #
def test_flu_alias_dropped_by_stoplist():
    assert INDEX.match_message("i have the flu") == set()
    assert INDEX.match_message("influenza season") == {"MC-FLU"}


def test_cold_alias_dropped_but_display_name_matches():
    assert INDEX.match_message("it is cold outside") == set()
    assert INDEX.match_message("common cold remedies") == {"MC-COLD"}


def test_short_alias_never_matches():
    # "TB" (2 chars) is below MIN_KEYWORD_LEN.
    assert INDEX.match_message("tb test tomorrow") == set()
    assert INDEX.match_message("tuberculosis symptoms") == {"MC-TB"}


def test_three_char_alias_never_matches():
    # "UTI" (3 chars) is below MIN_KEYWORD_LEN; the display name still works.
    assert INDEX.match_message("recurring uti") == set()
    assert INDEX.match_message("urinary tract infection symptoms") == {"MC-UTI"}


def test_alias_whitespace_is_stripped():
    assert INDEX.match_message("kshaya treatment") == {"MC-TB"}


def test_parenthetical_stripped_alias_falls_into_stoplist():
    # "Piles (Haemorrhoids)" cleans to "piles", which is stoplisted, so the
    # alias produces no pattern at all.
    assert INDEX.match_message("i have piles") == set()
    assert INDEX.match_message("haemorrhoids treatment") == {"MC-PILES"}


def test_parenthetical_stripped_alias_matches_base_word():
    # "Madhumeha (Sugar Urine Disease)" cleans to "madhumeha".
    assert INDEX.match_message("madhumeha diet plan") == {"MC-DM"}
    # The parenthetical text itself is discarded and never matches.
    assert INDEX.match_message("sugar urine disease") == set()


# --------------------------------------------------------------------------- #
# match_message: case, unicode, multiplicity, degenerate input
# --------------------------------------------------------------------------- #
def test_case_insensitive_matching():
    assert INDEX.match_message("TYPHOID FEVER") == {"MC-TYPHOID"}
    assert INDEX.match_message("TyPhOiD") == {"MC-TYPHOID"}
    assert INDEX.match_message("GOUT ATTACK") == {"MC-GOUT"}


def test_unicode_alias_matches():
    assert INDEX.match_message("मुझे मधुमेह है") == {"MC-DM"}


def test_multiple_conditions_in_one_message():
    got = INDEX.match_message("gout and typhoid fever after my influenza")
    assert got == {"MC-GOUT", "MC-TYPHOID", "MC-FLU"}


def test_same_alias_on_two_conditions_returns_both():
    assert INDEX.match_message("sandhivata pain in the knees") == {"MC-OA", "MC-RA"}


def test_display_name_matching():
    assert INDEX.match_message("does hypertension run in families") == {"MC-HTN"}


def test_empty_message():
    assert INDEX.match_message("") == set()


def test_punctuation_only_message():
    assert INDEX.match_message("!!! ??? ... --- ,,, ;;;") == set()


def test_unrelated_message_matches_nothing():
    assert INDEX.match_message("hello how are you today") == set()


# --------------------------------------------------------------------------- #
# map_engine_codes
# --------------------------------------------------------------------------- #
def test_map_known_code_uppercase():
    assert INDEX.map_engine_codes({"T2DM"}) == {"T2DM", "MC-DM"}


def test_map_known_code_lowercase_keeps_original_spelling():
    assert INDEX.map_engine_codes({"t2dm"}) == {"t2dm", "MC-DM"}


def test_map_unknown_code_passthrough():
    assert INDEX.map_engine_codes({"XYZ"}) == {"XYZ"}


def test_map_empty_set():
    assert INDEX.map_engine_codes(set()) == set()


def test_map_mixed_known_and_unknown():
    got = INDEX.map_engine_codes({"t2dm", "XYZ", "HTN", "htn2"})
    assert got == {"t2dm", "XYZ", "HTN", "htn2", "MC-DM", "MC-HTN"}


def test_map_does_not_mutate_input():
    codes = {"T2DM"}
    INDEX.map_engine_codes(codes)
    assert codes == {"T2DM"}


def test_map_mc_code_input_is_passthrough():
    # An already-mapped MC code is not in the engine map: unchanged.
    assert INDEX.map_engine_codes({"MC-DM"}) == {"MC-DM"}


# --------------------------------------------------------------------------- #
# load_condition_index: loading, caching, active filter, fail-open
# --------------------------------------------------------------------------- #
async def _seed(
    db,
    code: str,
    display: str,
    aliases: list | None = None,
    engine_codes: list | None = None,
    active: bool = True,
) -> None:
    db.add(
        ConditionRegistry(
            condition_code=code,
            display_name=display,
            aliases=aliases,
            engine_codes=engine_codes,
            active=active,
        )
    )
    await db.commit()


async def test_load_empty_table_returns_none(db_session):
    assert await load_condition_index(db_session) is None


async def test_load_builds_index_from_rows(db_session):
    await _seed(
        db_session, "MC-DM2", "Type 2 Diabetes Mellitus",
        aliases=["Madhumeha"], engine_codes=["T2DM"],
    )
    await _seed(db_session, "MC-TYPH", "Typhoid Fever")  # NULL aliases/engine codes
    idx = await load_condition_index(db_session)
    assert isinstance(idx, ConditionIndex)
    assert set(idx.by_code) == {"MC-DM2", "MC-TYPH"}
    assert idx.by_code["MC-TYPH"].aliases == ()
    assert idx.by_code["MC-TYPH"].engine_codes == ()
    assert idx.match_message("madhumeha and typhoid fever") == {"MC-DM2", "MC-TYPH"}
    assert idx.map_engine_codes({"T2DM"}) == {"T2DM", "MC-DM2"}


async def test_load_returns_cached_object_across_calls(db_session):
    await _seed(db_session, "MC-A", "Asthma")
    idx1 = await load_condition_index(db_session)
    idx2 = await load_condition_index(db_session)
    assert idx1 is not None
    assert idx1 is idx2


async def test_empty_result_is_cached_until_reset(db_session):
    assert await load_condition_index(db_session) is None
    await _seed(db_session, "MC-A", "Asthma")
    # The empty result was cached: still None without a reset.
    assert await load_condition_index(db_session) is None
    reset_index_cache()
    idx = await load_condition_index(db_session)
    assert idx is not None
    assert "MC-A" in idx.by_code


async def test_reset_index_cache_forces_reload(db_session):
    await _seed(db_session, "MC-A", "Asthma")
    idx1 = await load_condition_index(db_session)
    reset_index_cache()
    idx2 = await load_condition_index(db_session)
    assert idx1 is not None and idx2 is not None
    assert idx1 is not idx2


async def test_inactive_rows_excluded(db_session):
    await _seed(db_session, "MC-A", "Asthma", active=True)
    await _seed(db_session, "MC-B", "Bronchitis", active=False)
    idx = await load_condition_index(db_session)
    assert idx is not None
    assert "MC-B" not in idx.by_code
    assert idx.match_message("bronchitis") == set()
    assert idx.match_message("asthma") == {"MC-A"}


async def test_all_rows_inactive_returns_none(db_session):
    await _seed(db_session, "MC-B", "Bronchitis", active=False)
    assert await load_condition_index(db_session) is None


class _BrokenDB:
    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("db unavailable")


async def test_load_error_fails_open_and_recovers(db_session):
    # A failing session degrades to None (static fallback)...
    assert await load_condition_index(cast(AsyncSession, _BrokenDB())) is None
    # ...and does NOT poison the cache: the next good call loads the rows.
    await _seed(db_session, "MC-A", "Asthma")
    idx = await load_condition_index(db_session)
    assert idx is not None
    assert "MC-A" in idx.by_code


# --------------------------------------------------------------------------- #
# resolve_scope: registry-driven scoping with static fallback
# --------------------------------------------------------------------------- #
async def test_resolve_scope_empty_registry_equals_legacy(db_session):
    message = "tell me about diabetes"
    users = {"HTN"}
    got = await resolve_scope(db_session, message, users)
    assert got == scope_codes(message, users)
    assert got == {"T2DM", "HTN"}


async def test_resolve_scope_empty_registry_no_hits(db_session):
    assert await resolve_scope(db_session, "hello there", set()) == set()


async def test_resolve_scope_maps_aliases_and_engine_codes(db_session):
    await _seed(
        db_session, "MC-DM2", "Type 2 Diabetes Mellitus",
        aliases=["Madhumeha"], engine_codes=["T2DM"],
    )
    got = await resolve_scope(db_session, "my madhumeha is flaring", {"T2DM"})
    # Alias hit + user's legacy code kept and mapped.
    assert got == {"MC-DM2", "T2DM"}


async def test_resolve_scope_static_extraction_still_contributes(db_session):
    await _seed(db_session, "MC-TYPH", "Typhoid Fever")
    got = await resolve_scope(db_session, "worried about my blood sugar", set())
    # The registry is active but has no hit; the static map still fires.
    assert got == {"T2DM"}


async def test_resolve_scope_display_name_and_unknown_user_code(db_session):
    await _seed(db_session, "MC-TYPH", "Typhoid", aliases=["Enteric Fever"])
    got = await resolve_scope(db_session, "typhoid fever symptoms", {"CUSTOM"})
    assert got == {"MC-TYPH", "CUSTOM"}


async def test_resolve_scope_combines_registry_engine_and_static(db_session):
    await _seed(
        db_session, "MC-DM2", "Type 2 Diabetes Mellitus",
        aliases=["Madhumeha"], engine_codes=["T2DM"],
    )
    await _seed(db_session, "MC-HTN2", "Hypertension", engine_codes=["HTN"])
    got = await resolve_scope(
        db_session, "madhumeha and high blood pressure", {"HTN"}
    )
    # Alias hit + engine mapping (HTN kept AND mapped) + static "blood pressure".
    assert got == {"MC-DM2", "HTN", "MC-HTN2"}

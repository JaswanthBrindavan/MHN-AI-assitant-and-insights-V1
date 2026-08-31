"""Section-targeted MCP answers, brevity, and progressive disclosure.

These pin three defects that shipped together and were measured, not assumed:

1. "what is diabetes" rendered the top 3 chunks by rank — 1,432 chars with the
   definition landing THIRD, because a 111-char `prevalence` chunk scored
   0.1429 on header hits while the 839-char `symptoms` chunk scored 0.0096.
2. There was no progressive disclosure at all.
3. Section intent was a flat +0.05 nudge and never a filter, so
   "what are the symptoms of X" returned a reply BYTE-IDENTICAL to "what is X".

Pure unit tests: `target_sections`, `_prefer_section` and
`build_extractive_answer` are all side-effect free, so none of this needs a
database.
"""

from __future__ import annotations

import pytest

from app.rag.extractive import (
    _MENU_SECTIONS,
    build_extractive_answer,
    is_focused,
    rendered_chunks,
)
from app.rag.retrieval import RetrievedChunk, _prefer_section, target_sections


def _chunk(section: str, body: str = "", score: float = 0.0) -> RetrievedChunk:
    text = body or ("A clinically reviewed sentence about the topic. " * 2)
    return RetrievedChunk(
        id=section,
        condition_code="MC001",
        chunk_type=section,
        content=f"Diabetes mellitus — {section}:\n{text}",
        score=score,
    )


# --------------------------------------------------------------------------
# target_sections
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("message", "expected_first"),
    [
        ("what is diabetes", "definition"),
        ("what are the symptoms of diabetes", "symptoms"),
        ("how is diabetes diagnosed", "diagnosis"),
        ("what causes diabetes", "etiology"),
        ("what are the complications of diabetes", "complications"),
        # Sections that previously had no stem at all and so scored 0.0
        # on every chunk.
        ("how common is diabetes", "prevalence"),
        ("what types of diabetes are there", "classification"),
        ("what are the red flags for diabetes", "signs"),
        ("what conditions are associated with diabetes", "associated_conditions"),
        ("what triggers a diabetes flare", "lifestyle_triggers"),
    ],
)
def test_every_question_shape_selects_its_section(message, expected_first):
    assert target_sections(message)[0] == expected_first


def test_a_generic_opener_does_not_drag_definition_into_a_section_query():
    """The regression that made the fix look like it worked when it did not.

    "what are the symptoms of X" contains the generic opener "what are". While
    the openers were unioned with the specific stems, `definition` came back in
    the tuple, outranked `symptoms`, and the reply went back to being identical
    to the definition answer.
    """
    sections = target_sections("what are the symptoms of diabetes")
    assert "definition" not in sections
    assert sections[0] == "symptoms"


def test_generic_opener_still_applies_when_nothing_specific_is_asked():
    assert target_sections("what is diabetes") == ("definition",)
    assert target_sections("tell me about diabetes") == ("definition",)


def test_a_compound_question_keeps_both_halves():
    sections = target_sections(
        "what are the symptoms and complications of diabetes"
    )
    assert "symptoms" in sections
    assert "complications" in sections


def test_stems_are_word_anchored():
    """`"test" in message` also fired on "greatest"/"latest"."""
    assert "tests_quantitative" not in target_sections("the greatest of these")
    assert "tests_quantitative" in target_sections("what tests are used")


@pytest.mark.parametrize(
    ("message", "must_not_contain"),
    [
        # Prefix collisions that were harmless as a +0.05 nudge but DISCARD the
        # right chunks now that the table is a hard filter.
        ("is this a significant change", "signs"),
        ("what is my testosterone level", "tests_quantitative"),
        # Everyday words that were tried as stems and removed: a symptom
        # question must not be re-pointed at lab-reference sections.
        ("is this normal", "tests_quantitative"),
        ("i get a range of symptoms", "tests_quantitative"),
    ],
)
def test_everyday_words_do_not_hijack_the_filter(message, must_not_contain):
    assert must_not_contain not in target_sections(message)


def test_risk_factor_questions_are_not_answered_with_the_definition():
    """`is_definitional_ask` whitelists this phrasing for no-LLM service.

    Without a `risk factor` stem it fell to the generic "what are" opener and
    the reader got the definition, labelled as risk factors.
    """
    sections = target_sections("what are the risk factors for diabetes")
    assert sections[0] == "risk_profiles"
    assert "definition" not in sections


def test_no_section_named_returns_empty():
    assert target_sections("is it serious") == ()
    assert target_sections("") == ()


def test_target_sections_never_raises():
    for junk in ("", "   ", "?????", "\x00", "a" * 5000, "🙂"):
        assert isinstance(target_sections(junk), tuple)


# --------------------------------------------------------------------------
# _prefer_section
# --------------------------------------------------------------------------

def test_prefer_section_keeps_only_the_asked_for_section():
    ranked = [_chunk("prevalence"), _chunk("definition"), _chunk("symptoms")]
    kept = _prefer_section(ranked, ("symptoms",))
    assert [c.chunk_type for c in kept] == ["symptoms"]


def test_prefer_section_keeps_continuation_parts():
    ranked = [_chunk("symptoms"), _chunk("symptoms_2"), _chunk("prevalence")]
    kept = _prefer_section(ranked, ("symptoms",))
    assert [c.chunk_type for c in kept] == ["symptoms", "symptoms_2"]


def test_prefer_section_fails_open_when_the_section_is_absent():
    """A profile lacking the section degrades to the full ranking, never []."""
    ranked = [_chunk("prevalence"), _chunk("definition")]
    assert _prefer_section(ranked, ("symptoms",)) == ranked


def test_prefer_section_is_a_noop_without_a_target():
    ranked = [_chunk("prevalence"), _chunk("definition")]
    assert _prefer_section(ranked, ()) == ranked


# --------------------------------------------------------------------------
# build_extractive_answer — brevity and the disclosure menu
# --------------------------------------------------------------------------

def test_focused_answer_renders_one_section():
    """focused=True only ever sees chunks `_prefer_section` already filtered.

    `is_focused` is False unless the filter matched, and when it matched
    `_prefer_section` returned ONLY matching chunks — so a single-section ask
    reaches the renderer as a single-section list, continuation parts included.
    """
    chunks = [_chunk("definition"), _chunk("definition_2")]
    reply = build_extractive_answer(chunks, focused=True)
    assert reply is not None
    assert reply.count("**") == 2  # exactly one bolded section heading
    assert "What it is" in reply


def test_unfocused_answer_keeps_the_previous_three_section_shape():
    chunks = [_chunk("definition"), _chunk("prevalence"), _chunk("symptoms")]
    reply = build_extractive_answer(chunks, focused=False)
    assert reply is not None
    assert "What it is" in reply
    assert "How common it is" in reply


def test_focused_answer_is_shorter_than_the_unfocused_one():
    mixed = [_chunk("definition"), _chunk("prevalence"), _chunk("symptoms")]
    focused = build_extractive_answer([_chunk("definition")], focused=True)
    unfocused = build_extractive_answer(mixed, focused=False)
    assert focused is not None and unfocused is not None
    assert len(focused) < len(unfocused)


def test_the_menu_names_sections_that_were_not_shown():
    reply = build_extractive_answer(
        [_chunk("definition")], focused=True, with_menu=True
    )
    assert reply is not None
    assert "I can also cover" in reply
    assert "just ask." in reply
    # It must not offer the section the reader just read.
    assert "what it is" not in reply.split("I can also cover")[1]


# --------------------------------------------------------------------------
# is_focused — the fail-open guard
# --------------------------------------------------------------------------

def test_is_focused_false_when_the_filter_failed_open():
    """`_prefer_section` keeps the full ranking when the section is missing.

    Rendering that top-1 as a confident single-section answer would present an
    unrelated section as THE answer.
    """
    chunks = [_chunk("prevalence"), _chunk("definition")]
    assert is_focused(chunks, ("symptoms",)) is False


def test_is_focused_true_when_the_section_is_present():
    chunks = [_chunk("symptoms"), _chunk("signs")]
    assert is_focused(chunks, ("symptoms",)) is True


def test_is_focused_false_without_a_target():
    assert is_focused([_chunk("definition")], ()) is False


def test_is_focused_matches_continuation_parts():
    assert is_focused([_chunk("symptoms_2")], ("symptoms",)) is True


def test_the_menu_is_absent_unless_asked_for():
    """The caller passes with_menu only at risk == NONE.

    HIGH_ESCALATION is prepended to whatever this returns, and inviting a
    reader to browse the corpus underneath an urgent-care instruction would
    undercut it.
    """
    reply = build_extractive_answer([_chunk("definition")], focused=True)
    assert reply is not None
    assert "I can also cover" not in reply


@pytest.mark.parametrize(("section", "phrase"), list(_MENU_SECTIONS))
def test_every_menu_phrase_routes_back_to_the_section_it_offers(section, phrase):
    """The menu must not advertise a follow-up the router cannot route.

    An earlier wording offered "what contributes to it" and "what generally
    helps", neither of which contains a stem. Echoing the menu fell straight
    through to the unfiltered three-chunk answer the menu exists to avoid.
    """
    assert section in target_sections(phrase), (
        f"menu offers {phrase!r} but it does not resolve to {section!r}"
    )


def test_menu_sections_all_have_titles():
    from app.rag.extractive import _SECTION_TITLES

    for section, _phrase in _MENU_SECTIONS:
        assert section in _SECTION_TITLES


def test_rendered_chunks_matches_what_the_renderer_shows():
    """Citations are built from this, so it must not over-report.

    The old `render_limit` only reported the SLICE size; the renderer then
    dropped duplicates and empty-bodied chunks after it, so a citation could
    name a block the reader never saw.
    """
    assert [
        c.chunk_type
        for c in rendered_chunks([_chunk("definition")], focused=True)
    ] == ["definition"]
    mixed = [_chunk("definition"), _chunk("prevalence"), _chunk("symptoms")]
    assert len(rendered_chunks(mixed)) == 3


def test_rendered_chunks_excludes_a_duplicate_base_section():
    chunks = [_chunk("symptoms"), _chunk("symptoms_2"), _chunk("prevalence")]
    kinds = [c.chunk_type for c in rendered_chunks(chunks)]
    assert kinds == ["symptoms", "prevalence"], (
        "symptoms_2 collapses onto symptoms in the renderer, so it must not "
        "appear in the citation list either"
    )


def test_rendered_chunks_excludes_a_chunk_with_no_usable_body():
    empty = RetrievedChunk(
        id="x", condition_code="MC001", chunk_type="prevalence",
        content="Diabetes mellitus — prevalence:" + chr(10) + "short",
        score=0.0,
    )
    chunks = [_chunk("definition"), empty]
    assert [c.chunk_type for c in rendered_chunks(chunks)] == ["definition"]


def test_the_educational_disclaimer_survives_every_shape():
    for focused in (True, False):
        for with_menu in (True, False):
            reply = build_extractive_answer(
                [_chunk("definition"), _chunk("symptoms")],
                focused=focused,
                with_menu=with_menu,
            )
            assert reply is not None
            assert "not a diagnosis" in reply


def test_a_long_definition_is_not_truncated_mid_word():
    """MC001's definition body is a single 370-char line.

    At _MAX_LINE_CHARS = 260 the reply ended '...aka "Diabetes…'.
    """
    body = (
        "Diabetes is a chronic, metabolic disease characterized by elevated "
        "levels of blood glucose which leads over time to serious damage to "
        "the heart, blood vessels, eyes, kidneys and nerves. The most common "
        "is type 2 diabetes, usually in adults, which occurs when the body "
        "becomes resistant to insulin."
    )
    assert 260 < len(body) <= 400
    reply = build_extractive_answer([_chunk("definition", body)], focused=True)
    assert reply is not None
    assert "…" not in reply
    assert body in reply


# --------------------------------------------------------------------------
# Follow-up review findings
# --------------------------------------------------------------------------

def test_a_strong_opener_survives_a_compound_question():
    """"what is X and what are its symptoms" must keep BOTH halves.

    The generic-opener fallback fixed the byte-identical bug but over-corrected:
    an explicit "what is" alongside a specific stem lost the definition
    entirely, filtered out of retrieval before the renderer ever saw it.
    """
    sections = target_sections("what is diabetes and what are its symptoms")
    assert "definition" in sections
    assert "symptoms" in sections


def test_the_ambiguous_opener_still_does_not_union():
    """"what are" is how most questions begin, so it must stay a fallback."""
    assert "definition" not in target_sections(
        "what are the symptoms of diabetes"
    )


def test_focused_rendering_keeps_every_asked_for_section():
    """A flat limit of 1 discarded what the section filter deliberately kept."""
    chunks = [_chunk("symptoms"), _chunk("complications")]
    reply = build_extractive_answer(chunks, focused=True)
    assert reply is not None
    assert "Common symptoms" in reply
    assert "Possible complications" in reply


def test_focused_rendering_of_a_single_section_stays_single():
    reply = build_extractive_answer([_chunk("definition")], focused=True)
    assert reply is not None
    assert "How common it is" not in reply
    assert "Common symptoms" not in reply


def test_focused_rendering_is_capped():
    from app.rag.extractive import _MAX_SECTIONS_FOCUSED, _focused_limit

    many = [_chunk(s) for s in
            ("symptoms", "signs", "complications", "diagnosis", "etiology")]
    assert _focused_limit(many) == _MAX_SECTIONS_FOCUSED


def test_the_menu_is_never_the_whole_reply():
    """An empty model turn must not become a menu-only 'answer'.

    On the agentic engine the menu is appended to `display`. When the provider
    returns an empty or refusal turn, appending first turned "" into a string
    that PASSES validate_reply, so the reader got a browse list, no answer, and
    no degradation recorded. The orchestrator gates on `display` being truthy;
    this pins the property the gate protects.
    """
    from app.chat.validation import validate_reply
    from app.rag.extractive import disclosure_menu
    from app.triage.red_flags import NONE

    menu = disclosure_menu({"definition"})
    assert menu is not None
    # The menu alone is benign text — which is exactly the hazard: it passes.
    assert validate_reply(menu, NONE).ok
    # So emptiness must be caught BEFORE the append, never after.
    assert not validate_reply("", NONE).ok



# --------------------------------------------------------------------------- #
# Condition coverage
#
# Reported from staging: the corpus holds several diabetes profiles, and every
# answer came back about type 1. Ranking is per-chunk and lexical, so whichever
# profile words its overview closest to the question could take all k slots.
# --------------------------------------------------------------------------- #
def _chunk(code: str, section: str, score: float, n: int = 0):
    from app.rag.retrieval import RetrievedChunk

    return RetrievedChunk(
        id=f"{code}-{section}-{n}",
        condition_code=code,
        chunk_type=section,
        content=f"{code} {section}",
        score=score,
    )


def test_one_condition_cannot_take_every_slot():
    from app.rag.retrieval import spread_across_conditions

    # Type 1 outranks everything on raw score — the reported failure.
    ranked = [
        _chunk("MC_T1DM", "overview", 0.90),
        _chunk("MC_T1DM", "symptoms", 0.88, 1),
        _chunk("MC_T1DM", "diagnosis", 0.86, 2),
        _chunk("MC_T1DM", "signs", 0.84, 3),
        _chunk("MC_T2DM", "overview", 0.50),
        _chunk("MC_GDM", "overview", 0.40),
    ]
    out = spread_across_conditions(ranked, 4)
    codes = [c.condition_code for c in out]

    assert len(out) == 4
    assert set(codes) == {"MC_T1DM", "MC_T2DM", "MC_GDM"}, (
        f"one profile still monopolised the slots: {codes}"
    )
    # The strongest match still leads.
    assert codes[0] == "MC_T1DM"


def test_a_single_condition_is_untouched():
    """The common case must not be reordered or truncated differently."""
    from app.rag.retrieval import spread_across_conditions

    ranked = [_chunk("MC001", f"s{i}", 0.9 - i / 100, i) for i in range(6)]
    out = spread_across_conditions(ranked, 4)
    assert [c.id for c in out] == [c.id for c in ranked[:4]]


def test_spread_never_invents_or_drops_below_k():
    from app.rag.retrieval import spread_across_conditions

    ranked = [_chunk("A", "x", 0.9), _chunk("B", "y", 0.8)]
    assert len(spread_across_conditions(ranked, 4)) == 2  # only 2 available
    assert spread_across_conditions([], 4) == []
    assert spread_across_conditions(ranked, 0) == []


def test_spreading_after_a_k_limited_rerank_would_be_a_no_op():
    """The bug this guards: MMR asked for k returns k, and if all k came from
    one profile there is nothing left for the spread to reach for.

    So the reranker must ORDER the shortlist and let the spread SELECT. This
    test pins the distinction — spreading a k-length single-condition list
    cannot recover, which is why the call site passes len(shortlist).
    """
    from app.rag.retrieval import spread_across_conditions

    # What MMR-limited-to-k hands over: four chunks, one condition.
    k_limited = [_chunk("MC_T1DM", f"s{i}", 0.9 - i / 100, i) for i in range(4)]
    assert len(spread_across_conditions(k_limited, 4)) == 4
    assert {c.condition_code for c in spread_across_conditions(k_limited, 4)} == {
        "MC_T1DM"
    }, "nothing to spread — this is why the shortlist must be ordered, not cut"

    # What ordering the whole shortlist hands over: the other profiles survive.
    full = k_limited + [
        _chunk("MC_T2DM", "overview", 0.5),
        _chunk("MC_GDM", "overview", 0.4),
    ]
    out = spread_across_conditions(full, 4)
    assert {c.condition_code for c in out} == {"MC_T1DM", "MC_T2DM", "MC_GDM"}

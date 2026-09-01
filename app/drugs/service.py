"""Deterministic drug lookup + safe drug-information replies.

Backed by the core app's medicine catalogue (medicine_master, Flyway V19 —
which absorbed the drug_reference ingest). Every reply built here includes the
mandatory medication safety line and is written to pass the output validator
(no diagnosing, no med-causation claims).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.replies import MEDICATION_NOTE
from app.models.coredata import MedicineMaster

# Only approved, non-deleted catalogue rows are visible to chat.
_BASE_FILTERS = (
    MedicineMaster.status == "approved",
    MedicineMaster.deleted_at.is_(None),
)

# How many side effects / uses / substitutes to include in a reply.
MAX_LIST_ITEMS = 5

# Deterministic drug-info intents: (pattern with a <term> group).
_DRUG_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bside effects? of ([a-z0-9][a-z0-9 \-\.]{2,60})", re.IGNORECASE
    ),
    re.compile(
        r"\bwhat is ([a-z0-9][a-z0-9 \-\.]{2,60}?) (?:used|prescribed) for\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat (?:is|are) ([a-z0-9][a-z0-9 \-\.]{2,60}?) tablets? (?:for|used for)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsubstitutes? (?:for|of) ([a-z0-9][a-z0-9 \-\.]{2,60})", re.IGNORECASE
    ),
    re.compile(
        r"\balternatives? (?:for|to) ([a-z0-9][a-z0-9 \-\.]{2,60})", re.IGNORECASE
    ),
    re.compile(
        r"\bis ([a-z0-9][a-z0-9 \-\.]{2,60}?) habit[- ]forming\b", re.IGNORECASE
    ),
    re.compile(
        r"\btell me about (?:the )?(?:medicine|medication|drug|tablet) "
        r"([a-z0-9][a-z0-9 \-\.]{2,60})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\babout my (?:medicine|medication|tablet) ([a-z0-9][a-z0-9 \-\.]{2,60})",
        re.IGNORECASE,
    ),
)

# "is it ok to take X and Y together" put "together" inside the second term,
# so the reply read "Whether metformin and telmisartan together can be taken
# together...". Stripped before the noise passes.
_TERM_TOGETHER = re.compile(r"\s+together$", re.IGNORECASE)

_TERM_TRAILING_NOISE = re.compile(
    r"\s+(?:tablets?|capsules?|syrup|medicine|medication|drug|please|now|today)$",
    re.IGNORECASE,
)
_TERM_LEADING_NOISE = re.compile(
    r"^(?:my|the|this|that|a|an)\s+|^(?:medicines?|medications?|tablets?|drugs?)\s+",
    re.IGNORECASE,
)

# Everyday words that follow drug-question phrasing but are NOT medicines.
# Without this gate, "substitutes for sugar" would match the branded product
# "Sugar Control ..." via the prefix lookup. DRAFT — pending clinician sign-off.
NON_DRUG_TERMS: frozenset[str] = frozenset(
    {
        "sugar", "salt", "water", "milk", "honey", "ghee", "rice", "tea",
        "coffee", "caffeine", "alcohol", "smoking", "tobacco", "gutka",
        "exercise", "walking", "running", "yoga", "sleep", "stress",
        "protein", "whey", "creatine", "food", "fasting", "dieting",
        "chemotherapy", "radiation", "radiotherapy", "dialysis", "surgery",
        "vaccination", "pregnancy", "breastfeeding", "menopause",
        "sunlight", "screen time", "junk food", "fast food", "cold drinks",
        # Everyday foods, added when the interaction gate was hardened (Task
        # 25). "Can I take honey and lemon together" must still reach the LLM
        # as an ordinary question; without these, tightening that gate would
        # have turned every food pairing into a check-with-your-pharmacist
        # reply. DRAFT — pending clinician sign-off.
        "lemon", "lime", "ginger", "garlic", "turmeric", "haldi", "curd",
        "yogurt", "yoghurt", "buttermilk", "banana", "apple", "egg", "eggs",
        "chicken", "fish", "dal", "roti", "chapati", "bread", "juice",
        "green tea", "black tea", "lemon water", "warm water", "hot water",
        "jaggery", "dates", "nuts", "almonds", "cinnamon", "pepper",
        # GENERIC nouns for "a medicine", as opposed to a named one. Hardening
        # the interaction gate to fire on phrasing made these matter: without
        # them, "can I take my medicine with food?" -- an extremely ordinary
        # question -- produced "Whether medicine and food can be taken
        # together depends on...", which is nonsense. The refusal is only
        # meaningful when at least one side names a SPECIFIC substance.
        # "can I take paracetamol with my medicine?" still fires, correctly.
        "medicine", "medicines", "medication", "medications", "tablet",
        "tablets", "pill", "pills", "capsule", "capsules", "drug", "drugs",
        "syrup", "injection", "supplement", "supplements", "vitamin",
        "vitamins", "painkiller", "painkillers", "antibiotic", "antibiotics",
    }
)


def extract_drug_query_term(message: str) -> str | None:
    """Return the candidate drug term if the message is a drug-info question."""
    for pattern in _DRUG_QUERY_PATTERNS:
        m = pattern.search(message)
        if m:
            term = m.group(1).strip().strip("?.!,").strip()
            # Trim leading possessives/generics ("my medicine metformin") and
            # trailing filler ("dolo 650 tablet please"), iteratively.
            while True:
                trimmed = _TERM_LEADING_NOISE.sub("", term).strip()
                trimmed = _TERM_TRAILING_NOISE.sub("", trimmed).strip()
                if trimmed == term:
                    break
                term = trimmed
            if term.lower() in NON_DRUG_TERMS:
                return None
            return term or None
    return None


def _normalize(term: str) -> str:
    # Mirrors medicine_master's name_normalized trigger:
    # lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g')), stripped.
    return re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()


async def find_drug(db: AsyncSession, term: str) -> MedicineMaster | None:
    """Best-effort deterministic match: exact name → name prefix → composition."""
    norm = _normalize(term)
    if len(norm) < 3:
        return None

    def _first_active(rows: list[MedicineMaster]) -> MedicineMaster | None:
        # Prefer non-discontinued, then shortest name (most canonical), then
        # alphabetical for determinism.
        rows = sorted(
            rows, key=lambda r: (r.is_discontinued, len(r.name), r.name.lower())
        )
        return rows[0] if rows else None

    exact = list(
        (
            await db.execute(
                select(MedicineMaster).where(
                    *_BASE_FILTERS, MedicineMaster.name_normalized == norm
                )
            )
        ).scalars().all()
    )
    if exact:
        return _first_active(exact)

    # LIMIT without ORDER BY is nondeterministic on PostgreSQL — the candidate
    # window must be stable so the same query always resolves the same drug.
    prefix = list(
        (
            await db.execute(
                select(MedicineMaster)
                .where(
                    *_BASE_FILTERS,
                    MedicineMaster.name_normalized.like(f"{norm} %"),
                )
                .order_by(MedicineMaster.name_normalized, MedicineMaster.id)
                .limit(25)
            )
        ).scalars().all()
    )
    if prefix:
        return _first_active(prefix)

    # Looser prefix windows, in order of confidence:
    #   * dose-without-unit forms — "medrol 4" must find "Medrol 4mg Tablet",
    #     where the space-anchored prefix above cannot ("medrol 4 %" ≠
    #     "medrol 4mg …");
    #   * bare brand stems ≥4 chars — "digene"/"moov"/"volini" must find
    #     "Digene Acidity & Gas Relief …" even when the DB name never has the
    #     stem as its own word. Short stems ("pan") stay excluded so they
    #     cannot swallow unrelated products ("panadol").
    if re.search(r"\d$", norm) or len(norm) >= 4:
        dose_prefix = list(
            (
                await db.execute(
                    select(MedicineMaster)
                    .where(
                        *_BASE_FILTERS,
                        MedicineMaster.name_normalized.like(f"{norm}%"),
                    )
                    .order_by(MedicineMaster.name_normalized, MedicineMaster.id)
                    .limit(25)
                )
            ).scalars().all()
        )
        if dose_prefix:
            return _first_active(dose_prefix)

    candidates = list(
        (
            await db.execute(
                select(MedicineMaster)
                .where(
                    *_BASE_FILTERS,
                    MedicineMaster.composition_normalized.like(f"%{norm}%"),
                )
                .order_by(MedicineMaster.name_normalized, MedicineMaster.id)
                .limit(50)
            )
        ).scalars().all()
    )
    # LIKE is substring-based ("love" would match "clove"); require a whole-word
    # match on the salt name before accepting a composition hit.
    word_re = re.compile(r"\b" + re.escape(norm) + r"\b")
    composition = [
        r for r in candidates
        if r.composition_normalized and word_re.search(r.composition_normalized)
    ]
    if composition:
        # For a generic/salt query, a single-ingredient product represents the
        # substance better than a combination — prefer composition2 IS NULL,
        # then non-discontinued, then the deterministic name ordering.
        composition.sort(
            key=lambda r: (
                r.composition2 is not None,
                r.is_discontinued,
                len(r.name),
                r.name.lower(),
            )
        )
        return composition[0]
    return None


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Damerau-Levenshtein (optimal string alignment), capped at ``cap``.

    Adjacent transposition counts 1 — "dool"→"dolo" must score as one slip of
    the thumb, not two independent edits, or every swapped-letter typo lands
    outside the acceptance band.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


async def suggest_drug(db: AsyncSession, term: str) -> MedicineMaster | None:
    """A close catalogue match for a name :func:`find_drug` could NOT resolve.

    For misspelled adds ("dool 650" → Dolo 650 Tablet): candidate rows come
    from bounded, ordered prefix windows — the whole stem, its single-deletion
    variants (covering swapped/extra letters early in the word), and shrinking
    prefixes (covering slips later in the word) — then the stem is scored
    against each candidate's first word with a capped Damerau-Levenshtein
    distance. Deterministic: same term + same catalogue → same suggestion.
    """
    norm = _normalize(term)
    tokens = norm.split()
    stem = next((t for t in tokens if not t.isdigit()), "")
    if len(stem) < 4:
        return None  # too short to fuzzy-match safely ("b12", "od")
    digits = {t for t in tokens if t.isdigit()}

    prefixes: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        if len(p) >= 3 and p not in seen:
            seen.add(p)
            prefixes.append(p)

    _add(stem)
    for i in range(len(stem)):
        _add((stem[:i] + stem[i + 1:])[:4])
    for k in range(len(stem) - 1, 3, -1):
        _add(stem[:k])
    del prefixes[8:]  # bounded work no matter how long the stem is

    rows: dict[int, MedicineMaster] = {}
    for p in prefixes:
        got = (
            await db.execute(
                select(MedicineMaster)
                .where(
                    *_BASE_FILTERS,
                    MedicineMaster.name_normalized.like(f"{p}%"),
                )
                .order_by(MedicineMaster.name_normalized, MedicineMaster.id)
                .limit(80)
            )
        ).scalars().all()
        for r in got:
            rows.setdefault(r.id, r)
        if len(rows) >= 400:
            break

    max_d = 1 if len(stem) <= 5 else 2
    scored: list[tuple[tuple[int, int, bool, int, str], MedicineMaster]] = []
    for r in rows.values():
        words = (r.name_normalized or "").split()
        if not words:
            continue
        d = _edit_distance(stem, words[0], cap=max_d)
        if d > max_d:
            continue
        # A candidate carrying the typed number ("650") outranks one that
        # doesn't — "dool 650" must suggest Dolo 650, never Dolo 500.
        digit_miss = 0 if digits <= set(words) else 1
        scored.append(
            ((d, digit_miss, r.is_discontinued, len(r.name), r.name.lower()), r)
        )
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


async def find_substitutes(db: AsyncSession, drug: MedicineMaster) -> list[str]:
    """Up to 5 same-composition alternatives (deterministic order)."""
    if not drug.composition_normalized:
        return []
    rows = (
        await db.execute(
            select(MedicineMaster.name)
            .where(
                *_BASE_FILTERS,
                MedicineMaster.composition_normalized
                == drug.composition_normalized,
                MedicineMaster.id != drug.id,
                MedicineMaster.is_discontinued.is_(False),
            )
            .order_by(func.length(MedicineMaster.name), MedicineMaster.name)
            .limit(MAX_LIST_ITEMS)
        )
    ).scalars().all()
    return list(rows)


def build_drug_reply(
    drug: MedicineMaster,
    substitutes: Sequence[str | None] | None = None,
    allergy_warning: str = "",
) -> str:
    """A deterministic, validator-safe drug-information reply.

    PURE — the caller fetches ``substitutes`` via :func:`find_substitutes`.

    ``allergy_warning`` goes FIRST when present. It is a parameter rather than
    something read from the patient-context block because this path never sees
    that block: the drug handler returns from the orchestrator BEFORE
    `build_patient_context` runs, and it sits inside the legacy branch, so the
    reader's own allergies were unreachable from here on the default engine.

    A reader with a severe penicillin allergy asking "side effects of
    amoxicillin" got a clean monograph with no mention of it.
    """
    parts: list[str] = []
    if allergy_warning:
        parts.append(allergy_warning)
    # No manufacturer: it adds nothing a patient can act on, and the reply
    # should read like drug information, not a product listing.
    comp = ", ".join(c for c in (drug.composition1, drug.composition2) if c)
    intro = f"{drug.name}"
    if comp:
        intro += f" contains {comp}"
    parts.append(intro + ".")

    uses = [u for u in (drug.used_for or []) if u][:MAX_LIST_ITEMS]
    if uses:
        parts.append("It is generally used for: " + "; ".join(uses) + ".")

    # side_effects is a ", "-joined TEXT column in medicine_master.
    effects = [
        s for s in (drug.side_effects or "").split(", ") if s
    ][:MAX_LIST_ITEMS]
    if effects:
        parts.append(
            "Commonly reported side effects include: "
            + ", ".join(effects).lower()
            + ". Not everyone experiences these, and this list is not complete."
        )

    if drug.habit_forming is True:
        parts.append(
            "This medicine is listed as habit-forming, so it is especially "
            "important to use it exactly as prescribed."
        )
    elif drug.habit_forming is False:
        parts.append("This medicine is not listed as habit-forming.")

    if drug.is_discontinued:
        parts.append(
            "Note: this product is listed as discontinued; a pharmacist can "
            "advise on current availability."
        )

    subs = [s for s in (substitutes or []) if s][:MAX_LIST_ITEMS]
    if subs:
        parts.append(
            "Listed alternatives with similar composition include: "
            + "; ".join(subs)
            + " — a pharmacist or your doctor can confirm an appropriate substitute."
        )

    parts.append(MEDICATION_NOTE)
    parts.append(
        "This is general information, not medical advice for your specific "
        "situation — your doctor or pharmacist knows your context best."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Interaction / combination questions ("can I take X and Y together")
# --------------------------------------------------------------------------- #
# The medicine_master catalogue carries no interaction data, so these questions
# must never be answered substantively — neither by the LLM (ungrounded) nor
# with the generic safe fallback (a non-answer). Instead they get a dedicated
# deterministic reply that names both items and routes to a pharmacist.
_TERM = r"([a-z0-9][a-z0-9 \-\.]{1,60}?)"
# Terms are bounded by punctuation or end-of-sentence, NOT end-of-message: the
# original `$`-anchored forms were defeated by ANY trailing clause ("can I
# take warfarin with aspirin, my head hurts") and handed the one question
# class the codebase says can do the most harm to the LLM.
_END = r"\s*(?:[?.!,;:]|$| (?:or|because|since|as|but|and my)\b)"
_INTERACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\bcan i (?:take|have|use) {_TERM} (?:and|with|along with) {_TERM}"
        rf"(?: together)?{_END}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bis it (?:safe|ok|okay) to (?:take|have|use|combine|mix) {_TERM} "
        rf"(?:and|with|along with) {_TERM}{_END}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bdoes {_TERM} interact with {_TERM}{_END}", re.IGNORECASE
    ),
    re.compile(
        rf"\bcan {_TERM} (?:and|be taken with) {_TERM} be taken together\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:mix|mixing) {_TERM} (?:and|with) {_TERM}{_END}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bwhat happens if i (?:take|mix|combine) {_TERM} "
        rf"(?:and|with|along with) {_TERM}{_END}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bis {_TERM} safe (?:with|to take with|alongside) {_TERM}{_END}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:taking|took) {_TERM} (?:and|with|along with) {_TERM} together\b",
        re.IGNORECASE,
    ),
)
_INTERACTION_TERM_NOISE = re.compile(
    r"^(?:my|the|this|that|a|an|some)\s+", re.IGNORECASE
)


def _interaction_term(raw: str) -> str | None:
    """Clean one side of a combination question. None only if nothing is left.

    The noise strippers exist to turn "my dolo tablet" into "dolo". They can
    also strip a colloquial reference down to almost nothing: "my bp tablet"
    loses "my" and "tablet" and becomes "bp", two characters.

    That MUST NOT abandon the refusal. Measured in staging: "can i take
    metformin with my bp tablet" returned None here, so `_interaction_refusal`
    never fired, the turn went to the agentic engine, and the model answered
    from its own weights — "commonly prescribed together ... generally
    considered routine" — about the reader's real prescriptions, from a
    catalogue that holds NO interaction data. The same question naming both
    drugs was refused correctly.

    The interaction SHAPE is the safety signal, not the resolvability of the
    names. When cleaning leaves too little, fall back to the reader's own
    words: echoing "my bp tablet" back is honest and still routes them to a
    pharmacist.
    """
    term = raw.strip().strip("?.!,").strip()
    term = _TERM_TOGETHER.sub("", term).strip()
    cleaned = _INTERACTION_TERM_NOISE.sub("", term).strip()
    cleaned = _TERM_TRAILING_NOISE.sub("", cleaned).strip()
    if len(cleaned) >= 3:
        return cleaned
    return term or None


def extract_interaction_query(message: str) -> tuple[str, str] | None:
    """Return the two combined terms if the message asks about mixing them."""
    for pattern in _INTERACTION_PATTERNS:
        m = pattern.search(message)
        if m:
            first = _interaction_term(m.group(1))
            second = _interaction_term(m.group(2))
            if not first or not second:
                return None
            return (first, second)
    return None


def build_interaction_reply(term_a: str, term_b: str) -> str:
    """Deterministic, validator-safe reply for a combination question."""
    return (
        f"Whether {term_a} and {term_b} can be taken together depends on "
        "things I cannot verify from here — the doses, the timing, your other "
        "medicines, and factors like kidney and liver function. I don't have "
        "a validated interaction checker, so please ask a pharmacist or the "
        "prescriber about this specific combination before taking them "
        f"together. {MEDICATION_NOTE} This is general information, not "
        "medical advice for your specific situation — your doctor or "
        "pharmacist knows your context best."
    )


# --------------------------------------------------------------------------- #
# Dose / dosage questions ("how much dolo can I give my child")
# --------------------------------------------------------------------------- #
# medicine_master carries no dosing data, and a model-invented mg figure —
# especially a pediatric one — is the single most dangerous output class this
# product could produce. Dose questions therefore get a deterministic
# pharmacist/label routing, in the SHARED prologue, on both engines.
# A drug name is 1-2 tokens (optionally a trailing number: "dolo 650") — a
# bounded shape, not a lazy wildcard that stops at the minimum match.
_DOSE_TERM = (r"(?:of\s+|for\s+)?"
              r"([a-z][a-z0-9\-\.]{2,}(?:\s+[a-z0-9\-\.]{1,15})?)")
_CHILD = (r"(?:child|children|kid|kids|baby|infant|toddler|son|daughter|"
          r"\d+[- ]?(?:year|month|yr|mo)s?[- ]?old)")
_DOSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "what is the dose/dosage of X", "correct dosage for X"
    re.compile(
        rf"\b(?:what(?:'s| is)?|whats) (?:the )?(?:right |correct |safe |usual |recommended )?"
        rf"dos(?:e|age)\s*{_DOSE_TERM}?\s*(?:[?.!,;:]|$)",
        re.IGNORECASE,
    ),
    # "how much X can/should I take/give", "how much X for a 6 year old"
    re.compile(
        rf"\bhow much {_DOSE_TERM}\s*(?:can|should|do|to)?\s*"
        rf"(?:i|we|you|one)?\s*(?:take|give|use|have)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bhow much {_DOSE_TERM}\s*for (?:a |my |an )?{_CHILD}\b",
        re.IGNORECASE,
    ),
    # "how many mg/ml/tablets (of X)"
    re.compile(
        rf"\bhow many (?:mg|mcg|ml|tablets?|tabs?|pills?|drops?)\s*{_DOSE_TERM}?",
        re.IGNORECASE,
    ),
    # "can/should I give my child X", "can I give X to my baby"
    re.compile(
        rf"\b(?:can|should|could) (?:i|we) give (?:my |the |a |an )?{_CHILD}"
        r"\s+([a-z0-9][a-z0-9 \-\.]{1,40})\s*(?:[?.!,;:]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:can|should|could) (?:i|we) give {_TERM} to (?:my |the |a |an )?{_CHILD}\b",
        re.IGNORECASE,
    ),
    # "X dose for a child / for adults", "dolo dosage"
    re.compile(
        rf"\b{_TERM}\s+dos(?:e|age)\s+for\b",
        re.IGNORECASE,
    ),
)


def extract_dose_query(message: str) -> str | None:
    """The asked-about term for a dose question; "" for a termless one
    ("how much should I take?"); None when the message is not a dose ask."""
    for pattern in _DOSE_PATTERNS:
        m = pattern.search(message)
        if m:
            raw = (m.group(1) or "") if m.groups() else ""
            term = raw.strip().strip("?.!,").strip()
            term = _TERM_TRAILING_NOISE.sub("", term).strip()
            # The 2-token shape can catch a following function word
            # ("paracetamol can", "dolo for") — drop it.
            parts = term.split()
            if len(parts) == 2 and parts[1].lower() in {
                "can", "should", "could", "for", "to", "per", "a", "the",
                "my", "in", "with", "when", "if", "before", "after", "daily",
                "at", "is", "be",
            }:
                term = parts[0]
            # Grammar words captured by the looser shapes are not terms:
            # "how much should I take" is a dose ask with NO named drug.
            first = term.split()[0].lower() if term else ""
            if first in {"should", "can", "could", "do", "does", "to", "i",
                         "we", "you", "one", "it", "the", "a", "an", "my"}:
                term = ""
            return term
    return None


def build_dose_refusal(term: str) -> str:
    """Deterministic, validator-safe reply for a dose question."""
    what = f"the right amount of {term}" if term else "the right amount"
    return (
        f"I can't advise on {what} — safe dosing depends on age, body "
        "weight, other conditions and other medicines, and for children it "
        "changes with every kilogram. Please check the pack label and "
        "confirm the dose with your pharmacist or prescriber before taking "
        "or giving anything."
    )

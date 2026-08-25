"""Deterministic drug lookup + safe drug-information replies.

Backed by the clinically-validated merged medicines database (drug_reference).
Every reply built here includes the mandatory medication safety line and is
written to pass the output validator (no diagnosing, no med-causation claims).
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.replies import MEDICATION_NOTE
from app.models.knowledge import DrugReference

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
    return re.sub(r"\s+", " ", term.strip().lower())


async def find_drug(db: AsyncSession, term: str) -> DrugReference | None:
    """Best-effort deterministic match: exact name → name prefix → composition."""
    norm = _normalize(term)
    if len(norm) < 3:
        return None

    def _first_active(rows: list[DrugReference]) -> DrugReference | None:
        # Prefer non-discontinued, then shortest name (most canonical), then
        # alphabetical for determinism.
        rows = sorted(
            rows, key=lambda r: (r.is_discontinued, len(r.name), r.name.lower())
        )
        return rows[0] if rows else None

    exact = list(
        (
            await db.execute(
                select(DrugReference).where(DrugReference.name_normalized == norm)
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
                select(DrugReference)
                .where(DrugReference.name_normalized.like(f"{norm} %"))
                .order_by(DrugReference.name_normalized, DrugReference.id)
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
                    select(DrugReference)
                    .where(DrugReference.name_normalized.like(f"{norm}%"))
                    .order_by(DrugReference.name_normalized, DrugReference.id)
                    .limit(25)
                )
            ).scalars().all()
        )
        if dose_prefix:
            return _first_active(dose_prefix)

    candidates = list(
        (
            await db.execute(
                select(DrugReference)
                .where(DrugReference.composition_normalized.like(f"%{norm}%"))
                .order_by(DrugReference.name_normalized, DrugReference.id)
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


def build_drug_reply(drug: DrugReference, allergy_warning: str = "") -> str:
    """A deterministic, validator-safe drug-information reply.

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

    uses = [u for u in (drug.uses or []) if u][:MAX_LIST_ITEMS]
    if uses:
        parts.append("It is generally used for: " + "; ".join(uses) + ".")

    effects = [s for s in (drug.side_effects or []) if s][:MAX_LIST_ITEMS]
    if effects:
        parts.append(
            "Commonly reported side effects include: "
            + ", ".join(effects).lower()
            + ". Not everyone experiences these, and this list is not complete."
        )

    if drug.habit_forming:
        hf = drug.habit_forming.strip().lower()
        if hf == "yes":
            parts.append(
                "This medicine is listed as habit-forming, so it is especially "
                "important to use it exactly as prescribed."
            )
        elif hf == "no":
            parts.append("This medicine is not listed as habit-forming.")

    if drug.is_discontinued:
        parts.append(
            "Note: this product is listed as discontinued; a pharmacist can "
            "advise on current availability."
        )

    subs = [s for s in (drug.substitutes or []) if s][:MAX_LIST_ITEMS]
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
# The drug_reference dataset carries no interaction data, so these questions
# must never be answered substantively — neither by the LLM (ungrounded) nor
# with the generic safe fallback (a non-answer). Instead they get a dedicated
# deterministic reply that names both items and routes to a pharmacist.
_TERM = r"([a-z0-9][a-z0-9 \-\.]{1,60}?)"
_INTERACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\bcan i (?:take|have|use) {_TERM} (?:and|with|along with) {_TERM}"
        r"(?: together)?\s*[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bis it (?:safe|ok|okay) to (?:take|have|use|combine) {_TERM} "
        rf"(?:and|with|along with) {_TERM}\s*[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bdoes {_TERM} interact with {_TERM}\s*[?.!]*$", re.IGNORECASE
    ),
    re.compile(
        rf"\bcan {_TERM} (?:and|be taken with) {_TERM} be taken together\b",
        re.IGNORECASE,
    ),
)
_INTERACTION_TERM_NOISE = re.compile(
    r"^(?:my|the|this|that|a|an|some)\s+", re.IGNORECASE
)


def extract_interaction_query(message: str) -> tuple[str, str] | None:
    """Return the two combined terms if the message asks about mixing them."""
    for pattern in _INTERACTION_PATTERNS:
        m = pattern.search(message)
        if m:
            terms = []
            for raw in (m.group(1), m.group(2)):
                term = raw.strip().strip("?.!,").strip()
                term = _INTERACTION_TERM_NOISE.sub("", term).strip()
                term = _TERM_TRAILING_NOISE.sub("", term).strip()
                if len(term) < 3:
                    return None
                terms.append(term)
            return (terms[0], terms[1])
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

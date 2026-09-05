"""Post-LLM output validation.

DRAFT — pending clinician sign-off. Enforces the non-negotiable safety rules on
any generated reply:
  * non-empty
  * no banned diagnostic phrasing ("you have X", "this is likely X", numeric
    disease probabilities, "your medication is causing X")
  * at HIGH/EMERGENCY, no pure-reassurance reply — it must carry an escalation
    directive.
On failure the caller substitutes a deterministic safe reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.triage.red_flags import EMERGENCY, HIGH

# Condition vocabulary used to detect diagnostic assertions (DRAFT).
# Includes common Indian infectious/chronic disease names NOT covered by the
# MCP corpus registry (e.g. dengue, malaria) plus bare lay forms of covered
# ones ("typhoid" vs the registry's "Typhoid Enteric Fever").
_CONDITION_LEXICON = (
    "diabetes",
    "diabetic",
    "hypertension",
    "high blood pressure",
    "cancer",
    "tumour",
    "tumor",
    "heart attack",
    "heart disease",
    "heart failure",
    "coronary artery disease",
    "coronary",
    "stroke",
    "angina",
    "copd",
    "asthma",
    "kidney disease",
    "dengue",
    "malaria",
    "typhoid",
    "chikungunya",
    "tuberculosis",
    "pneumonia",
    "jaundice",
    "hepatitis",
    "appendicitis",
    "arthritis",
    "anemia",
    "anaemia",
    "epilepsy",
    "leukemia",
    "leukaemia",
    "lymphoma",
    "hiv",
    "meningitis",
    "sepsis",
)
_COND_RE = "|".join(re.escape(c) for c in _CONDITION_LEXICON)

# "you have/are/... <up to a few words> <condition>" — diagnostic assertion.
# Requires a condition token nearby so benign "you have questions" is not flagged.
#
# Two families of phrasing are educational rather than diagnostic and are
# excluded with fixed-width lookbehinds:
#   * conditional framings — "if/when/whether/once you have X", and verbs that
#     make the clause hypothetical: "(if you) think/suspect/expect/believe you
#     (might) have X", "…that you have X"
#   * benign head nouns directly before the condition — "a higher risk of X",
#     "a family history of X", "the chance of X" (central phrasings for a
#     family-history product, never assertions about the user's own status)
_CONDITIONAL_GUARDS = (
    r"(?<![Ii]f )(?<![Ww]hen )(?<!ther )(?<![Oo]nce )(?<![Tt]hat )"
    r"(?<!ink )(?<!pect )(?<!ieve )"
)
_BENIGN_HEAD_GUARDS = (
    r"(?<!risk of )(?<!risks of )(?<!risk for )(?<!chance of )(?<!chances of )"
    r"(?<!history of )(?<!likelihood of )(?<!odds of )"
)


def _diagnostic_pattern(condition_alternation: str) -> str:
    """Full diagnostic-assertion pattern over a condition alternation.

    Two branches: "you … have <condition>" tolerates a gap ("you have severe
    type 2 diabetes"), while "you are <condition>" is kept tight — a free gap
    turned risk statements ("you are at higher risk of diabetes") into
    false positives.
    """
    cond = _BENIGN_HEAD_GUARDS + "(?:" + condition_alternation + r")\b"
    have_branch = (
        _CONDITIONAL_GUARDS
        + r"\byou(?:'ve got| are suffering from|'re suffering from"
        + r"| suffer from| seem to have| appear to have"
        + r"|(?: most| almost)?"
        + r"(?: surely| certainly| clearly| obviously| probably| likely| definitely"
        + r"| may| might)? have)\b[^.?!]{0,40}?\b"
        + cond
    )
    are_branch = (
        _CONDITIONAL_GUARDS
        + r"\byou(?: are|'re)(?: most| very| quite)?"
        + r"(?: surely| certainly| clearly| obviously| probably| likely"
        + r"| definitely| now)?"
        + r"(?: having| experiencing| developing)?(?: a| an| the)? "
        + cond
    )
    return "(?:" + have_branch + "|" + are_branch + ")"


_DIAGNOSTIC_RE = re.compile(_diagnostic_pattern(_COND_RE), re.IGNORECASE)

# Cache of dynamic diagnostic regexes built from registry condition names.
_dynamic_cache: dict[tuple[str, ...], re.Pattern[str]] = {}


def _dynamic_diagnostic_re(extra_conditions: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile (and cache) a diagnostic-assertion regex over registry names.

    Trailing non-word characters are stripped from each name — a name ending
    in ")" or "." would make the trailing ``\\b`` unmatchable.
    """
    names = tuple(
        sorted(
            {
                cleaned
                for c in extra_conditions
                if len(cleaned := re.sub(r"[^\w]+$", "", c.strip().lower())) >= 4
            }
        )
    )
    if not names:
        return None
    cached = _dynamic_cache.get(names)
    if cached is not None:
        return cached
    alternation = "|".join(re.escape(n) for n in names)
    pattern = re.compile(_diagnostic_pattern(alternation), re.IGNORECASE)
    _dynamic_cache[names] = pattern
    return pattern

# Numeric disease probability, e.g. "80% chance you have ...".
_PROBABILITY_RE = re.compile(
    r"\b\d{1,3}\s?(?:%|percent|per cent)\s?"
    r"(?:chance|probability|risk|likelihood)\b", re.IGNORECASE
)

# Phrases that are diagnostic/med-causal regardless of condition token.
_BANNED_SUBSTRINGS = (
    "this is likely",
    "it is likely that you",
    "you likely have",
    "you probably have",
    "you most likely have",
    "you are suffering from",
    "you're suffering from",
    "your diagnosis is",
    "i diagnose you",
    "you have been diagnosed",
    "your medication is causing",
    "your medications are causing",
    "your meds are causing",
    "is caused by your medication",
    "caused by your medication",
)

# Grading a WEARABLE number. `chat-visual-payload-contract.md` §7: "a
# threshold, band, grade or traffic light on a wearable number. Davi has no
# reference ranges for sleep or HRV and will not invent them."
#
# The summary tool's description ASKS the model not to do this. That is a
# prompt instruction, and on the agentic engine nothing downstream enforced
# it. Both engines pass through here, so the rule lives here.
#
# FOUR CONJUNCTS, not an ordered phrase template. The first version was an
# ordered regex over an adjective list and failed in both directions at once:
# it blocked four of five plainly descriptive sentences ("Your steps are
# counted by the wrist sensor, which is fine for walking") and admitted nine
# rephrasings of the thing it exists to stop ("below the recommended 10,000"
# — no adjective from the list, so it walked straight through). A guard that
# blocks safe prose and admits the unsafe sentence teaches everyone to route
# around it.
#
# A sentence is a grade when ALL of these hold:
#   1. it is about the READER's own reading — "your sleep", "you logged",
#      "your device"; a bare "sleep of 7-9 hours is healthy" is the validated
#      corpus answering a general question and must stay allowed;
#   2. it names a wearable metric (a bare "heart rate" is deliberately absent
#      -- that is a VITAL with real reference bands (V28), which Davi grades);
#   3. it quotes a FIGURE. Every sentence the rule exists to block does; none
#      of the descriptive false positives did. This one conjunct removed all
#      four of them;
#   4. it carries an evaluative or comparative predicate.
#
# KNOWN GAPS, stated rather than papered over: a verdict with no figure ("that
# is a solid week of sleep for you") and an impersonally-phrased one
# ("sleeping 46.7 hours across the week is a healthy amount") both pass.
# Closing either means dropping conjunct 1 or 3, and each of those blocks
# ordinary corpus prose about sleep — the trade the brief asks to be made in
# this direction.
_METRICS = (
    r"sleep\w*|slept|steps?|step count|hrv|heart rate variability"
    r"|resting heart rate|resting pulse"
)
_WEARABLE_METRIC_RE = re.compile(rf"\b(?:{_METRICS})\b", re.IGNORECASE)
# The reader's OWN reading, not the world's.
_PERSONAL_METRIC_RE = re.compile(
    # `[\w,.]` not `\w`: "your 7,400 daily steps" is how a step count is
    # actually written -- Davi writes it that way itself -- and a comma
    # ended the match, so the headline shape the rule exists for walked
    # through on a one-character rephrase. {0,4} for "your weekly total of".
    rf"\byour\s+(?:[\w,.]+\s+){{0,6}}?(?:{_METRICS})\b"
    r"|\byou\s+(?:logged|recorded|walked|slept|sleeping|walking|averaging"
    r"|took|averaged|hit|managed|got|clocked|racked up)\b"
    r"|\byour\s+(?:\w+\s+){0,2}?(?:device|watch|tracker|wearable|band|ring)\b"
    r"|\bfor you\b",
    re.IGNORECASE,
)
_FIGURE_RE = re.compile(r"\d")
# Explicitly normative or comparative — these carry the grade on their own.
# "under"/"over" are gated on a normative noun because "52,300 steps over 7
# days" is a count, not a comparison, and the ungated form blocked it.
_NORMATIVE_SRC = (
    r"\bbelow\b|\babove\b|\bshort of\b|\bexceed\w*\b|\bfall\w*\s+short\b"
    r"|\b(?:under|over)\s+(?:the\s+)?"
    r"(?:recommended|target|guideline|ideal|typical|normal|average)\b"
    r"|\brecommended\b|\btarget\b|\bideal\b|\boptimal\b|\bon track\b"
    r"|\b(?:normal|healthy|typical|expected)\s+range\b"
    r"|\btoo\s+(?:low|high|little|much|few|many)\b"
    # Traffic lights and scores are named in the payload contract as
    # things Davi never puts on a wearable number, and were missing.
    r"|\b(?:green|amber|red)\s+light\b|\bsub-?optimal\b"
    r"|\b(?:sleep|activity|recovery|readiness|wellbeing)\s+score\b"
    # Comparatives grade just as surely as adjectives do.
    r"|\b(?:lower|higher|shorter|longer|worse|better)\s+than\b"
    r"|\btoward(?:s)?\s+the\s+(?:lower|higher|upper|bottom|top)\b"
)
# "reference range" is deliberately NOT in that list: the only place the
# phrase appears in this product is the REFUSAL -- "I have no reference range
# to say whether any of it is high or low" -- and counting it made the
# wearable summary line grade itself.
_NORMATIVE_RE = re.compile(_NORMATIVE_SRC, re.IGNORECASE)
# A verdict adjective in PREDICATIVE position only. Attributive uses are
# ordinary English -- "a low battery", "a normal daytime pulse", "as good as
# how often you carry it" -- and enumerating the adjectives bare is exactly
# what made the rule fire on descriptive prose.
_VERDICT_RE = re.compile(
    _NORMATIVE_SRC
    + r"|\b(?:is|are|was|were|look|looks|seem|seems|sit|sits|fall|falls"
    r"|remain|remains|stay|stays|come|comes|rate|rates|count|counts)\b"
    r"(?:\s+\w+){0,3}?\s+"
    # "fine" is deliberately absent: it carries no clinical meaning on its
    # own and is ordinary English about a MEASUREMENT ("tracked in 30-second
    # epochs, which is fine for most nights").
    r"\b(?:good|great|excellent|healthy|poor|bad|low|high|normal|solid"
    r"|decent|okay|impressive|concerning|worrying|reassuring)\b",
    re.IGNORECASE,
)
# A sentence end is a terminator followed by whitespace, so "46.7 h" stays one
# sentence and "45 ms. That is what your device recorded" becomes two.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])(?=\s)")
# "Your sleep averaged 5.1 hours a night. That is below what most adults
# need." — a bare back-reference inherits the previous sentence's subject and
# figure. Merged ONLY for the normative verdicts: allowing the predicative
# branch to merge would swallow the wearable line's own "I have no reference
# range to say whether any of it is high or low", which is the opposite of a
# grade and is the sentence that makes the refusal honest.
_BACKREF_RE = re.compile(
    r"\s*(?:that|this|it)\b(?:'s|’s)?(?:\s+(?:is|was))?\b", re.IGNORECASE
)


_SELF_GRADING_RE = re.compile(
    r"\b(?:green|amber|red)\s+light\b|\bsub-?optimal\b"
    r"|\b(?:sleep|activity|recovery|readiness|wellbeing)\s+score\b",
    re.IGNORECASE,
)


def grades_a_wearable_figure(text: str) -> bool:
    """True when a sentence puts a verdict on the reader's own wearable number."""
    sentences = _SENTENCE_SPLIT_RE.split(text)
    for i, sentence in enumerate(sentences):
        scope = sentence
        if i and _BACKREF_RE.match(sentence) and _NORMATIVE_RE.search(sentence):
            scope = sentences[i - 1] + " " + sentence
        graded = _VERDICT_RE.search(scope)
        # A traffic light or a named score carries the grade on its own, so
        # it is exempt from the figure conjunct: "your sleep score is amber"
        # has no digit in it and is precisely what the contract forbids.
        needs_figure = not _SELF_GRADING_RE.search(scope)
        if (
            _PERSONAL_METRIC_RE.search(scope)
            and _WEARABLE_METRIC_RE.search(scope)
            and (not needs_figure or _FIGURE_RE.search(scope))
            and graded
        ):
            return True
    return False

# An empty table stated as a clinical finding. "Not on record for you:
# allergy information" is what the deterministic summary says; "you have no
# allergies" is a claim about the reader's body made out of rows nobody
# entered. The records framing is explicitly allowed -- that is the whole
# difference -- so "you have no active medications ON RECORD" passes.
_ABSENCE_AS_FINDING_RE = re.compile(
    r"\byou\s+(?:have|had|have got|['\u2019]ve got)\s+(?:no|not any|zero)\s+"
    r"(?:known\s+|current\s+|active\s+|other\s+|major\s+)*"
    r"(?:allerg\w*|medical conditions?|health conditions?|conditions?"
    r"|medications?|medicines?|meds|comorbidities)\b"
    r"(?!\s*(?:on record|recorded|listed|on file|in your record))"
    # The adjectival form, which the noun-phrase shape above missed entirely:
    # "You are not allergic to anything" is the same claim about the reader's
    # body, made out of the same empty table.
    r"|\byou\s+(?:are|were|'re|\u2019re)\s+not\s+"
    r"(?:allergic|on any (?:medication|medicines?|meds))\b",
    re.IGNORECASE,
)

# A personal go-ahead for a treatment, which is the other half of the same
# turn: "You have no allergies on record, so you are clear to take any
# medication." The absence framing there is correct; the CONCLUSION is not
# Davi's to draw, and no roster of records can support it.
#
# Three shapes, because requiring the adjective to follow "you" immediately
# let the two commonest phrasings through verbatim: "It is safe for you to
# take ibuprofen" and "You should be fine to take it".
_PERSONAL_CLEARANCE_RE = re.compile(
    r"\byou(?:'|\u2019)?(?:re| are)?\s+(?:therefore\s+|so\s+)?"
    r"(?:clear|safe|fine|ok|okay)\s+to\s+(?:take|use|start|have|continue)\b"
    r"|\b(?:it|that)(?:'|\u2019)?(?:s| is| would be| should be)?\s+"
    r"(?:perfectly\s+|quite\s+|generally\s+|probably\s+)?"
    r"(?:safe|fine|ok|okay)\s+(?:for you\s+)?to\s+"
    r"(?:take|use|start|have|continue)\b"
    r"|\byou\s+(?:should|will|'ll|\u2019ll|would|could)\s+be\s+"
    r"(?:fine|ok|okay|safe|clear)\s+to\s+(?:take|use|start|have|continue)\b"
    r"|\byou\s+can\s+(?:safely|certainly)\s+(?:take|use|start|have|continue)\b",
    re.IGNORECASE,
)

# The underlying model/provider must never be named to the user — Ink answers
# identity questions deterministically as "Ink" (router + canned reply), and
# this is the last line of defense if a leak slips into generated text.
# Word-boundaried: "SGPT" (liver enzyme) and "claudication" must never match.
_PROVIDER_LEAK_RE = re.compile(
    r"\b(?:anthropic|openai|chatgpt|gpt\s?-?\d[\w.-]*|gpt-4o|claude|sonnet"
    r"|haiku|opus|gemini|deepseek|mistral|llama|qwen|grok|copilot)\b"
    r"|\b(?:large\s+)?language\s+model\b",
    re.IGNORECASE,
)

# Markers that count as an escalation directive at HIGH/EMERGENCY.
_ESCALATION_MARKERS = (
    "emergency",
    "call your local",
    "call an ambulance",
    "nearest emergency",
    "seek medical care",
    "seek immediate",
    "urgent care",
    "go to the nearest",
    "see a doctor now",
    "medical care promptly",
    "contact a doctor",
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""


def find_banned(
    text: str, extra_conditions: tuple[str, ...] | None = None
) -> str | None:
    """Return the first banned pattern found, or None.

    ``extra_conditions`` extends the diagnostic-assertion lexicon with the
    clinically-validated registry names (512 conditions) when available.
    """
    low = text.lower()
    for phrase in _BANNED_SUBSTRINGS:
        if phrase in low:
            return phrase
    if _PROVIDER_LEAK_RE.search(text):
        return "provider-leak"
    if grades_a_wearable_figure(text):
        return "wearable-grading"
    if _ABSENCE_AS_FINDING_RE.search(text):
        return "absence-as-finding"
    if _PERSONAL_CLEARANCE_RE.search(text):
        return "personal-clearance"
    if _DIAGNOSTIC_RE.search(text):
        return "diagnostic-assertion"
    if extra_conditions:
        dynamic = _dynamic_diagnostic_re(extra_conditions)
        if dynamic is not None and dynamic.search(text):
            return "diagnostic-assertion"
    if _PROBABILITY_RE.search(text):
        return "numeric-disease-probability"
    return None


# Care-discouraging phrasing that must never survive at HIGH/EMERGENCY —
# even alongside an escalation banner, a body that talks the reader OUT of
# care ("this is not an emergency, no need to see anyone") teaches them the
# banner is boilerplate. (The old check was negation-blind: the word
# "emergency" inside "not an emergency" COUNTED AS the escalation.)
_CARE_DISCOURAGING_RE = re.compile(
    r"\bnot?\s+(?:really\s+|actually\s+)?an?\s+emergency\b"
    r"|\bno\s+emergency\b"
    r"|\bno need to (?:see|go|call|visit|rush|consult)\b"
    r"|\bnothing to worry\b"
    r"|\b(?:don'?t|do not|doesn'?t|does not) "
    r"(?:need (?:a|to see|to call|to go|to visit)|bother going)\b"
    r"|\bno need (?:for|of) (?:a )?(?:doctor|hospital)\b"
    r"|\bsettle on its own\b|\bnothing serious\b",
    re.IGNORECASE,
)

_NEGATION_WINDOW_RE = re.compile(
    r"\b(?:not?|isn'?t|doesn'?t|don'?t|needn'?t|never|without|no need to)\s*"
    r"(?:\w+\s+){0,6}$"
)


def has_escalation(text: str) -> bool:
    """True when a REAL escalation directive is present.

    Negation-aware: a marker preceded by a nearby negation ("you do NOT need
    to go to the nearest emergency department") is not an escalation.
    """
    low = text.lower()
    for m in _ESCALATION_MARKERS:
        start = low.find(m)
        while start != -1:
            window = low[max(0, start - 30):start]
            if not _NEGATION_WINDOW_RE.search(window):
                return True
            start = low.find(m, start + 1)
    return False


def discourages_care(text: str) -> bool:
    """True when the text talks the reader OUT of seeking care."""
    return bool(_CARE_DISCOURAGING_RE.search(text))


def validate_reply(
    reply: str,
    risk_level: str,
    extra_conditions: tuple[str, ...] | None = None,
) -> ValidationResult:
    """Validate a generated reply against the safety rules."""
    if not reply or not reply.strip():
        return ValidationResult(False, "empty")

    banned = find_banned(reply, extra_conditions)
    if banned is not None:
        return ValidationResult(False, f"banned:{banned}")

    if risk_level in (HIGH, EMERGENCY):
        if discourages_care(reply):
            # Pure reassurance at HIGH is the stated invariant this enforces;
            # it fires even when an escalation banner is ALSO present, because
            # a body that contradicts the banner teaches readers to ignore it.
            return ValidationResult(False, "reassurance-at-high")
        if not has_escalation(reply):
            return ValidationResult(False, "missing-escalation")

    return ValidationResult(True)


def redact_reason(reason: str) -> str:
    """A trace-safe form of a validation reason.

    ``validate_reply`` returns the MATCHED phrase ("banned:you probably have")
    so the corrective retry can be specific. The trace is user-visible, so it
    gets the category only — echoing the banned text back to the reader defeats
    the point of blocking it.
    """
    return reason.split(":", 1)[0] if ":" in reason else reason

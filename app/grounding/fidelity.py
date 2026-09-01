"""Numeric fidelity guards — pure, stdlib only.

Two related jobs, one mechanism:

* ``digits_preserved`` — a machine translation must not corrupt a dosage, a lab
  value, or the Tele-MANAS helpline number.
* ``values_traceable`` — when a reply is composed by the model from tool
  results, every clinical value it states must actually appear in one of those
  results. The model may summarise and rephrase; it may not drift a number.

Only UNIT-BEARING values are checked. Ordinary prose numbers ("three things to
discuss", "step 2", "in 2 weeks") are not clinical claims and are ignored, so
the guard has no opinion about normal writing.

This is deliberately narrow. It does NOT catch a wrong claim with no digits in
it — see app/grounding/claims.py for the clinical-assertion check that does.
"""

from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\d+")

# A blood-pressure pair, or a number immediately followed by a clinical unit.
# The unit vocabulary mirrors app/grounding/claims.py so the two guards agree
# on what counts as a clinical value.
# NOTE on the trailing boundary: "%" is a non-word character, so a trailing
# \b after it demands a following word character and "6.1%." would never
# match. Percentages therefore get their own alternative with no \b, and the
# word-suffixed units keep theirs. Blood pressure comes first because it is
# the most specific shape.
#
# NOTE on the THOUSANDS SEPARATOR: a comma is a word boundary, so "14,000 ml"
# used to tokenise as the FRAGMENT "000 ml". No source contains that -- the
# source holds "14000ml" -- so a verbatim-correct figure was untraceable, the
# whole reply was replaced by the safe reply, and the one corrective retry was
# handed "000 ml" as the thing to fix. It failed the other way too: "1,500 mg"
# quoted against a source saying "500 mg" TRACED, because the fragment matched.
# The comma'd branch must come FIRST -- alternation is ordered, and the plain
# branch would otherwise claim "14" and leave the rest behind.
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT_VALUE_RE = re.compile(
    r"\b\d{2,3}\s*/\s*\d{2,3}(?:\s*mmhg)?"
    rf"|\b(?:{_NUM})\s?%"
    rf"|\b(?:{_NUM})\s?(?:mg/dl|mmhg|mmol/l|mcg|mg|g/dl|g|ml|iu|bpm|kg)\b",
    re.IGNORECASE,
)


# Devanagari, Bengali, Gujarati, Gurmukhi, Odia, Tamil, Telugu, Kannada and
# Malayalam digit blocks, folded to ASCII before comparison — a translation
# that (correctly) rendered "104" as "१०४" used to TRIP the guard and
# permanently degrade digit-bearing translations, including the Tele-MANAS
# helpline number (audit low).
_INDIC_DIGIT_FOLD = {}
for _base in (0x0966, 0x09E6, 0x0AE6, 0x0A66, 0x0B66, 0x0BE6, 0x0C66,
              0x0CE6, 0x0D66):
    for _d in range(10):
        _INDIC_DIGIT_FOLD[_base + _d] = ord("0") + _d


def digits_preserved(source: str, translated: str) -> bool:
    """True when every digit sequence survived a transformation unchanged.

    Used on the translation pivot: IndicTrans2 is a pure MT model and will
    happily renumber things. A mismatch fails the whole translation and the
    English reply is shown instead.

    ORDER-SENSITIVE on purpose: "take 2 of the 500mg" and "take 500 of the
    2mg" contain the same multiset of digit runs, and only one of them is a
    dose the reader survives (audit medium — the Counter compare passed a
    dose<->strength swap). Native-script digits are folded to ASCII first.
    """
    src = source.translate(_INDIC_DIGIT_FOLD)
    dst = translated.translate(_INDIC_DIGIT_FOLD)
    return _DIGITS_RE.findall(src) == _DIGITS_RE.findall(dst)


def _normalize(value: str) -> str:
    """Collapse whitespace, case and thousands separators.

    So '128 / 84 mmHg' == '128/84mmhg', and a reply that writes the reader's
    own weekly total as '14,000 ml' compares equal to the '14000 ml' the tool
    returned. Both sides go through this, so it does not matter which one
    carries the comma -- and `format_wearable` already prints '12,000 steps'.
    """
    return re.sub(r"\s+", "", value.strip().lower()).replace(",", "")


def unit_values(text: str) -> list[str]:
    """Unit-bearing values found in the text, in order, as written."""
    return [m.group(0) for m in _UNIT_VALUE_RE.finditer(text)]


_PARTS_RE = re.compile(r"^(\d+(?:\.\d+)?)([a-z/%]+)$")

# Divisors a helpful answer actually uses on a period total: days in a week or
# a month, hours in a day, months/weeks in a year, and halves/thirds/quarters.
# NOT `range(2, 32)`: with thirty divisors and a percentage tolerance, a
# made-up figure has a real chance of landing on total/n by accident, and this
# guard is the reason that class of bug is visible at all.
_DIVISORS = (2, 3, 4, 7, 12, 24, 30, 31, 52, 365)

# Units a period total is measured in. Derivation is allowed ONLY for these.
#
# The divisors above are calendar arithmetic on a total, but 2, 3 and 4 are
# also dose-splitting arithmetic, and nothing about the shape distinguishes
# them. With derivation allowed on clinical units, a source holding
# "Metformin 500 mg" made "Take 250 mg twice a day" traceable, and a source
# holding a blood sugar of 240 mg/dL made "your blood sugar was 120 mg/dL"
# traceable — a halved lab value and a halved dose, both invented, both
# passing the guard whose whole purpose is to catch invented clinical values.
#
# A per-day average is only ever asked of a period total. It is never asked
# of a dose or a lab result, so refusing those costs nothing.
_DERIVABLE_UNITS = frozenset({
    "ml", "l", "h", "hr", "hrs", "hour", "hours", "min", "mins", "minute",
    "minutes", "steps", "step", "count", "kcal", "cup", "cups",
})


def _parts(value: str) -> tuple[float, str] | None:
    """``"2,000 ml"`` -> ``(2000.0, "ml")``. None for a shape with no single
    magnitude — a blood-pressure pair, which must never be derived."""
    m = _PARTS_RE.match(_normalize(value))
    if m is None:
        return None
    return float(m.group(1)), m.group(2)


def _derived(value: str, traced: list[str]) -> bool:
    """True when ``value`` is this reply's own arithmetic over a traced value.

    Dividing a weekly total by seven is the single most likely thing a helpful
    model does with a weekly total, and the guard replaced the WHOLE reply for
    it: "You logged 14,000 ml over the week - roughly 2,000 ml per day" was
    verbatim-correct in both halves and the reader got the safe reply.

    Deliberately narrow. Same unit, a calendar divisor, and a source value
    that actually exists: an invented figure with no arithmetic relationship
    to anything the tools returned still fails. The prompt asks the model not
    to derive at all (see `_GROUNDING_RULES`); this is what happens when it
    does anyway, and the alternative is throwing a correct answer away.
    """
    got = _parts(value)
    if got is None or got[0] <= 0:
        return False
    magnitude, unit = got
    # A dose or a lab value is never a period total, so it is never derived.
    # See _DERIVABLE_UNITS.
    if unit not in _DERIVABLE_UNITS:
        return False
    for source in traced:
        have = _parts(source)
        if have is None or have[1] != unit or have[0] <= 0:
            continue
        for n in _DIVISORS:
            # Models round: "roughly 2,000 ml a day" from 14,000 / 7 is exact,
            # "about 2,140" from 15,000 / 7 is not.
            if abs(have[0] / n - magnitude) <= max(0.5, magnitude * 0.01):
                return True
    return False


def values_traceable(reply: str, sources: list[str]) -> tuple[bool, list[str]]:
    """Check every clinical value in ``reply`` against the supplied sources.

    Returns ``(ok, untraceable_values)``. With no sources there is nothing to
    check — a general education answer cites no patient data and is out of
    scope for this guard.

    Comparison is on the normalized form, so the model may reformat a value
    ("128/84 mmHg" from "128/84mmhg") but not change it.
    """
    if not sources:
        return True, []
    haystack = _normalize(" ".join(sources))
    # The boundaries reject a value that is really PART of a longer number --
    # "62" inside "162" or "62.5" -- and nothing else. A bare dot on either
    # side is a sentence boundary, not a decimal point: whitespace is stripped
    # before the compare, so a source ending "...62 bpm. That is what your
    # device recorded" normalises to "62bpm.thatis", and the old `(?![\d.])`
    # refused to match it. Every deterministic reply that ENDS in a unit value
    # -- a vitals line, a lab line, a wearable line -- was untraceable, and the
    # model quoting one back had its whole reply replaced by the safe reply.
    stray = [
        value
        for value in unit_values(reply)
        if not re.search(
            rf"(?<!\d)(?<!\d\.){re.escape(_normalize(value))}(?!\d)(?!\.\d)",
            haystack,
        )
    ]
    if stray:
        traced = unit_values(" ".join(sources))
        stray = [v for v in stray if not _derived(v, traced)]
    # Preserve the reply's own casing and spacing for a legible log line, and
    # de-duplicate so one repeated value is reported once.
    return (not stray), sorted(set(stray))

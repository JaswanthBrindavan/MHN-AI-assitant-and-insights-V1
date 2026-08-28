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
_UNIT_VALUE_RE = re.compile(
    r"\b\d{2,3}\s*/\s*\d{2,3}(?:\s*mmhg)?"
    r"|\b\d+(?:\.\d+)?\s?%"
    r"|\b\d+(?:\.\d+)?\s?(?:mg/dl|mmhg|mmol/l|mcg|mg|g/dl|g|ml|iu|bpm|kg)\b",
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
    """Collapse whitespace and case so '128 / 84 mmHg' == '128/84mmhg'."""
    return re.sub(r"\s+", "", value.strip().lower())


def unit_values(text: str) -> list[str]:
    """Unit-bearing values found in the text, in order, as written."""
    return [m.group(0) for m in _UNIT_VALUE_RE.finditer(text)]


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
    stray = [
        value
        for value in unit_values(reply)
        if not re.search(
            rf"(?<![\d.]){re.escape(_normalize(value))}(?![\d.])", haystack
        )
    ]
    # Preserve the reply's own casing and spacing for a legible log line, and
    # de-duplicate so one repeated value is reported once.
    return (not stray), sorted(set(stray))

"""Deterministic medication add / stop / remove / list — the whole transaction,
in the shared prologue, on BOTH engines.

Why this exists: leaving the add flow to the agentic model made it *sometimes*
deflect mid-conversation ("I'll keep this general — discuss with a clinician")
instead of completing, because the safety rules and the tool permission pull in
opposite directions and the model weighs them afresh every turn. A transaction
must not be probabilistic. Here the intent, the schedule slot-fill, the
confirmation and the write are all deterministic, so the flow completes every
time; the model only ever handles genuinely conversational turns.

State across turns lives on the LAST assistant message's
``extracted_intent.pending_med`` (an existing JSON column — no new table):

    {"stage": "await_schedule"|"confirm", "action": "add"|"remove",
     "name": str, "strength": str|None,
     "schedule_pattern": str|None, "is_prn": bool}

Placement (see orchestrator): AFTER the triage floor and emergency directive
(so an emergency typed mid-flow still wins) and BEFORE the scope guard (so a
bare answer like "yes, morning and night" is not declined as off-topic).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("davi.medication_flow")

# Doses/day -> the slot letters Spring's schedulePattern expects (M/A/E/N).
_SLOTS_BY_COUNT = {1: "M", 2: "ME", 3: "MAE", 4: "MAEN"}
_SLOT_WORD_TO_LETTER = {"morning": "M", "afternoon": "A", "noon": "A",
                        "evening": "E", "night": "N", "bedtime": "N"}
_NUM_WORD = {"once": 1, "one": 1, "twice": 2, "two": 2, "thrice": 3, "three": 3,
             "four": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# --- safety guard: despair / overdose language NEVER enters this flow --------
# The medication flow only runs at NONE risk, but the triage tables have known
# gaps — so this module keeps its own tripwire. A message carrying any of
# these markers is released untouched (return None) at every stage: it must
# never become an add/stop/remove, never be read as a schedule, and never
# count as a confirmation.
_SELF_HARM_RE = re.compile(
    r"\b(?:want to die|end (?:it|my life|it all)|kill (?:myself|me)|"
    r"hurt (?:myself|me)|suicid\w*|overdos\w*|od'?d\b|lethal|"
    r"don'?t want to (?:live|be here|be alive|wake)|"
    r"stop (?:living|breathing)|no reason to live|done with life|"
    r"(?:took|take|taken|swallowed)\s+(?:all|too many|the whole|them all)|"
    r"whole (?:bottle|strip|pack)|cutting myself|cut my wrists|hang myself|"
    r"end everything|be dead|ready to die|hospice|wanting to (?:die|be alive)|"
    r"stopped wanting to (?:live|be alive)|rid of me\b|"
    r"delete me\b|remove me from this world)", re.I)

# --- intent detection (deterministic; the model never sees these turns) ------
_ADD_VERB = (r"\b(?:add|start(?:ed|ing)?|begin|put me on|get me (?:started|going)"
             r"(?: on)?|now (?:on|taking)|prescribed|been prescribed)\b")
_STOP_VERB = (r"\b(?:stop(?:ped)?|finish(?:ed)?|complete[d]?|done with|"
              r"no longer (?:taking|on)|(?:came|come|coming) off)\b")
_REMOVE_VERB = (r"\b(?:remove|delete|take (?:it |them |me )?off|get rid of|"
                r"clear .*meds?\b)\b")
# LIST: only the reader's OWN list — "my meds", "what am I on". A generic
# knowledge ask ("list of common medications") must not dump their record.
_LIST_RE = re.compile(
    r"\b(?:list|show)\b[^?]*\bmy\b[^?]*\bmed(?:ication)?s?\b|"
    r"\bmy (?:medications?|meds)\b\s*(?:list|please|\?|$)|"
    r"\bwhat med(?:ication)?s? (?:am i|do i)\b|"
    r"\bwhich med(?:ication)?s? (?:am i|do i)\b", re.I)

# A medication signal: a dose unit, or a candidate drug name near the verb.
_DOSE_UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|iu|g)\b", re.I)
_NAME_NEAR_VERB_RE = re.compile(r"\b[a-z][a-z][a-z-]+\b(?:\s+\d{1,4})?", re.I)
# Question-shaped framing. Two families: (1) inverted question form — a modal
# directly governing a pronoun ("should I", "do I", "will it") — so a REPORT
# like "doctor said I should start X" is NOT caught; (2) deliberative phrases
# ("wondering if", "is it fine to", "any harm if", Indian-English "ok na").
# can(?!['’t]) keeps "can't take it anymore, stop it" a statement.
_INTERROGATIVE_RE = re.compile(
    r"\b(?:should|c(?:an|ould)(?!['’]?t)|may|shall|would|do(?:es)?|will|"
    r"am|need)\s+(?:i|we|you|it|one|my)\b"
    r"|\bis it\b|\bis there\b|\bany (?:harm|risk|problem|issue|need)\b"
    r"|\bwondering\b|\bsuppose\b|\bwhether\b|\bnot sure\b|\bbetter to\b"
    r"|\bok(?:ay)? na\b|\bok(?:ay)? to\b|\bfine to\b|\bsafe to\b"
    r"|\bwhat happens\b|\bwhat if\b|\bdo you think\b",
    re.I)

# Name-quality vocabularies. A "medication name" made of these is not a name:
# food/kitchen words (never a deterministic med), pronouns/descriptors (the
# actual drug is unidentified), organs/conditions (colloquial "sugar tablet",
# "thyroid medicine" — real commands, but only the reader's course list or the
# model can say WHICH drug), and app-object words (reminders, photos).
_FOOD_WORDS = frozenset(
    "sugar salt water tea coffee milk flour oil rice salad honey ghee butter "
    "lemon juice masala spice spices food breakfast lunch dinner snack "
    "olive".split())
_JUNK_WORDS = frozenset(
    "reminder reminders appointment appointments alarm alarms calendar note "
    "notes entry name account profile photo photos conversation chat waitlist "
    "study world homework workout marathon diet app".split())
_VAGUE_WORDS = frozenset(
    "one ones it that this those them thing stuff little white red blue "
    "yellow pink small big round oval half thyroid bp pressure cholesterol "
    "heart diabetes blood thinner blocker beta statin antibiotic antibiotics "
    "painkiller painkillers".split())
_BULK_RE = re.compile(r"\b(?:all|everything|every)\b", re.I)
# Romanized-Indic filler around a drug name ("mujhe dolo 650 add karo"): the
# deterministic name extractor would keep the filler, so defer to the LLM.
_ROMANIZED_RE = re.compile(
    r"\b(?:mujhe|mera|meri|mere|karo|kar do|kardo|band|hatao|chey|cheyyi|"
    r"cheyy?andi|naa|naaku|nuvvu|daal|jodo|shuru|se hatao|list chey|"
    r"hata do|hata\b|theesey|teesey|theeyi|aapu|nilipiv\w*)\b", re.I)

# Cyrillic homoglyphs that sneak into drug names via copy-paste ("metfоrmin").
_HOMOGLYPHS = str.maketrans("аеорсхуАЕОРСХУ", "aeopcxyAEOPCXY")


def _is_question(message: str) -> bool:
    return message.rstrip().endswith("?") or bool(
        _INTERROGATIVE_RE.search(message))


def _name_quality(name: str) -> str:
    """'ok' | 'vague' (real command, unidentifiable drug -> LLM) | 'junk'
    (not a medication at all -> release)."""
    tokens = [t for t in re.split(r"[^a-z0-9-]+", name.lower()) if t]
    if not tokens:
        return "junk"
    _units = {"mg", "mcg", "ml", "iu", "g", "tab", "tabs", "tablet"}
    words = [t for t in tokens if not t.isdigit() and t not in _units]
    if any(t in _JUNK_WORDS for t in words):
        return "junk"
    if words and all(t in _VAGUE_WORDS or t in _FOOD_WORDS for t in words):
        # "sugar tablet" / "the little white one" — a real command whose drug
        # only the course list or the model can identify.
        return "vague"
    if len(words) > 4:
        # A whole trailing clause swallowed into the "name" ("warfarin, I can
        # manage the clots myself now") — extraction failed; let the model read.
        return "vague"
    return "ok"


def _clean_name(raw: str) -> str:
    raw = re.sub(r"\b(?:tablet|tablets|tab|tabs|pill|pills|capsule|capsules|"
                 r"syrup|from|to|my|the|medication|medicine|meds|list|now|anymore|"
                 r"already|today|yesterday|new)\b", " ",
                 raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip(" .,-")
    return raw


def _extract_name_strength(message: str, verb_re: str) -> tuple[str, str | None]:
    """Everything after the action verb is the candidate name; a trailing
    strength (``500mg`` / bare ``650``) is split off."""
    after = re.split(verb_re, message, maxsplit=1, flags=re.I)
    tail = after[-1] if len(after) > 1 else message
    # strip a leading "me on"/"on"/"taking" the split may have left
    tail = re.sub(r"^\s*(?:me\s+)?(?:on|taking|with)\b", " ", tail, flags=re.I)
    strength = None
    m = _DOSE_UNIT_RE.search(tail)
    if m:
        # Normalise "500mg" -> "500 mg" so the strength Spring stores is uniform.
        strength = re.sub(r"(?<=\d)\s*(mg|mcg|ml|iu|g)\b", r" \1",
                          m.group(0), flags=re.I).strip()
        strength = re.sub(r"\s+", " ", strength)
        tail = tail[:m.start()] + " " + tail[m.end():]
    else:
        # bare number after the name, e.g. "dolo 650" -> strength "650"
        m2 = re.search(r"\b([a-z][a-z-]{2,})\s+(\d{2,4})\b", tail, flags=re.I)
        if m2:
            strength = m2.group(2)
    # drop any schedule clause from the name
    tail = re.split(r"\b(?:once|twice|thrice|\d+\s*times?|as\s*needed|"
                    r"when needed|prn|morning|afternoon|evening|night|daily|"
                    r"a day|per day|every day)\b", tail, flags=re.I)[0]
    name = _clean_name(tail)
    if strength and name:
        name = f"{name} {strength}".strip()
    return name, strength


def parse_schedule(text: str) -> tuple[str | None, bool] | None:
    """(schedule_pattern, is_prn) from a schedule phrase, or None if unreadable.

    'as needed' -> (None, True); 'twice a day' -> ('ME', False);
    'morning, afternoon and evening' -> ('MAE', False); '1-0-1' -> ('MN', ...);
    'TID' -> ('MAE', ...); 'every 8 hours' -> ('MAE', ...); 'SOS' -> PRN."""
    low = unicodedata.normalize("NFKC", text).lower()
    # Voice-to-text artifact: "to/too times a day" is "2 times a day".
    low = re.sub(r"\bto{1,2}\s+times\b", "2 times", low)
    if _SELF_HARM_RE.search(low):
        return None  # "4 times the dose to end it" is not a schedule
    # A weekly/monthly frequency is NOT expressible as a daily slot pattern —
    # returning a daily pattern for "3 times a week" would be a 7x overdose.
    if re.search(r"\b(?:a|per|every|each)\s+(?:week|month|fortnight)\b|"
                 r"\bweekly\b|\bmonthly\b|\balternate day\b|\bevery other\b",
                 low):
        return None
    # Negated phrases must not match: "not three times, twice a day" -> ME,
    # "not as needed, every morning" -> M. Strip the negated clause first.
    low = re.sub(
        r"\bnot?\s+(?:(?:once|twice|thrice|one|two|three|four|\d)\s*"
        r"(?:times?|x)?(?:\s*(?:a|per|/)?\s*(?:day|daily))?"
        r"|as[- ]?needed|prn"
        r"|(?:in the |at |every )?(?:morning|afternoon|noon|evening|night|"
        r"bedtime)s?)\b", " ", low)
    if re.search(r"\bas[- ]?needed\b|\bwhen (?:needed|required)\b|\bprn\b|"
                 r"\bsos\b|\bonly when\b|\bif (?:i have|needed)\b|"
                 r"\bas and when\b", low):
        return (None, True)
    # Indian prescription triplet "1-0-1" (morning-noon-night): a nonzero digit
    # in a position means a dose in that slot.
    m = re.search(r"\b([0-9])\s*-\s*([0-9])\s*-\s*([0-9])\b", low)
    if m:
        d = [int(m.group(i)) for i in (1, 2, 3)]
        if any(d) and all(x <= 3 for x in d):
            # Third position: with all three dosed (1-1-1, the with-meals TID
            # script) it is the dinner dose (E); as a lone or paired dose
            # (0-0-1, 1-0-1) it is the night dose (N).
            third = "E" if all(d) else "N"
            pat = "".join(
                letter for x, letter in zip(d, "MA" + third, strict=False)
                if x)
            return (pat, False)
    # Latin dosing abbreviations (common on Indian prescriptions).
    for abbrev, pat in (("od", "M"), ("bd", "ME"), ("bid", "ME"),
                        ("tid", "MAE"), ("tds", "MAE"), ("qid", "MAEN"),
                        ("qds", "MAEN"), ("qhs", "N"), ("hs", "N")):
        if re.search(rf"\b{abbrev}\b", low):
            return (pat, False)
    # Interval dosing: every N hours -> 24/N doses a day (capped at 4).
    m = re.search(r"\bevery\s+(\d{1,2})\s*(?:hours|hrs|hourly|h)\b", low)
    if m:
        hours = int(m.group(1))
        if 4 <= hours <= 24:
            return (_SLOTS_BY_COUNT[min(4, max(1, round(24 / hours)))], False)
    # Slot words, incl. plurals ("in the mornings") and meal names.
    slotmap = dict(_SLOT_WORD_TO_LETTER)
    slotmap.update({"breakfast": "M", "lunch": "A", "dinner": "E",
                    "supper": "E", "nite": "N", "mrng": "M", "eve": "E"})
    slots = [letter for w, letter in slotmap.items()
             if re.search(rf"\b{w}s?\b", low)]
    if slots:
        # de-dup, keep canonical M A E N order
        order = "MAEN"
        pat = "".join(sorted(set(slots), key=order.index))[:4]
        return (pat, False)
    m = re.search(r"\b(once|twice|thrice|one|two|three|four|[1-4])\b"
                  r"(?:\s*(?:times?|x))?\s*(?:a|per|/)?\s*(?:day|daily)\b", low)
    if m:
        n = _NUM_WORD.get(m.group(1))
        if n:
            return (_SLOTS_BY_COUNT[n], False)
    # A bare count ("2 times", "before food twice") only as a SHORT, direct
    # answer — in a long sentence "3 times" is usually not a daily frequency
    # ("3 times I forgot to take it last week").
    if len(low.split()) <= 4 and not re.search(r"\btab(?:let)?s?\b|\bpills?\b",
                                               low):
        # "...tablets" excluded: "4 tablets" is a quantity per dose, not
        # 4 doses a day.
        m = re.search(r"\b(once|twice|thrice|[1-4])\b(?:\s*(?:times?|x))?\b",
                      low)
        if m:
            n = _NUM_WORD.get(m.group(1))
            if n:
                return (_SLOTS_BY_COUNT[n], False)
    return None


_YES_RE = re.compile(r"^\s*(?:y+e+s+|yes|ya|haan|yeah|yep|yup|ok(?:ay)?|sure|"
                     r"correct(?!\s+me)|confirm|go ahead|do it|that'?s right|"
                     r"right|please do|add it)\b", re.I)
# A yes that carries a correction must not be taken at face value.
_CORRECTION_RE = re.compile(r"\bbut\b|\bnot\b|\bactually\b|\binstead\b|"
                            r"\bexcept\b|\bmake (?:it|that)\b|\bchange\b|"
                            r"\bthough\b|\bonly\b", re.I)
_NO_RE = re.compile(r"^\s*(?:no|nope|nah|don'?t|cancel|stop|wait|not? (?:right|"
                    r"correct)|never ?mind)\b", re.I)
# A reversal or refusal AFTER a leading yes: "yes, actually no",
# "yeah no, don't remove it" — never a confirmation.
_LATE_NO_RE = re.compile(r"\bno\b|\bdon'?t\b|\bcancel\b|\bnever ?mind\b|"
                         r"\bwait\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    """True/False only for a CLEAN yes/no. A yes that carries a question mark,
    a correction ('yes but twice a day'), a late reversal ('yes, actually no'),
    or crisis language is None — the flow must clarify, never write."""
    if text.rstrip().endswith("?") or _SELF_HARM_RE.search(text):
        return None
    # A leading quote/emoji must not defeat the anchors ('yes' in curly
    # quotes, "thumbs-up yes").
    text = re.sub(r"^[^\w]+", "", text)
    m = _YES_RE.search(text)
    if m:
        rest = text[m.end():]
        if _CORRECTION_RE.search(rest):
            return None  # "yes but ..." — a correction, clarify first
        if _LATE_NO_RE.search(rest):
            return False  # "yeah no, don't" — a reversal into refusal
        return True
    m = _NO_RE.search(text)
    if m:
        # "no wait yes" / "no, that's the right one — remove it": a reversal
        # AFTER a leading no is ambiguous — clarify, never guess.
        rest = text[m.end():]
        if re.search(r"\by+e+s+\b|\bya\b|\bhaan\b|\bthat'?s (?:right|"
                     r"the (?:right )?one)\b|\bdo it\b|\bgo ahead\b|"
                     r"\badd it\b|\bremove it\b", rest, re.I):
            return None
        return False
    return None


def _schedule_words(is_prn: bool, pat: str | None) -> str:
    if is_prn or not pat:
        return "as needed"
    words = {"M": "morning", "A": "afternoon", "E": "evening", "N": "night"}
    when = ", ".join(words[c] for c in pat if c in words)
    times = {1: "once", 2: "twice", 3: "three times", 4: "four times"}.get(
        len(pat), f"{len(pat)} times")
    return f"{times} a day ({when})"


_MED_WORD_RE = re.compile(r"\bmed(?:ication|icine|s)?\b|\bpill|\btablet|"
                          r"\bcapsule|\bsyrup\b", re.I)
_DRUG_NUM_RE = re.compile(r"\b[a-z][a-z-]{2,}\s+\d{2,4}\b", re.I)


def detect_intent(message: str) -> dict | None:
    """A fresh medication command, or None. Question-shaped framing is NOT a
    command ('should I stop metformin?' / 'any harm if I stop amlodipine' are
    questions, not writes); despair/overdose language never enters."""
    message = unicodedata.normalize("NFKC", message).translate(_HOMOGLYPHS)
    # "dolo650" -> "dolo 650" (but leave short tokens like b12 alone).
    message = re.sub(r"\b([a-z]{3,})(\d{2,4})\b", r"\1 \2", message,
                     flags=re.I)
    if _SELF_HARM_RE.search(message):
        return None
    if _is_question(message):
        # A question must never fire a WRITE — but a question-shaped pure
        # list request ("what medications am I on?") is a safe READ.
        if (_LIST_RE.search(message)
                and not re.search(_REMOVE_VERB + "|" + _STOP_VERB + "|"
                                  + _ADD_VERB, message, re.I)
                and not _DOSE_UNIT_RE.search(message)
                and not _DRUG_NUM_RE.search(message)):
            return {"action": "list"}
        return None
    if _ROMANIZED_RE.search(message):
        return None  # the LLM reads romanized-Indic phrasing; regex must not
    # A bulk request ("stop ALL my meds", "clear everything") must be
    # arbitrated by the model, never resolved as one fake drug — and must not
    # fall through to LIST.
    if (_BULK_RE.search(message) and _MED_WORD_RE.search(message)
            and re.search(_STOP_VERB + "|" + _REMOVE_VERB, message, re.I)):
        return None
    # Command verbs are checked BEFORE list: "remove atorvastatin from my meds"
    # ends in "my meds" but is a remove, not a list request.
    for action, verb in (("remove", _REMOVE_VERB), ("stop", _STOP_VERB),
                         ("add", _ADD_VERB)):
        if re.search(verb, message, re.I):
            name, strength = _extract_name_strength(message, verb)
            if not name:
                continue
            # "stop ALL my meds" / "remove everything" is a bulk request; two
            # drugs joined by "and" need splitting. Both go to the LLM.
            if _BULK_RE.search(name) or re.search(r"\band\b", name, re.I):
                return None
            quality = _name_quality(name)
            if quality == "junk":
                # "remove my profile photo", "I finished my homework" — not a
                # medication at all. (Non-junk misses still get the course-list
                # gate at the flow level.)
                return None
            if quality == "vague":
                return None  # real command, unidentifiable drug -> LLM layer
            # An add needs a HARD medication signal (dose, drug+number, or an
            # explicit "medication"/"pill" word). A bare "add X" is ambiguous
            # ("add salt to my food" is not a medication) — it is left to the
            # LLM capture layer, which knows a drug name from a foodstuff.
            if action == "add":
                has_dose = bool(_DOSE_UNIT_RE.search(message))
                has_signal = (has_dose or _DRUG_NUM_RE.search(message)
                              or _MED_WORD_RE.search(message))
                if not has_signal:
                    continue
                # A dose unit alone does not make food a medicine: "add 200g
                # of flour", "add 5ml olive oil" stay out entirely.
                name_words = [t for t in re.split(r"[^a-z-]+", name.lower())
                              if t]
                if any(t in _FOOD_WORDS for t in name_words):
                    return None
            sched = parse_schedule(message)
            out = {"action": action, "name": name, "strength": strength}
            if action == "add" and sched is not None:
                out["schedule_pattern"], out["is_prn"] = sched
            return out
    # A LIST request only when it is a pure listing ask — a specific drug+dose
    # in the message ("...my medication list" with a named drug) is ambiguous,
    # so it is left to the LLM capture layer rather than mis-read as a list.
    if (_LIST_RE.search(message) and not _DOSE_UNIT_RE.search(message)
            and not _DRUG_NUM_RE.search(message)):
        return {"action": "list"}
    return None


# --------------------------------------------------------------------------- #
# LLM capture layer — the model reads ANY phrasing into structured fields, but
# it only fills a form; the deterministic flow below owns the transaction, so
# the model can never deflect or falsely confirm a write. Gated behind a cheap
# verb check so it does not run on every message, and it fails CLOSED to the
# deterministic result (a bad/failed extraction just means "not a command").
# --------------------------------------------------------------------------- #
_MED_VERB_SIGNAL = re.compile(
    r"\b(?:add|start|starting|started|begin|begun|put me on|get me|now on|now "
    r"taking|prescrib|stop|stopp|finish|complete|done with|no longer|"
    r"(?:came|come|coming) off|clear|remove|delete|take (?:it|them|me) off|"
    r"get rid|list|show|which|what)\b",
    re.I)


# A command verb directly governing a candidate name ("start me on amlodipine"):
# ambiguous enough that the LLM should arbitrate whether the name is a drug.
_CMD_VERB_NAME = re.compile(
    r"\b(?:add|start(?:ed|ing)?|begin|put (?:me )?on|get me (?:started|going)|"
    r"now on|prescrib\w*|stop(?:ped)?|finish\w*|complete\w*|remove|delete)\b"
    r"\s+(?:me\s+on\s+|on\s+|my\s+|taking\s+)?[a-z][a-z-]{2,}", re.I)


def _looks_like_med_command(message: str) -> bool:
    message = unicodedata.normalize("NFKC", message).translate(_HOMOGLYPHS)
    if _SELF_HARM_RE.search(message) or _is_question(message):
        return False
    # Romanized-Indic phrasing around a drug or med word is a real command the
    # regex layer cannot read — that is exactly what the LLM layer is for.
    if _ROMANIZED_RE.search(message) and (
        _DRUG_NUM_RE.search(message) or _MED_WORD_RE.search(message)
        or _DOSE_UNIT_RE.search(message)
        or re.search(r"\b(?:band|hatao|hata|chey|cheyyi|shuru|daal|jodo|nikaal|"
                     r"karo|kardo|theesey|teesey|theeyi|aapu)\b.*\b[a-z]{4,}\b|"
                     r"\b[a-z]{4,}\b.*\b(?:band|hatao|hata|chey|cheyyi|shuru|daal|"
                     r"jodo|nikaal|karo|kardo|theesey|teesey|theeyi|aapu)\b", message, re.I)
    ):
        return True
    # A real command whose drug the regex cannot identify ("take me off the
    # blood thinner", "stopped taking my thyroid medicine", "stop it") is
    # EXACTLY what the LLM layer is for.
    for verb in (_REMOVE_VERB, _STOP_VERB, _ADD_VERB):
        if re.search(verb, message, re.I):
            name, _s = _extract_name_strength(message, verb)
            if name and _name_quality(name) == "vague":
                words = {t for t in re.split(r"[^a-z-]+", name.lower()) if t}
                food_only = words <= (
                    _FOOD_WORDS | {"of", "mg", "mcg", "ml", "iu", "g"})
                if not (food_only and not _MED_WORD_RE.search(message)):
                    return True
            break
    if not _MED_VERB_SIGNAL.search(message):
        return False
    # Food with only a dose unit is cooking, not medicine ("add 5ml olive
    # oil") — unless an explicit med word says otherwise ("my sugar tablet").
    if not _MED_WORD_RE.search(message):
        tail_words = set(re.split(r"[^a-z-]+", message.lower()))
        if tail_words & _FOOD_WORDS:
            return False
    if (re.search(r"\bmed(?:ication|icine|s)?\b|\bpill|\btablet|\bcapsule|"
                  r"\bsyrup|\bdose\b", message, re.I)
            or _DOSE_UNIT_RE.search(message)
            or _DRUG_NUM_RE.search(message)):
        return True
    # A verb governing a plausible name — let the LLM decide if it is a drug.
    if not _CMD_VERB_NAME.search(message):
        return False
    # ...but not when the "name" is an app object or a foodstuff.
    words = set(re.split(r"[^a-z-]+", message.lower()))
    return not (words & _JUNK_WORDS)


_EXTRACT_SYS = (
    "You extract a medication command from ONE user message for a health app. "
    "Reply with ONLY a compact JSON object and nothing else.\n"
    'Schema: {"is_command": boolean, "action": "add"|"stop"|"remove"|"list"|'
    '"none", "name": string, "strength": string_or_null, "times_per_day": '
    "integer_1_to_4_or_null, \"as_needed\": boolean}.\n"
    "is_command is true ONLY when the reader is telling you to add/start, "
    "stop/finish, remove/delete, or list THEIR OWN medications. A QUESTION "
    "about a medicine (\"should I stop X\", \"what is X for\", \"can I take X\") "
    "is is_command=false, action=none. name is the medicine only, no dose "
    'words (e.g. "metformin", "dolo 650"). strength is like "500 mg" or "650" '
    "or null. Set times_per_day only if a fixed daily count or specific slots "
    "were stated; set as_needed true only for as-needed/PRN. When unsure, "
    'return {"is_command": false, "action": "none", "name": "", "strength": '
    'null, "times_per_day": null, "as_needed": false}.'
)


async def _extract_via_llm(message: str, provider) -> dict | None:
    """Structured extraction. Returns a fresh-intent dict or None. Never raises."""
    if provider is None:
        return None
    try:
        raw = await provider.generate(system=_EXTRACT_SYS, user=message)
        blob = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(blob)
    except Exception:  # noqa: BLE001 — extraction is best-effort, fail to None
        logger.info("medication extraction failed; deterministic result stands",
                    exc_info=True)
        return None
    if not isinstance(data, dict) or not data.get("is_command"):
        return None
    action = str(data.get("action") or "none")
    if action not in ("add", "stop", "remove", "list"):
        return None
    name = _clean_name(str(data.get("name") or ""))
    if action != "list" and not name:
        return None
    out: dict = {"action": action, "name": name,
                 "strength": (str(data["strength"]).strip()
                              if data.get("strength") else None)}
    if action == "add":
        if data.get("as_needed"):
            out["is_prn"], out["schedule_pattern"] = True, None
        else:
            tpd = data.get("times_per_day")
            if isinstance(tpd, int) and not isinstance(tpd, bool) and 1 <= tpd <= 4:
                out["schedule_pattern"], out["is_prn"] = _SLOTS_BY_COUNT[tpd], False
    return out


async def _detect_fresh(message: str, provider) -> dict | None:
    """Deterministic fast-path, then the LLM capture layer for the long tail."""
    intent = detect_intent(message)
    if intent is not None:
        return intent
    if _looks_like_med_command(message):
        return await _extract_via_llm(message, provider)
    return None


async def _capture_schedule(message: str, provider) -> tuple[str | None, bool] | None:
    """Schedule from a free answer: deterministic first, LLM fallback."""
    sched = parse_schedule(message)
    if sched is not None:
        return sched
    got = await _extract_via_llm(f"add a medication, {message}", provider)
    if got and ("schedule_pattern" in got or got.get("is_prn")):
        return (got.get("schedule_pattern"), got.get("is_prn", False))
    return None


# --------------------------------------------------------------------------- #
# The turn handler
# --------------------------------------------------------------------------- #
def _reply(text: str, *, action: str = "medication_flow",
           pending: dict | None = None, ok: bool | None = None) -> dict:
    prov: dict = {"path": "medication_flow"}
    if ok is not None:
        prov["ok"] = ok
    # pending is carried out so the orchestrator persists it on the reply.
    return {"reply": text, "action": action, "provenance": prov,
            "pending_med": pending}


async def handle_medication_turn(
    db: AsyncSession, user_id: uuid.UUID, message: str,
    pending: dict | None, provider=None,
) -> dict | None:
    """Return a reply dict (with ``pending_med`` to persist on the reply), or
    None when this is not a medication turn — then the caller falls through to
    scope + the engine. Never raises to the caller: a write that fails is
    reported honestly, never confirmed falsely."""
    from app.chat.data_handlers import perform_medication_write
    from app.medicines.service import list_courses

    # Normalise once (fullwidth digits, homoglyphs); then the tripwire: any
    # despair/overdose language releases the turn UNTOUCHED at every stage —
    # never an add/stop/remove, never a schedule, never a confirmation.
    message = unicodedata.normalize("NFKC", message).translate(_HOMOGLYPHS)
    if _SELF_HARM_RE.search(message):
        return None

    # --- resume an in-flight flow ------------------------------------------
    if pending and pending.get("stage") == "await_schedule":
        if parse_yes_no(message) is False:
            return _reply("Okay, I won't add it. Tell me if you change your mind.")
        sched = await _capture_schedule(message, provider)
        if sched is None:
            # Re-ask once; if they still don't answer with a schedule, RELEASE
            # the flow (return None) so they aren't trapped — the message goes
            # to the normal pipeline and the draft self-clears.
            if pending.get("reasked") or detect_intent(message) is not None:
                return None
            return _reply(
                "Sorry — how often do you take "
                f"{pending['name']}? For example 'once a day', 'twice a day', "
                "or 'as needed'.",
                pending={**pending, "reasked": True})
        pat, prn = sched
        nxt = {**pending, "stage": "confirm", "schedule_pattern": pat,
               "is_prn": prn}
        return _reply(
            f"Just to confirm: add {pending['name']}, "
            f"{_schedule_words(prn, pat)} — shall I add it?",
            pending=nxt)

    if pending and pending.get("stage") == "confirm":
        yn = parse_yes_no(message)

        # A CORRECTION at the confirm step must never be swallowed by a
        # leading "yes": "yes but twice a day, not three times" would otherwise
        # write the OLD schedule. Any parseable schedule in the reply that
        # differs from the draft updates the draft and re-confirms — with a
        # yes ("yes but twice") or a no ("no, twice a day") alike.
        if pending.get("action") == "add":
            sched = parse_schedule(message)
            if sched is not None and sched != (
                pending.get("schedule_pattern"), pending.get("is_prn", False)
            ):
                pat, prn = sched
                nxt = {**pending, "schedule_pattern": pat, "is_prn": prn}
                nxt.pop("reasked", None)
                return _reply(
                    f"Got it — just to confirm: add {pending['name']}, "
                    f"{_schedule_words(prn, pat)} — shall I add it?",
                    pending=nxt)
        if yn is None and _CORRECTION_RE.search(message):
            # An assent carrying a correction we could NOT parse ("correct,
            # but it's Pan 40 not Pan 20"). Writing the draft would record the
            # wrong thing — re-extract the whole reply instead.
            got = await _extract_via_llm(message, provider)
            if got and got["action"] == "add":
                nxt = {"stage": "confirm", "action": "add",
                       "name": got["name"] or pending["name"],
                       "strength": got.get("strength") or pending.get("strength"),
                       "schedule_pattern": got.get(
                           "schedule_pattern", pending.get("schedule_pattern")),
                       "is_prn": got.get("is_prn",
                                         pending.get("is_prn", False))}
                return _reply(
                    f"Just to confirm: add {nxt['name']}, "
                    f"{_schedule_words(nxt['is_prn'], nxt['schedule_pattern'])} "
                    "— shall I add it?",
                    pending=nxt)
            return _reply(
                "I want to get this exactly right — tell me the medicine name "
                "and how often you take it, in one message.",
                pending=None)

        if yn is None:
            if pending.get("reasked") or detect_intent(message) is not None:
                return None  # release rather than trap
            return _reply(
                f"Sorry, I didn't catch that — should I "
                f"{'add' if pending['action'] == 'add' else 'remove'} "
                f"{pending['name']}? (yes / no)",
                pending={**pending, "reasked": True})
        if yn is False:
            verb = "add" if pending["action"] == "add" else "remove"
            return _reply(f"Okay, I won't {verb} it.")
        ability = await perform_medication_write(
            db, user_id, pending["action"], pending["name"],
            strength=pending.get("strength"), is_prn=pending.get("is_prn", False),
            schedule_pattern=pending.get("schedule_pattern"))
        return {**ability, "pending_med": None}

    # --- a fresh command ----------------------------------------------------
    intent = await _detect_fresh(message, provider)
    if intent is None:
        return None

    action = intent["action"]

    if action == "list":
        listed = await list_courses(user_id)
        if not listed.ok:
            return await _unavailable(listed.reason)
        if not listed.courses:
            return _reply("You have no active medications on record. You can add "
                          "one here, or in the Medications section of the app.")
        names = "; ".join(c.name for c in listed.courses)
        return _reply(
            f"Your active medications on record are: {names}. Private entries "
            "aren't shown. Don't change or stop any of these on your own — "
            "discuss changes with your prescriber.")

    if action == "add":
        name = intent["name"]
        if "schedule_pattern" in intent or intent.get("is_prn"):
            nxt = {"stage": "confirm", "action": "add", "name": name,
                   "strength": intent.get("strength"),
                   "schedule_pattern": intent.get("schedule_pattern"),
                   "is_prn": intent.get("is_prn", False)}
            return _reply(
                f"Just to confirm: add {name}, "
                f"{_schedule_words(nxt['is_prn'], nxt['schedule_pattern'])} — "
                "shall I add it?",
                pending=nxt)
        nxt = {"stage": "await_schedule", "action": "add", "name": name,
               "strength": intent.get("strength")}
        return _reply(
            f"I can add {name} — how often do you take it? For example 'once a "
            "day', 'twice a day' (morning and evening), or 'as needed'.",
            pending=nxt)

    # stop / remove — resolve first so we can confirm the exact course.
    # hard_signal: an explicit medication marker in the MESSAGE (dose, drug+
    # number, or a med word). Without one, "stop X" is only treated as a
    # medication command when X actually resolves against the reader's course
    # list — so "stop worrying" / "I stopped smoking" fall through to the
    # normal pipeline (tracker, LLM) instead of a wrong "couldn't find X in
    # your medications" reply.
    hard = bool(
        _DOSE_UNIT_RE.search(message)
        or re.search(r"\b[a-z][a-z-]{2,}\s+\d{2,4}\b", message, re.I)
        or re.search(r"\bmed(?:ication|icine|s)?\b|\bpill|\btablet|\bcapsule|"
                     r"\bsyrup|\bdose\b", message, re.I)
    )
    return await _handle_stop_remove(
        db, user_id, action, intent["name"], hard_signal=hard)


async def _handle_stop_remove(
    db, user_id, action: str, name: str, *, hard_signal: bool = True,
) -> dict | None:
    from app.chat.data_handlers import perform_medication_write
    from app.medicines.service import _resolve

    # Remove must see stopped courses too; stop only acts on active ones.
    resolved = await _resolve(user_id, name, active_only=(action == "stop"))
    if not resolved.ok:
        if resolved.reason == "ambiguous":
            names = ", ".join(c.name for c in resolved.courses[:4])
            return _reply(
                f"You have more than one medication matching '{name}' "
                f"({names}). Which one did you mean?")
        # No hard medication marker AND nothing on the course list (or the
        # list is unreachable): this was probably never a medication command
        # ("stop worrying", "I stopped smoking") — release it to the normal
        # pipeline rather than hijack the turn with a medication reply.
        if not hard_signal:
            return None
        if resolved.reason == "not_found":
            where = "active " if action == "stop" else ""
            return _reply(
                f"I couldn't find an {where}'{name}' in your medications, so "
                "there's nothing to "
                f"{'stop' if action == 'stop' else 'remove'}. You can check the "
                "Medications section in the app.")
        return await _unavailable(resolved.reason)

    course = resolved.course
    if course is None:
        return _reply(
            f"I couldn't find '{name}' in your medications, so there's nothing "
            "to change. You can check the Medications section in the app.")
    exact = course.name.lower() == name.strip().lower()
    if action == "remove" or not exact:
        # Destructive, or a fuzzy match — confirm the exact course by name.
        verb = "remove" if action == "remove" else "stop"
        nxt = {"stage": "confirm", "action": action,
               "name": course.name if course else name}
        return _reply(
            f"Did you mean {course.name}? Shall I {verb} it?", pending=nxt)
    # Exact active match, non-destructive stop — do it.
    ability = await perform_medication_write(db, user_id, "stop", course.name)
    return {**ability, "pending_med": None}


async def _unavailable(reason: str | None) -> dict:
    from app.chat.data_handlers import _MED_UNAVAILABLE
    if reason in ("not_configured", "no_token"):
        return _reply(_MED_UNAVAILABLE, action="none", ok=False)
    return _reply(
        "I couldn't update that just now — please try again in a moment, or use "
        "the Medications section of the app.", action="none", ok=False)

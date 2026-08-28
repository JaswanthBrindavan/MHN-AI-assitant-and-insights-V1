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
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("davi.medication_flow")

# Doses/day -> the slot letters Spring's schedulePattern expects (M/A/E/N).
_SLOTS_BY_COUNT = {1: "M", 2: "ME", 3: "MAE", 4: "MAEN"}
_SLOT_WORD_TO_LETTER = {"morning": "M", "afternoon": "A", "noon": "A",
                        "evening": "E", "night": "N", "bedtime": "N"}
_NUM_WORD = {"once": 1, "one": 1, "twice": 2, "two": 2, "thrice": 3, "three": 3,
             "four": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# --- intent detection (deterministic; the model never sees these turns) ------
_ADD_VERB = (r"\b(?:add|start(?:ed|ing)?|begin|put me on|get me (?:started|going)"
             r"(?: on)?|now (?:on|taking)|prescribed|been prescribed)\b")
_STOP_VERB = (r"\b(?:stop(?:ped)?|finish(?:ed)?|complete[d]?|done with|"
              r"no longer (?:taking|on)|came? off)\b")
_REMOVE_VERB = (r"\b(?:remove|delete|take (?:it |them )?off|get rid of|"
                r"clear .*med)\b")
_LIST_RE = re.compile(
    r"\b(?:list|show|what(?:'s| are| is)?|which)\b[^?]*\bmedications?\b|"
    r"\bmy (?:medications?|meds)\b\s*(?:list|please|\?|$)|"
    r"\bwhat medications? (?:am i|do i)\b", re.I)

# A medication signal: a dose unit, or a candidate drug name near the verb.
_DOSE_UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|iu|g)\b", re.I)
_NAME_NEAR_VERB_RE = re.compile(r"\b[a-z][a-z][a-z-]+\b(?:\s+\d{1,4})?", re.I)
_INTERROGATIVE_RE = re.compile(
    r"\b(?:should|can|could|may|shall|would|is it (?:safe|ok|okay)|"
    r"what happens|what if|do you think)\b", re.I)


def _clean_name(raw: str) -> str:
    raw = re.sub(r"\b(?:tablet|tablets|tab|tabs|pill|pills|capsule|capsules|"
                 r"syrup|from|to|my|the|medication|medicine|meds|list)\b", " ",
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
    'morning, afternoon and evening' -> ('MAE', False)."""
    low = text.lower()
    if re.search(r"\bas[- ]?needed\b|\bwhen (?:needed|required)\b|\bprn\b|"
                 r"\bonly when\b|\bif (?:i have|needed)\b", low):
        return (None, True)
    slots = [_SLOT_WORD_TO_LETTER[w] for w in
             ("morning", "afternoon", "noon", "evening", "night", "bedtime")
             if re.search(rf"\b{w}\b", low)]
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
    m = re.search(r"\b([1-4])\s*(?:times?|x)\b", low)
    if m:
        return (_SLOTS_BY_COUNT[int(m.group(1))], False)
    return None


_YES_RE = re.compile(r"^\s*(?:yes|yeah|yep|yup|ok(?:ay)?|sure|correct|confirm|"
                     r"go ahead|do it|that'?s right|right|please do|add it)\b",
                     re.I)
_NO_RE = re.compile(r"^\s*(?:no|nope|nah|don'?t|cancel|stop|wait|not? (?:right|"
                    r"correct)|never ?mind)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    if _YES_RE.search(text):
        return True
    if _NO_RE.search(text):
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


def detect_intent(message: str) -> dict | None:
    """A fresh medication command, or None. Interrogative framing is NOT a
    command ('should I stop metformin?' is a question, not a stop)."""
    if _INTERROGATIVE_RE.search(message):
        return None
    # Command verbs are checked BEFORE list: "remove atorvastatin from my meds"
    # ends in "my meds" but is a remove, not a list request.
    for action, verb in (("remove", _REMOVE_VERB), ("stop", _STOP_VERB),
                         ("add", _ADD_VERB)):
        if re.search(verb, message, re.I):
            name, strength = _extract_name_strength(message, verb)
            if not name:
                continue
            # An add needs a HARD medication signal (dose, drug+number, or an
            # explicit "medication"/"pill" word). A bare "add X" is ambiguous
            # ("add salt to my food" is not a medication) — it is left to the
            # LLM capture layer, which knows a drug name from a foodstuff.
            if action == "add" and not (
                _DOSE_UNIT_RE.search(message)
                or re.search(r"\b[a-z][a-z-]{2,}\s+\d{2,4}\b", message, re.I)
                or re.search(r"\bmed(?:ication|icine|s)?\b|\bpill|\btablet|"
                             r"\bcapsule|\bsyrup\b", message, re.I)
            ):
                continue
            sched = parse_schedule(message)
            out = {"action": action, "name": name, "strength": strength}
            if action == "add" and sched is not None:
                out["schedule_pattern"], out["is_prn"] = sched
            return out
    # A LIST request only when it is a pure listing ask — a specific drug+dose
    # in the message ("...my medication list" with a named drug) is ambiguous,
    # so it is left to the LLM capture layer rather than mis-read as a list.
    if (_LIST_RE.search(message) and not _DOSE_UNIT_RE.search(message)
            and not re.search(r"\b[a-z][a-z-]{2,}\s+\d{2,4}\b", message, re.I)):
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
    r"taking|prescrib|stop|stopp|finish|complete|done with|no longer|came? off|"
    r"remove|delete|take (?:it|them|me) off|get rid|list|show|which|what)\b",
    re.I)


# A command verb directly governing a candidate name ("start me on amlodipine"):
# ambiguous enough that the LLM should arbitrate whether the name is a drug.
_CMD_VERB_NAME = re.compile(
    r"\b(?:add|start(?:ed|ing)?|begin|put (?:me )?on|get me (?:started|going)|"
    r"now on|prescrib\w*|stop(?:ped)?|finish\w*|complete\w*|remove|delete)\b"
    r"\s+(?:me\s+on\s+|on\s+|my\s+|taking\s+)?[a-z][a-z-]{2,}", re.I)


def _looks_like_med_command(message: str) -> bool:
    if _INTERROGATIVE_RE.search(message):
        return False
    if not _MED_VERB_SIGNAL.search(message):
        return False
    if (re.search(r"\bmed(?:ication|icine|s)?\b|\bpill|\btablet|\bcapsule|"
                  r"\bsyrup|\bdose\b", message, re.I)
            or _DOSE_UNIT_RE.search(message)
            or re.search(r"\b[a-z][a-z-]{2,}\s+\d{2,4}\b", message, re.I)):
        return True
    # A verb governing a plausible name — let the LLM decide if it is a drug.
    return bool(_CMD_VERB_NAME.search(message))


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
    return await _handle_stop_remove(db, user_id, action, intent["name"])


async def _handle_stop_remove(db, user_id, action: str, name: str) -> dict:
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

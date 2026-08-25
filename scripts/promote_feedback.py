"""Turn a down-voted turn into a regression case.

This is the half that makes feedback a LOOP rather than a suggestion box.
The acceptance bar for Task 22 is that a bad reply becomes a regression test
in under a minute, and a minute is only achievable if the promotion is one
command rather than a hand-written JSON edit.

    python -m scripts.promote_feedback --list
    python -m scripts.promote_feedback <feedback-id> --name my_case
    python -m scripts.promote_feedback <feedback-id> --dry-run

What it writes into evals/quality_cases.json is deliberately MINIMAL: the
question, and `addresses` seeded from the question's own content words. It
does NOT write the reply that was down-voted — that reply was judged wrong,
so freezing it as the expectation would enshrine the defect. A human edits
the case to say what a good answer looks like; the script's job is to get
the question into the file with the surrounding structure correct.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import uuid

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.chat import ConversationMessage
from app.models.common import utcnow
from app.models.feedback import TurnFeedback

CASES_PATH = pathlib.Path(__file__).resolve().parent.parent / "evals" / "quality_cases.json"

# Words that carry no signal about what the answer must address.
_STOP = {
    "what", "when", "where", "which", "who", "why", "how", "is", "are", "was",
    "were", "do", "does", "did", "can", "could", "should", "would", "will",
    "the", "a", "an", "my", "me", "i", "you", "it", "that", "this", "of",
    "for", "and", "or", "to", "in", "on", "at", "with", "about", "from",
    "have", "has", "had", "be", "been", "am", "so", "if", "any", "get",
    "got", "there", "here", "some", "very", "much", "many", "just",
}


def _slug(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]
    return "_".join(words[:4]) or "feedback_case"


def _addresses(text: str) -> str:
    """Content words the reply should engage with.

    Crude on purpose — a starting point a human corrects, not a judgement.
    """
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return " ".join(seen[:3])


async def _load(feedback_id: uuid.UUID | None) -> list[tuple[TurnFeedback, str | None]]:
    async with get_sessionmaker()() as db:
        if feedback_id is not None:
            # An explicit id promotes THAT row, whatever its rating — a
            # maintainer who names a row means it.
            stmt = select(TurnFeedback).where(TurnFeedback.id == feedback_id)
        else:
            stmt = (
                select(TurnFeedback)
                .where(
                    TurnFeedback.rating == "down",
                    TurnFeedback.triaged_at.is_(None),
                )
                .order_by(TurnFeedback.created_at.desc())
                .limit(50)
            )
        rows = (await db.execute(stmt)).scalars().all()

        out: list[tuple[TurnFeedback, str | None]] = []
        for row in rows:
            reply = (
                await db.execute(
                    select(ConversationMessage).where(
                        ConversationMessage.id == row.message_id
                    )
                )
            ).scalars().first()
            question = None
            if reply is not None:
                q = (
                    await db.execute(
                        select(ConversationMessage)
                        .where(
                            ConversationMessage.session_id == reply.session_id,
                            ConversationMessage.role == "user",
                            ConversationMessage.created_at <= reply.created_at,
                        )
                        .order_by(ConversationMessage.created_at.desc())
                        .limit(1)
                    )
                ).scalars().first()
                question = q.message if q else None
            out.append((row, question))
        return out


async def _mark_triaged(feedback_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as db:
        row = (
            await db.execute(
                select(TurnFeedback).where(TurnFeedback.id == feedback_id)
            )
        ).scalars().first()
        if row is not None:
            row.triaged_at = utcnow()
            await db.commit()


def add_case(question: str, name: str, *, reason: str | None) -> dict:
    """Append a case to quality_cases.json. Returns the case written.

    Refuses to add a duplicate name — quality_eval keys its report by name,
    and two cases sharing one would silently hide a result.
    """
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    existing = {c["name"] for c in data["cases"]}
    candidate = name
    suffix = 2
    while candidate in existing:
        candidate = f"{name}_{suffix}"
        suffix += 1

    case = {
        "name": candidate,
        "message": question,
        "addresses": _addresses(question),
        # No `scripted` reply: the down-voted answer was the DEFECT. A human
        # fills in what a good answer looks like.
        "_from_feedback": reason or "down",
    }
    data["cases"].append(case)
    CASES_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feedback_id", nargs="?", help="the feedback row to promote")
    parser.add_argument("--list", action="store_true", help="show untriaged down-votes")
    parser.add_argument("--name", help="case name (default: derived from the question)")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = parser.parse_args()

    if args.list or not args.feedback_id:
        rows = asyncio.run(_load(None))
        if not rows:
            print("No untriaged down-votes.")
            return 0
        for row, question in rows:
            print(f"{row.id}  [{row.reason or '-'}]  {(question or '?')[:70]}")
            if row.comment:
                print(f"    reader said: {row.comment[:70]}")
        return 0

    try:
        feedback_id = uuid.UUID(args.feedback_id)
    except ValueError:
        print(f"Not a uuid: {args.feedback_id}", file=sys.stderr)
        return 2

    rows = asyncio.run(_load(feedback_id))
    if not rows:
        print(f"No feedback row {feedback_id}", file=sys.stderr)
        return 1
    row, question = rows[0]
    if not question:
        print(
            "That turn has no user message before it — nothing to replay. "
            "The conversation was probably deleted.",
            file=sys.stderr,
        )
        return 1

    name = args.name or _slug(question)
    if args.dry_run:
        print(json.dumps({"name": name, "message": question}, indent=2))
        return 0

    case = add_case(question, name, reason=row.reason)
    asyncio.run(_mark_triaged(feedback_id))
    print(f"Added case '{case['name']}' to {CASES_PATH.name} and marked triaged.")
    print("Now edit it: add `scripted` / `expects_tools` to say what GOOD looks like.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

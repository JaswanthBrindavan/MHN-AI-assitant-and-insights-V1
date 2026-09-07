"""Every reader-scoped read must decline a question about somebody else.

Audit H7 has now been fixed in four different places, each a different ROUTE
to the same mistake — answering about a relative from the reader's own rows:

    #72  the deterministic parsers      (parse_metric_query and two siblings)
    #83  the tool path                  (execute_tool / READER_ONLY_TOOLS)
    #85  the family handler             (added the correct answer at step 3.50)
    #86  the shared prologue            (handle_about_me_query at step 3.49)

Each fix closed the route that had been FOUND. None of them made the next
route fail in CI, which is why there was a fourth — discovered in production,
not by the ~4,100 tests that were passing at the time.

This file is the invariant instead of a fifth patch. It enumerates the places
that read the reader's own records and requires each to be classified, so
adding a new one is a decision somebody has to record rather than an omission
nobody notices. The pattern is borrowed from ``tests/test_erasure.py``, which
derives its table list from the mappers for the same reason.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from app.chat.abilities import names_another_person
from app.chat.tools.registry import EXECUTORS, READER_ONLY_TOOLS

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "app" / "chat" / "data_handlers.py"

#: Reads that return the READER's own records, scoped by ``user_id`` alone.
#: A handler calling one of these answers about the reader and nobody else.
READER_SCOPED_READS = {
    "medical_records",
    "latest_vital",
    "recent_lab_values",
    "lifestyle_totals",
    "active_medications",
}

#: Handlers that legitimately call a reader-scoped read while ALSO resolving a
#: family member — they pick the owner first and read under the sharing gate.
#: Adding a name here is a claim that the handler is family-aware; check it.
FAMILY_AWARE = {
    "handle_document_query",
    "handle_ai_result_query",
    "handle_family_record_query",
    "handle_family_list_query",
}

#: Tools that can stand for somebody other than the reader, so they must NOT
#: be in READER_ONLY_TOOLS. Everything else must be one or the other.
FAMILY_AWARE_TOOLS = {
    "get_documents",
    "get_document_ai_result",
    "get_family_members",
    "get_family_member_record",
}

#: Tools that read nothing about any person — reference data, or a write.
IMPERSONAL_TOOLS = {
    "lookup_medicine",
    "get_condition_guidance",
    "check_value_against_range",
    "analyze_image",
    "add_medication",
    "stop_medication",
    "remove_medication",
    "log_lifestyle_entry",
}


def _handlers_calling_a_reader_scoped_read() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    found: dict[str, ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("handle_"):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in READER_SCOPED_READS
            ):
                found[node.name] = node
                break
    return found


def _guards_against_another_person(node: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(sub, ast.Name) and sub.id == "names_another_person"
        for sub in ast.walk(node)
    )


def test_at_least_one_handler_is_found() -> None:
    """The scan must actually be scanning something.

    A rename that made ``_handlers_calling_a_reader_scoped_read`` return {} would
    turn every assertion below into a no-op — the exact shape of the dead
    guards this repo has already been bitten by.
    """
    assert len(_handlers_calling_a_reader_scoped_read()) >= 2


@pytest.mark.parametrize(
    "name", sorted(_handlers_calling_a_reader_scoped_read()),
)
def test_a_reader_scoped_handler_declines_or_is_family_aware(name: str) -> None:
    node = _handlers_calling_a_reader_scoped_read()[name]
    if name in FAMILY_AWARE:
        return
    assert _guards_against_another_person(node), (
        f"{name} reads the reader's own records and does not check "
        f"names_another_person. Asked about a relative it will answer from the "
        f"READER's rows — audit H7, which has now happened four times. Either "
        f"guard it, or add it to FAMILY_AWARE with a reason if it resolves the "
        f"member itself."
    )


@pytest.mark.parametrize("tool", sorted(EXECUTORS))
def test_every_tool_is_classified(tool: str) -> None:
    """A new tool must be sorted into one of three buckets, deliberately."""
    buckets = [
        tool in READER_ONLY_TOOLS,
        tool in FAMILY_AWARE_TOOLS,
        tool in IMPERSONAL_TOOLS,
    ]
    assert sum(buckets) == 1, (
        f"{tool} is in {sum(buckets)} of the three buckets. Every tool must be "
        f"exactly one of: READER_ONLY_TOOLS (declines a family ask), "
        f"FAMILY_AWARE_TOOLS (resolves the member itself), or IMPERSONAL_TOOLS "
        f"(reads nothing about a person)."
    )


def test_the_two_tool_sets_do_not_overlap() -> None:
    assert not (READER_ONLY_TOOLS & FAMILY_AWARE_TOOLS)


@pytest.mark.parametrize("relation", [
    # Every label the app's Family Connect picker offers. A reader can only
    # ask about someone using a word this recognises.
    "parent", "child", "grandparent", "grandchild", "sibling", "cousin",
    "parent-in-law", "child-in-law", "sibling-in-law", "spouse", "partner",
    "fiance", "guardian", "ward",
])
def test_every_relation_the_app_offers_is_understood(relation: str) -> None:
    """The picker and the parser must agree on what a relative is called.

    Measured on a real account: of the fourteen labels, only "cousin" and
    "grandchild" were recognised, so a reader who chose "Parent" could not say
    "my parent" and be understood.
    """
    assert names_another_person(f"what conditions does my {relation} have?"), (
        f"the app lets a reader label a connection {relation!r}, and the "
        f"assistant does not recognise that word"
    )


async def test_the_prologue_handler_declines_for_every_offered_relation(
    db_session,
) -> None:
    """End to end, not only through the predicate."""
    from app.chat.data_handlers import handle_about_me_query

    for relation in ("parent", "sibling", "spouse", "guardian", "child-in-law"):
        got = await handle_about_me_query(
            db_session, uuid.uuid4(), f"what health issues does my {relation} have?"
        )
        assert got is None, (relation, got)

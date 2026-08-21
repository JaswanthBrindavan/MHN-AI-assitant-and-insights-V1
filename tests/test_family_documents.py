"""Family-member document fetching: relation vocabulary beyond parents
(grandchild/grandson/…), gendered↔generic relation-name matching against
production's Relation rows, name-based asks ("Bhargava's reports"), and the
anti-hijack guarantee ("my son" never resolves a Grandson connection)."""

from __future__ import annotations

import uuid

from app.chat.abilities import find_relation, parse_document_query
from app.chat.data_handlers import handle_document_query
from app.coredata.service import (
    _relation_matches,
    resolve_family_member,
    resolve_family_member_by_name,
)
from app.models.common import utcnow
from app.models.core import User
from app.models.coredata import FamilyConnect, Relation, Report

VIEWER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
OWNER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1")


async def _link(db, *, name: str, inverse: str, viewer_is_requester=True):
    rel = Relation(name=name, inverse=inverse)
    db.add(rel)
    await db.flush()
    db.add(FamilyConnect(
        requester_id=VIEWER if viewer_is_requester else OWNER,
        acceptor_id=OWNER if viewer_is_requester else VIEWER,
        accepted=True, relation_id=rel.id,
        acc_read=True, req_read=True,  # both sides share documents
    ))
    await db.flush()


def _user(uid: uuid.UUID, name: str, login: str) -> User:
    return User(
        id=uid, name=name, email=f"{login}@example.com", user_name=login,
        health_card_number=login[:12], hashcode="x",
    )


# --------------------------------------------------------------------------- #
# Parser vocabulary
# --------------------------------------------------------------------------- #
def test_find_relation_covers_descendants_and_extended_family():
    assert find_relation("show my grandchild's reports") == "grandchild"
    assert find_relation("pull my grandson's scans") == "grandson"
    assert find_relation("my granddaughter's vaccination") == "granddaughter"
    assert find_relation("my grandkid's bills") == "grandchild"
    assert find_relation("my aunty's prescriptions") == "aunt"
    assert find_relation("my nephew's lab reports") == "nephew"
    # "my grandson" must never be read as "my son".
    assert find_relation("my grandson's reports") == "grandson"


def test_parse_document_query_by_name():
    q = parse_document_query("pull up bhargava's lab reports")
    assert q is not None and q.owner_name == "bhargava" and q.relation is None
    # Relation wins over the possessive when both are present.
    q = parse_document_query("show my grandchild's reports")
    assert q is not None and q.relation == "grandchild" and q.owner_name is None
    # Everyday possessives are not names.
    assert parse_document_query("show yesterday's reports") is None


# --------------------------------------------------------------------------- #
# Relation-name matching (production uses gendered AND generic names)
# --------------------------------------------------------------------------- #
def test_relation_matching_equivalence_and_word_boundaries():
    assert _relation_matches("grandchild", "Grandchild")
    assert _relation_matches("grandson", "Grandchild")
    assert _relation_matches("grandchild", "Grandson")
    assert _relation_matches("son", "Child")
    assert _relation_matches("grandfather", "Grandparent")
    assert _relation_matches("father", "Father")
    # Anti-hijack: substrings never match.
    assert not _relation_matches("son", "Grandson")
    assert not _relation_matches("mother", "Grandmother")
    assert not _relation_matches("father", "Grandfather")


async def test_resolve_grandchild_connection(db_session):
    # Viewer sent the request; the relations row names the OTHER person:
    # "Grandchild" — exactly the staging shape that failed.
    await _link(db_session, name="Grandchild", inverse="Grandparent")
    assert await resolve_family_member(db_session, VIEWER, "grandchild") == OWNER
    assert await resolve_family_member(db_session, VIEWER, "grandson") == OWNER
    # And the OTHER side asks for their grandparent.
    assert await resolve_family_member(db_session, OWNER, "grandfather") == VIEWER
    # "my son" must NOT resolve a grandchild connection.
    assert await resolve_family_member(db_session, VIEWER, "son") is None


async def test_resolve_by_name_prefix_both_directions(db_session):
    await _link(db_session, name="Grandchild", inverse="Grandparent")
    db_session.add(_user(OWNER, "Bhargava Ram", "bhargav"))
    await db_session.flush()
    assert await resolve_family_member_by_name(db_session, VIEWER, "bhargava") == OWNER
    assert await resolve_family_member_by_name(db_session, VIEWER, "bhargav") == OWNER
    assert await resolve_family_member_by_name(db_session, VIEWER, "ram") == OWNER
    assert await resolve_family_member_by_name(db_session, VIEWER, "someone") is None


async def test_resolve_by_name_requires_read_grant(db_session):
    rel = Relation(name="Grandchild", inverse="Grandparent")
    db_session.add(rel)
    await db_session.flush()
    db_session.add(FamilyConnect(
        requester_id=VIEWER, acceptor_id=OWNER, accepted=True,
        relation_id=rel.id, acc_read=False,  # no owner-side grant
    ))
    db_session.add(_user(OWNER, "Bhargava Ram", "bhargav"))
    await db_session.flush()
    assert await resolve_family_member_by_name(db_session, VIEWER, "bhargava") is None


# --------------------------------------------------------------------------- #
# End to end — the staging repro: "show my grandchild's reports"
# --------------------------------------------------------------------------- #
async def test_grandchild_documents_fetched_not_own(db_session):
    await _link(db_session, name="Grandchild", inverse="Grandparent")
    now = utcnow()
    db_session.add(Report(
        user_id=OWNER, filepath="reports/gc.pdf", private=False, created_at=now,
    ))
    db_session.add(Report(
        user_id=VIEWER, filepath="reports/mine.pdf", private=False, created_at=now,
    ))
    await db_session.flush()

    r = await handle_document_query(
        db_session, VIEWER, "show my grandchild's reports"
    )
    assert r is not None
    assert "your grandchild" in r["reply"]
    paths = {d["slug"] for d in r["documents"]}
    assert paths == {"gc.pdf"}  # the grandchild's report, never the viewer's


async def test_documents_by_name_end_to_end(db_session):
    await _link(db_session, name="Grandchild", inverse="Grandparent")
    db_session.add(_user(OWNER, "Bhargava Ram", "bhargav"))
    db_session.add(Report(
        user_id=OWNER, filepath="reports/gc2.pdf", private=False,
        created_at=utcnow(),
    ))
    await db_session.flush()

    r = await handle_document_query(
        db_session, VIEWER, "pull up bhargava's reports"
    )
    assert r is not None
    assert "Bhargava" in r["reply"]
    assert {d["slug"] for d in r["documents"]} == {"gc2.pdf"}


async def test_unshared_relation_gets_honest_not_found(db_session):
    r = await handle_document_query(
        db_session, VIEWER, "show my grandchild's reports"
    )
    assert r is not None
    assert "couldn't find a connected family member" in r["reply"]
    assert r["provenance"]["resolved"] is False

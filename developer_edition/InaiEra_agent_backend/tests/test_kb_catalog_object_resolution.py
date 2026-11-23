"""A knowledge base reference must reach UCS as a catalog object.

The reported symptom was a publish that could never succeed:

    Publish blocked — entitlement check failed for one or more objects:
    {'objects_checked': ['ACME Support FAQ', 'ACME Platform Docs'],
     'agent_reason': 'no matching grant', 'user_reason': 'no matching grant'}

Both halves of the cause were in the same field. The Builder's
"Select Knowledge to Search" multi-select stored each KB's DISPLAY
NAME in `knowledge_bases`, while two different consumers read that
same field with two different meanings:

  * handle_retrieval slugified it ("ACME Support FAQ" ->
    "acme_support_faq") and looked up a KnowledgeBase row, so
    retrieval worked and the chatbot answered correctly;
  * objects_to_check passed the raw string to UCS as a catalog
    object, and UCS's rbac_cache.decide() splits an object on "." into
    catalog/schema/object — so a dotless display name parses as a
    catalog-level, all-wildcard match and denies against every real
    grant, forever. Not "denied because unauthorised": denied because
    unanswerable.

The fix resolves a reference to the catalog object that actually
governs it, and — critically — leaves anything that is NOT one of this
platform's KBs untouched, so a Tool node's real catalog object is
still checked exactly as before. Dropping objects out of the plan
would reopen the §0.84 governance hole rather than fix this one.

These tests use the real Postgres this platform runs against, because
resolution is a database lookup and a stand-in would prove nothing
about the mapping.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, delete

from app.db import engine
from app.flow_compiler import (governed_objects, kb_catalog_object, kb_slug,
                               objects_to_check, resolve_kb)
from app.models import KnowledgeBase
from app.ucs_client import UCS_KB_CATALOG, UCS_KB_SCHEMA, kb_fqn


def _nodes(refs, tool_object=None):
    nodes = [{"id": "n1", "type": "trigger", "config": {}}]
    if tool_object:
        nodes.append({"id": "n2", "type": "tool", "config": {"catalog_object": tool_object}})
    nodes.append({"id": "n3", "type": "retrieval", "config": {"knowledge_bases": list(refs)}})
    return nodes


@pytest.fixture
def kb():
    """A real registered KB whose id is the slug of its display name —
    the ordinary case, and the one the report came from."""
    suffix = uuid.uuid4().hex[:8]
    name = f"Resolution Test {suffix}"
    kb_id = kb_slug(name)
    with Session(engine) as s:
        s.add(KnowledgeBase(id=kb_id, name=name, provider="pgvector",
                            config={"table": "t"}, status="connected"))
        s.commit()
    yield kb_id, name
    with Session(engine) as s:
        s.exec(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        s.commit()


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


class TestTheReportedFailure:
    def test_a_display_name_becomes_a_three_part_catalog_object(self, session, kb):
        kb_id, name = kb
        resolved = kb_catalog_object(session, name)
        assert resolved == f"{UCS_KB_CATALOG}.{UCS_KB_SCHEMA}.{kb_id}"
        # Three parts is the whole point — UCS splits on "." and a
        # dotless name is read as a catalog-wide wildcard.
        assert resolved.count(".") == 2

    def test_the_raw_reference_really_was_unusable_as_an_object(self, session, kb):
        """Guards the diagnosis, not just the fix: if a display name ever
        became a legitimate 3-part object this test should fail and be
        re-read."""
        _kb_id, name = kb
        assert objects_to_check(_nodes([name])) == [name]
        assert "." not in name

    def test_publish_and_both_execution_paths_check_the_resolved_object(self, session, kb):
        kb_id, name = kb
        assert governed_objects(session, _nodes([name])) == [kb_fqn(kb_id)]


class TestEveryFormASavedConfigCanHold:
    def test_the_id_itself(self, session, kb):
        kb_id, _name = kb
        assert resolve_kb(session, kb_id).id == kb_id

    def test_the_display_name(self, session, kb):
        kb_id, name = kb
        assert resolve_kb(session, name).id == kb_id

    def test_a_renamed_kb_is_still_found_by_its_id(self, session, kb):
        """The latent bug the shared resolver also closes: slug(new name)
        no longer equals the id after a rename, so a slug-only lookup
        silently missed a real, connected KB — and retrieval returned a
        placeholder chunk instead of failing loudly."""
        kb_id, _name = kb
        row = session.get(KnowledgeBase, kb_id)
        row.name = "Totally Different Name"
        session.add(row)
        session.commit()
        assert resolve_kb(session, kb_id).id == kb_id
        assert kb_catalog_object(session, kb_id) == kb_fqn(kb_id)
        # And by the new name, which is what the Builder would store.
        assert resolve_kb(session, "Totally Different Name").id == kb_id


class TestWhatMustNotBeTouched:
    @pytest.mark.parametrize("obj", [
        "pg_sales.public.orders",
        "lakehouse_aws.demo.orders",
        "events.default.clicks_v1",
        "agent_catalog.agents.a1",
    ])
    def test_a_real_external_catalog_object_passes_through_unchanged(self, session, obj):
        assert kb_catalog_object(session, obj) == obj

    def test_a_tool_node_object_is_not_rewritten(self, session, kb):
        kb_id, name = kb
        objs = governed_objects(session, _nodes([name], tool_object="pg_sales.public.orders"))
        assert objs == ["pg_sales.public.orders", kb_fqn(kb_id)]

    def test_nothing_is_dropped_from_the_plan(self, session, kb):
        """The failure mode to avoid while fixing this: making publish
        succeed by quietly checking fewer objects. Count in == count
        out, always."""
        kb_id, name = kb
        nodes = _nodes([name, "pg_sales.public.orders", "not-a-registered-kb"],
                       tool_object="events.default.clicks_v1")
        assert len(governed_objects(session, nodes)) == len(objects_to_check(nodes)) == 4

    def test_an_unknown_reference_is_still_checked_rather_than_skipped(self, session):
        assert kb_catalog_object(session, "no-such-kb-anywhere") == "no-such-kb-anywhere"

    def test_an_empty_selection_still_yields_nothing(self, session):
        assert governed_objects(session, _nodes([])) == []


class TestTheKbCanDeclareItsOwnObject:
    def test_a_declared_catalog_object_wins_over_the_convention(self, session, kb):
        """A KB backed by an already-governed table should be governed as
        that table, not as a second, parallel object nobody grants."""
        kb_id, name = kb
        row = session.get(KnowledgeBase, kb_id)
        row.config = {**(row.config or {}), "catalog_object": "pg_sales.public.customers"}
        session.add(row)
        session.commit()
        assert kb_catalog_object(session, name) == "pg_sales.public.customers"
        assert kb_catalog_object(session, kb_id) == "pg_sales.public.customers"


class TestTheOneSharedResolver:
    def test_retrieval_and_governance_cannot_disagree_again(self):
        """harness._kb_lookup_id was a second, independent copy of the
        slugification. Two copies is how the two paths came to mean
        different things by the same string."""
        import inspect
        from app import harness
        assert "kb_slug" in inspect.getsource(harness._kb_lookup_id)
        assert "resolve_kb(session, kb_ref)" in inspect.getsource(harness.handle_retrieval)

    def test_publish_uses_the_resolved_objects(self):
        import inspect
        from app.routers.agents import _resolve_and_publish
        src = inspect.getsource(_resolve_and_publish)
        assert "governed_objects(session," in src
        assert "objects_to_check(a.flow" not in src

    def test_kb_fqn_is_configurable_and_three_part(self):
        assert kb_fqn("x") == f"{UCS_KB_CATALOG}.{UCS_KB_SCHEMA}.x"
        assert kb_fqn("x").count(".") == 2

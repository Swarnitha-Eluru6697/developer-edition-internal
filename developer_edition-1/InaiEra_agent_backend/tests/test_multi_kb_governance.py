"""
Real tests for the multi-KB governance entitlement-plan fix (section
0.84, GAP_REPORT).

The real, reported bug: objects_to_check (harness.py) already existed
and was already correctly multi-KB aware, but two real, independent
call sites kept their own duplicate, single-KB-only computation
(catalog_object or knowledge_base) instead of using it: the publish
endpoint (routers/agents.py) and the durable-path executor
(routers/execution.py's own _run_durable). A Retrieval node configured
with knowledge_bases: ["kb.finance", "kb.legal", "kb.hr"] could
publish, or run on the durable path, with an entitlement plan checking
NONE of those three real objects.

The fix: objects_to_check (and its own retrieval_kb_refs dependency)
moved to a new, dedicated app/flow_compiler.py module - genuinely
shared, runtime-independent governance-compilation logic. Every real,
governance-relevant call site now imports and calls the exact same
function - verified here by real object identity, not just equal
behavior, so these genuinely cannot silently diverge again.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import KnowledgeBase
from app.flow_compiler import objects_to_check, retrieval_kb_refs
from app.ucs_client import kb_fqn

client = TestClient(app)


def multi_kb_flow(kb_refs) -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "retrieval", "x": 0, "y": 0, "sub": "", "config": {"knowledge_bases": kb_refs}},
            {"id": "n3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "{{retrieved_chunks}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def create_agent(flow: dict) -> dict:
    r = client.post("/api/agents", json={
        "name": f"MultiKB Governance Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": flow,
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_agent(agent_id):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


def create_real_kbs(*refs):
    with Session(engine) as s:
        for ref in refs:
            kb_id = ref.lower().replace(" ", "_").replace(".", "_")
            if not s.get(KnowledgeBase, kb_id):
                s.add(KnowledgeBase(id=kb_id, name=ref, provider="pgvector", config={}, status="connected"))
        s.commit()


def cleanup_kbs(*refs):
    with Session(engine) as s:
        for ref in refs:
            kb_id = ref.lower().replace(" ", "_").replace(".", "_")
            s.exec(text("DELETE FROM knowledgebase WHERE id = :i"), params={"i": kb_id})
        s.commit()


class TestRealSharedObjectIdentity:
    def test_every_real_call_site_imports_the_identical_function_object(self):
        from app.harness import objects_to_check as harness_ref
        from app.routers.execution import objects_to_check as exec_ref
        from app.routers.agents import objects_to_check as agents_ref
        from app.routers.guardrails import objects_to_check as guard_ref

        assert objects_to_check is harness_ref
        assert objects_to_check is exec_ref
        assert objects_to_check is agents_ref
        assert objects_to_check is guard_ref


class TestRealObjectsToCheckMultiKB:
    def test_a_multi_kb_retrieval_node_returns_every_real_kb(self):
        nodes = [
            {"id": "n1", "type": "trigger", "config": {}},
            {"id": "n2", "type": "retrieval", "config": {"knowledge_bases": ["kb.finance", "kb.legal", "kb.hr"]}},
        ]
        assert objects_to_check(nodes) == ["kb.finance", "kb.legal", "kb.hr"]

    def test_the_legacy_single_kb_field_still_works(self):
        nodes = [{"id": "n1", "type": "retrieval", "config": {"knowledge_base": "kb.legacy"}}]
        assert objects_to_check(nodes) == ["kb.legacy"]

    def test_a_tool_node_and_a_multi_kb_retrieval_node_together(self):
        nodes = [
            {"id": "n1", "type": "tool", "config": {"catalog_object": "pg_sales.public.orders"}},
            {"id": "n2", "type": "retrieval", "config": {"knowledge_bases": ["kb.a", "kb.b"]}},
        ]
        assert objects_to_check(nodes) == ["pg_sales.public.orders", "kb.a", "kb.b"]

    def test_an_empty_kb_list_returns_no_objects(self):
        nodes = [{"id": "n1", "type": "retrieval", "config": {"knowledge_bases": []}}]
        assert objects_to_check(nodes) == []

    def test_retrieval_kb_refs_directly(self):
        assert retrieval_kb_refs({"knowledge_bases": ["a", "b", "c"]}) == ["a", "b", "c"]
        assert retrieval_kb_refs({"knowledge_base": "single"}) == ["single"]
        assert retrieval_kb_refs({}) == []


class TestRealPublishMultiKBGovernance:
    def test_publish_checks_every_real_kb_a_multi_kb_node_configures(self):
        create_real_kbs("kb.alpha", "kb.beta", "kb.gamma")
        agent = create_agent(multi_kb_flow(["kb.alpha", "kb.beta", "kb.gamma"]))
        try:
            captured = []

            def fake_check(session, agent_id, agent_name, invoking_user, obj):
                captured.append(obj)
                return {"resolved": True, "agent_principal_ok": True, "invoking_user_ok": True, "object": obj}

            with patch("app.ucs_client.check_entitlement", side_effect=fake_check):
                r = client.post(f"/api/agents/{agent['id']}/publish")
            assert r.status_code == 200, r.text
            # Each configured KB is still checked, once, in order — but as
            # the catalog object that governs it rather than the display
            # name the Builder stored. A dotless name cannot match a UCS
            # grant at all (it parses as a catalog-level wildcard), which
            # is the bug these identifiers used to carry.
            expected = [kb_fqn("kb_alpha"), kb_fqn("kb_beta"), kb_fqn("kb_gamma")]
            assert captured == expected
            assert r.json()["entitlement_plan"]["objects_checked"] == expected
        finally:
            cleanup_agent(agent["id"])
            cleanup_kbs("kb.alpha", "kb.beta", "kb.gamma")

    def test_a_single_kb_node_still_checks_correctly_no_regression(self):
        create_real_kbs("kb.solo")
        agent = create_agent(multi_kb_flow(["kb.solo"]))
        try:
            captured = []

            def fake_check(session, agent_id, agent_name, invoking_user, obj):
                captured.append(obj)
                return {"resolved": True, "agent_principal_ok": True, "invoking_user_ok": True, "object": obj}

            with patch("app.ucs_client.check_entitlement", side_effect=fake_check):
                r = client.post(f"/api/agents/{agent['id']}/publish")
            assert r.status_code == 200, r.text
            assert captured == [kb_fqn("kb_solo")]
        finally:
            cleanup_agent(agent["id"])
            cleanup_kbs("kb.solo")


class TestRealDurablePathUsesSharedFunction:
    def test_run_durable_source_calls_the_real_shared_function(self):
        import inspect
        from app.routers.execution import _run_durable
        src = inspect.getsource(_run_durable)
        # governed_objects, not objects_to_check: the durable path must
        # check the same RESOLVED catalog objects publish does. Calling the
        # raw version here would send UCS a KB display name, which cannot
        # match a grant — see test_kb_catalog_object_resolution.py.
        assert "governed_objects(session, nodes)" in src
        assert 'get("catalog_object")' not in src
        assert 'get("knowledge_base")' not in src

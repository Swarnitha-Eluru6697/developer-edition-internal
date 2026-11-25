"""
Real tests for the server-side FlowValidator fix (section 0.73,
GAP_REPORT).

The real, reported gap: frontend/src/flowValidation.ts is a real,
working validator, but it was the ONLY validator - the backend never
applied equivalent checks before publish, and NodeIn.type accepted any
string at all. A raw API caller (or a direct /import-dsl call) could
build and publish a flow the Builder's own UI would have visibly
flagged and blocked.

The fix: a real, comprehensive app/flow_validator.py, wired into
add_node (rejects unknown types at creation) and the publish endpoint
(blocks publish with structured, itemized errors). Also fixed along
the way, found while building this:
- add_edge used to reject the genuinely legitimate "success"/"error"
  handle a code/LLM node with "Fail branch" enabled needs.
- delete_node's container-children cascade only checked for "loop",
  silently orphaning an "iteration" container's own children.
- Three separate silent "skipped" paths for an unsupported node type
  reaching runtime now fail loudly instead.

These tests exercise the real, running API directly - not mocked.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app

client = TestClient(app)


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        # Real, more complete cleanup (§0.87, GAP_REPORT test fix) —
        # the original version here only cleared agentlog/agentversion,
        # since every real, existing test in this file only ever
        # published an agent, never ran it. TestRealNestedContainer
        # Rejection's own valid-loop test genuinely runs one too,
        # which leaves a real Run/Trace row a bare DELETE FROM agent
        # then violates via a real foreign key — cleared here so this
        # one, real, shared helper stays correct for any future test
        # that runs an agent too, not just this specific one.
        s.exec(text("DELETE FROM run WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM trace WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agentversion WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealAddNodeTypeValidation:
    def test_a_bogus_node_type_is_rejected_at_creation(self):
        r = client.post("/api/agents", json={
            "name": f"Validator Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/nodes", json={"type": "totally_bogus_type", "x": 0, "y": 0, "sub": "", "config": {}},
                              headers={"If-Match": "1"})
            assert r2.status_code == 400
            assert "not a real, supported node type" in r2.json()["detail"]
        finally:
            cleanup_agent(agent_id)

    def test_every_real_palette_type_is_accepted(self):
        r = client.post("/api/agents", json={
            "name": f"Validator Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        })
        agent_id = r.json()["id"]
        try:
            for real_type in ["llm", "code", "condition", "template", "http", "classifier", "aggregator"]:
                r2 = client.get(f"/api/agents/{agent_id}")
                rev = r2.json()["draft_revision"]
                r3 = client.post(f"/api/agents/{agent_id}/nodes", json={"type": real_type, "x": 0, "y": 0, "sub": "", "config": {}},
                                  headers={"If-Match": str(rev)})
                assert r3.status_code == 200, f"{real_type}: {r3.text}"
        finally:
            cleanup_agent(agent_id)


class TestRealPublishBlocking:
    def test_publish_blocks_a_workflow_with_no_output_node(self):
        r = client.post("/api/agents", json={
            "name": f"Validator Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                          {"id": "n2", "type": "respond", "x": 100, "y": 0, "sub": "", "config": {}}],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "missing_output_node" for i in issues)
        finally:
            cleanup_agent(agent_id)

    def test_a_genuinely_malformed_dsl_import_reports_every_real_issue_at_publish(self):
        r = client.post("/api/agents/import-dsl", json={
            "dsl": {
                "name": f"Malformed Import {uuid.uuid4().hex[:8]}", "app_type": "workflow",
                "flow": {
                    "nodes": [
                        {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                        {"id": "n2", "type": "totally_bogus_type", "x": 100, "y": 0, "sub": "", "config": {}},
                        {"id": "n3", "type": "condition", "x": 200, "y": 0, "sub": "", "config": {}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                        {"id": "e2", "source": "n2", "target": "does_not_exist", "source_handle": None},
                        {"id": "e3", "source": "n3", "target": "n1", "source_handle": "bogus_handle"},
                    ],
                },
            },
            "as_new_agent": True,
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            codes = {i["code"] for i in r2.json()["detail"]["issues"]}
            assert codes == {"unsupported_node_type", "invalid_config", "dangling_edge", "invalid_handle", "missing_output_node"}
        finally:
            cleanup_agent(agent_id)

    def test_publish_succeeds_for_a_genuinely_valid_flow(self):
        r = client.post("/api/agents", json={
            "name": f"Validator Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "output", "x": 100, "y": 0, "sub": "", "config": {"variables": [{"name": "result"}]}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 200, r2.text
        finally:
            cleanup_agent(agent_id)

    def test_publish_blocks_an_unreachable_disconnected_node(self):
        r = client.post("/api/agents", json={
            "name": f"Validator Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "output", "x": 100, "y": 0, "sub": "", "config": {"variables": [{"name": "result"}]}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                    {"id": "n4", "type": "llm", "x": 300, "y": 200, "sub": "", "config": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "unreachable_node" and i["node_id"] == "n4" for i in issues)
        finally:
            cleanup_agent(agent_id)


class TestRealFailBranchHandleFix:
    def test_a_code_nodes_success_and_error_handles_are_now_genuinely_accepted(self):
        r = client.post("/api/agents", json={
            "name": f"Fail Branch Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "code", "x": 100, "y": 0, "sub": "", "config": {"error_handling": "Fail branch", "code": "x=1"}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                    {"id": "n4", "type": "respond", "x": 200, "y": 100, "sub": "", "config": {}},
                ],
                "edges": [],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/edges", json={"source": "n2", "target": "n3", "source_handle": "success"},
                              headers={"If-Match": "1"})
            assert r2.status_code == 200, r2.text
            r3 = client.post(f"/api/agents/{agent_id}/edges", json={"source": "n2", "target": "n4", "source_handle": "error"},
                              headers={"If-Match": str(r2.json()["draft_revision"])})
            assert r3.status_code == 200, r3.text
        finally:
            cleanup_agent(agent_id)

    def test_a_plain_nodes_named_handle_is_still_rejected(self):
        r = client.post("/api/agents", json={
            "name": f"Fail Branch Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "llm", "x": 100, "y": 0, "sub": "", "config": {}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ],
                "edges": [],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/edges", json={"source": "n2", "target": "n3", "source_handle": "success"},
                              headers={"If-Match": "1"})
            assert r2.status_code == 400
        finally:
            cleanup_agent(agent_id)


class TestRealDeleteNodeIterationCascade:
    def test_deleting_an_iteration_container_removes_its_own_real_children(self):
        r = client.post("/api/agents", json={
            "name": f"Cascade Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "iteration", "x": 100, "y": 0, "sub": "", "config": {}},
                    {"id": "n3", "type": "llm", "x": 120, "y": 20, "sub": "", "config": {}, "parent_id": "n2"},
                    {"id": "n4", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ],
                "edges": [],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.delete(f"/api/agents/{agent_id}/nodes/n2", headers={"If-Match": "1"})
            assert r2.status_code == 200, r2.text
            node_ids = {n["id"] for n in r2.json()["flow"]["nodes"]}
            assert "n3" not in node_ids
        finally:
            cleanup_agent(agent_id)


class TestRealNestedContainerRejection:
    """Real tests for section 0.87, GAP_REPORT — a real, reported
    correctness gap: the validator explicitly allowed a real container
    (loop/iteration) to nest inside another real container, but
    neither real execution path (sync or Temporal/durable) actually
    supports it - a nested container reaches UnsupportedNodeType the
    moment execution tries to walk into it, since run_loop_body/
    run_iteration_body dispatch a body's own children via exec_node/
    NODE_HANDLERS, which has no real entry for "loop" or "iteration"
    (those two types are only ever handled by the OUTER traversal's
    own dispatch). Fixed by rejecting the nested case at the one, real,
    authoritative, publish-blocking validation layer - the report's
    own explicitly stated safer option, given real recursive support
    would need a substantial rewrite of both real execution paths,
    including Temporal's own determinism-constrained container model,
    which can't be live-verified end-to-end in this sandbox."""

    def test_a_loop_nested_inside_a_loop_is_rejected_at_publish(self):
        """The exact scenario from the bug report: Loop -> Loop -> LLM."""
        r = client.post("/api/agents", json={
            "name": f"Nested Loop Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "outer_loop", "type": "loop", "x": 100, "y": 0, "sub": "", "config": {"max_loops": 5}},
                    {"id": "inner_loop", "type": "loop", "x": 150, "y": 0, "sub": "", "config": {"max_loops": 5}, "parent_id": "outer_loop"},
                    {"id": "tmpl1", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "hi"}, "parent_id": "inner_loop"},
                ],
                "edges": [{"id": "e1", "source": "t1", "target": "outer_loop", "source_handle": None}],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "nested_container_unsupported" and i["node_id"] == "inner_loop" for i in issues)
        finally:
            cleanup_agent(agent_id)

    def test_an_iteration_nested_inside_a_loop_is_also_rejected(self):
        r = client.post("/api/agents", json={
            "name": f"Nested Iteration Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "loop1", "type": "loop", "x": 100, "y": 0, "sub": "", "config": {"max_loops": 5}},
                    {"id": "iter1", "type": "iteration", "x": 150, "y": 0, "sub": "", "config": {}, "parent_id": "loop1"},
                ],
                "edges": [{"id": "e1", "source": "t1", "target": "loop1", "source_handle": None}],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "nested_container_unsupported" and i["node_id"] == "iter1" for i in issues)
        finally:
            cleanup_agent(agent_id)

    def test_a_valid_non_nested_loop_still_publishes_and_runs_correctly(self):
        """No regression - a real, non-nested container (a regular
        node as the child, not another container) must still publish
        and genuinely execute exactly as before."""
        r = client.post("/api/agents", json={
            "name": f"Valid Loop Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "loop1", "type": "loop", "x": 100, "y": 0, "sub": "", "config": {"max_loops": 3}},
                    {"id": "tmpl1", "type": "template", "x": 150, "y": 0, "sub": "", "config": {"template": "hi {{loop_iteration}}"}, "parent_id": "loop1"},
                    {"id": "exit1", "type": "exit_loop", "x": 200, "y": 0, "sub": "", "config": {}, "parent_id": "loop1"},
                ],
                "edges": [
                    {"id": "e1", "source": "t1", "target": "loop1", "source_handle": None},
                    {"id": "e2", "source": "tmpl1", "target": "exit1", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 200, r2.text
            r3 = client.post(f"/api/agents/{agent_id}/run", json={"input": "test"})
            assert r3.status_code == 200, r3.text
            assert r3.json()["status"] == "success"
            assert r3.json()["output"] == "hi 1"
        finally:
            cleanup_agent(agent_id)

    def test_the_validator_function_directly_confirms_no_false_positive_on_a_regular_child(self):
        from app.flow_validator import validate_flow
        nodes = [
            {"id": "t1", "type": "trigger", "config": {}},
            {"id": "loop1", "type": "loop", "config": {"max_loops": 5}},
            {"id": "llm1", "type": "llm", "config": {"prompt": "hi"}, "parent_id": "loop1"},
        ]
        edges = [{"id": "e1", "source": "t1", "target": "loop1", "source_handle": None}]
        result = validate_flow(nodes, edges, app_type="chatflow")
        assert not any(i.code == "nested_container_unsupported" for i in result.errors)

    def test_three_levels_of_nesting_is_also_rejected_not_just_two(self):
        from app.flow_validator import validate_flow
        nodes = [
            {"id": "t1", "type": "trigger", "config": {}},
            {"id": "l1", "type": "loop", "config": {"max_loops": 5}},
            {"id": "l2", "type": "iteration", "config": {}, "parent_id": "l1"},
            {"id": "l3", "type": "loop", "config": {"max_loops": 5}, "parent_id": "l2"},
        ]
        edges = [{"id": "e1", "source": "t1", "target": "l1", "source_handle": None}]
        result = validate_flow(nodes, edges, app_type="chatflow")
        nested_errors = [i for i in result.errors if i.code == "nested_container_unsupported"]
        # Both l2 (nested in l1) and l3 (nested in l2) are real, separate violations.
        assert len(nested_errors) == 2
        assert {i.node_id for i in nested_errors} == {"l2", "l3"}


class TestRealRuntimeFailLoud:
    def test_an_unsupported_node_type_reaching_run_flow_sync_fails_loudly_not_silently(self):
        from app.harness import run_flow_sync
        from app.models import Agent

        agent_id = f"runtime-test-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(Agent(
                id=agent_id, name=agent_id, type="App", subtype="Custom", plane="App",
                owner="you", status="draft", version="v1", app_type="workflow",
                flow={"nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "totally_bogus_runtime_type", "x": 100, "y": 0, "sub": "", "config": {}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ], "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                ]},
            ))
            s.commit()
            agent = s.get(Agent, agent_id)

            result = run_flow_sync(s, agent, "user:test", "hello")
            assert result["status"] == "failed"
            assert result["fail_class"] == "orchestration"
            error_span = next(sp for sp in result["spans"] if sp.get("node") == "totally_bogus_runtime_type")
            assert error_span["status"] == "error"
            assert "totally_bogus_runtime_type" in error_span["error"]
            assert not any(sp.get("status") == "skipped" for sp in result["spans"])

            s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
            s.delete(agent)
            s.commit()


class TestRealDurableParentDurableChildHitlRejection:
    """Real tests for section 0.95, GAP_REPORT — a real, reported
    architectural gap: a parent flow that already needs the durable
    (Temporal) execution path (it has its own hitl node) calling a
    Sub-Agent whose own target ALSO needs the durable path (its own
    current flow has a hitl node) hits a real, unhandled gap.
    handle_subagent (harness.py) raises a genuine HitlPause when the
    CHILD pauses, but execute_node_activity (temporal_workflow.py) -
    the real code that actually runs a subagent node for a durable
    PARENT - has no special HitlPause handling at all; its own generic
    except Exception catches it and reports a false "orchestration
    failure", even though the child genuinely, correctly paused for a
    real person's input.

    The real, architecturally correct fix (a genuine Temporal Child
    Workflow, started from real workflow code) is a substantial
    rewrite of the durable execution engine that can't be verified
    end-to-end in this sandbox (no reachable Temporal test server).
    Rejected instead at the one, real, authoritative, publish-blocking
    validation layer - the report's own suggested, safer alternative
    to shipping an unverifiable runtime fix, matching this session's
    own established precedent for the identical class of decision
    (§0.87's own nested-container rejection)."""

    def test_a_durable_parent_calling_a_durable_child_is_rejected_at_publish(self):
        """Reproduces the exact scenario from the report directly."""
        child = client.post("/api/agents", json={
            "name": f"Durable Child {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "c1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "c2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                        "delivery_methods": ["web_app"], "form_content": "Approve child?",
                        "actions": [{"id": "approve", "title": "Approve"}],
                    }},
                    {"id": "c3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "child done"}},
                ],
                "edges": [
                    {"id": "ce1", "source": "c1", "target": "c2", "source_handle": None},
                    {"id": "ce2", "source": "c2", "target": "c3", "source_handle": None},
                ],
            },
        })
        child_id = child.json()["id"]
        pub_child = client.post(f"/api/agents/{child_id}/publish")
        assert pub_child.status_code == 200, pub_child.text
        parent = None
        try:
            r = client.post("/api/agents", json={
                "name": f"Durable Parent {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
                "template_flow": {
                    "nodes": [
                        {"id": "p1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                        {"id": "p2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                            "delivery_methods": ["web_app"], "form_content": "Approve parent step?",
                            "actions": [{"id": "approve", "title": "Approve"}],
                        }},
                        {"id": "p3", "type": "subagent", "x": 200, "y": 0, "sub": "", "config": {"target_agent_id": child_id}},
                    ],
                    "edges": [
                        {"id": "pe1", "source": "p1", "target": "p2", "source_handle": None},
                        {"id": "pe2", "source": "p2", "target": "p3", "source_handle": None},
                    ],
                },
            })
            assert r.status_code == 200, r.text
            parent = r.json()

            r2 = client.post(f"/api/agents/{parent['id']}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "durable_parent_durable_child_hitl_unsupported" and i["node_id"] == "p3" for i in issues)
        finally:
            if parent:
                cleanup_agent(parent["id"])
            cleanup_agent(child_id)

    def test_a_sync_parent_calling_a_durable_child_still_publishes_correctly(self):
        """No regression - the real, existing, working case (§0.83,
        GAP_REPORT): a parent with no hitl node of its own calling a
        durable child is unaffected, since handle_subagent's own
        real HitlPause is genuinely caught by the SYNC harness in that
        case, exactly as designed."""
        child = client.post("/api/agents", json={
            "name": f"Durable Child For Sync Parent {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "c1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "c2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                        "delivery_methods": ["web_app"], "form_content": "Approve?",
                        "actions": [{"id": "approve", "title": "Approve"}],
                    }},
                    {"id": "c3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "done"}},
                ],
                "edges": [
                    {"id": "ce1", "source": "c1", "target": "c2", "source_handle": None},
                    {"id": "ce2", "source": "c2", "target": "c3", "source_handle": None},
                ],
            },
        })
        child_id = child.json()["id"]
        assert client.post(f"/api/agents/{child_id}/publish").status_code == 200
        parent = None
        try:
            r = client.post("/api/agents", json={
                "name": f"Sync Parent {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
                "template_flow": {
                    "nodes": [
                        {"id": "p1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                        {"id": "p3", "type": "subagent", "x": 200, "y": 0, "sub": "", "config": {"target_agent_id": child_id}},
                    ],
                    "edges": [{"id": "pe2", "source": "p1", "target": "p3", "source_handle": None}],
                },
            })
            parent = r.json()
            r2 = client.post(f"/api/agents/{parent['id']}/publish")
            assert r2.status_code == 200, r2.text
        finally:
            if parent:
                cleanup_agent(parent["id"])
            cleanup_agent(child_id)

    def test_a_durable_parent_calling_a_sync_only_child_still_publishes_correctly(self):
        """No false positive - a durable parent (its own hitl node)
        calling a Sub-Agent whose target never needs durable routing
        at all (no hitl node anywhere in the target's own flow) is
        genuinely unaffected - there's no real child pause that could
        ever be misreported in the first place."""
        child = client.post("/api/agents", json={
            "name": f"Sync Only Child {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "c1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "c2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": "sync done"}},
                ],
                "edges": [{"id": "ce1", "source": "c1", "target": "c2", "source_handle": None}],
            },
        })
        child_id = child.json()["id"]
        assert client.post(f"/api/agents/{child_id}/publish").status_code == 200
        parent = None
        try:
            r = client.post("/api/agents", json={
                "name": f"Durable Parent Sync Child {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
                "template_flow": {
                    "nodes": [
                        {"id": "p1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                        {"id": "p2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                            "delivery_methods": ["web_app"], "form_content": "Approve?",
                            "actions": [{"id": "approve", "title": "Approve"}],
                        }},
                        {"id": "p3", "type": "subagent", "x": 200, "y": 0, "sub": "", "config": {"target_agent_id": child_id}},
                    ],
                    "edges": [
                        {"id": "pe1", "source": "p1", "target": "p2", "source_handle": None},
                        {"id": "pe2", "source": "p2", "target": "p3", "source_handle": None},
                    ],
                },
            })
            parent = r.json()
            r2 = client.post(f"/api/agents/{parent['id']}/publish")
            assert r2.status_code == 200, r2.text
        finally:
            if parent:
                cleanup_agent(parent["id"])
            cleanup_agent(child_id)

    def test_the_validator_function_directly_uses_the_targets_own_draft_flow_when_unpublished(self):
        """Direct, unit-level proof _real_current_flow correctly falls
        back to a genuinely unpublished target's own live flow (there's
        no AgentVersion snapshot to prefer yet)."""
        from app.flow_validator import validate_flow
        from app.db import engine
        from app.models import Agent

        child = client.post("/api/agents", json={
            "name": f"Unpublished Durable Child {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "c1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "c2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                        "delivery_methods": ["web_app"], "form_content": "Approve?",
                        "actions": [{"id": "approve", "title": "Approve"}],
                    }},
                    {"id": "c3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "done"}},
                ],
                "edges": [
                    {"id": "ce1", "source": "c1", "target": "c2", "source_handle": None},
                    {"id": "ce2", "source": "c2", "target": "c3", "source_handle": None},
                ],
            },
        })
        child_id = child.json()["id"]  # deliberately never published
        try:
            nodes = [
                {"id": "p1", "type": "trigger", "config": {}},
                {"id": "p2", "type": "hitl", "config": {"delivery_methods": ["web_app"], "form_content": "x", "actions": []}},
                {"id": "p3", "type": "subagent", "config": {"target_agent_id": child_id}},
            ]
            edges = [
                {"id": "pe1", "source": "p1", "target": "p2", "source_handle": None},
                {"id": "pe2", "source": "p2", "target": "p3", "source_handle": None},
            ]
            with Session(engine) as s:
                result = validate_flow(nodes, edges, app_type="chatflow", session=s)
            assert any(i.code == "durable_parent_durable_child_hitl_unsupported" for i in result.errors)
        finally:
            cleanup_agent(child_id)


class TestRealGraphCycleDetectionAndRuntimeDeadlockGuard:
    """Real tests for section 0.103, GAP_REPORT.

    The real, reported gap: flow_validator.py only ever checked
    parent_id containment cycles, never a genuine cycle in the real
    source->target execution edges nodes actually run along. Confirmed
    live, before this fix: a real, unbroken edge cycle (Trigger -> A
    <-> B) let BOTH real schedulers (harness.py's run_graph_concurrent,
    temporal_workflow.py's AgentRunWorkflow) reach their own normal,
    successful completion path — `if not wave: break` can't tell
    "every node ran" apart from "no node can ever become ready again".

    The fix, at both levels the report asked for: flow_validator.py
    now runs a real, iterative, three-color DFS cycle check over the
    top-level graph and, separately, each container's own children;
    and both real schedulers now distinguish a genuine deadlock (real
    nodes remain neither done nor dead) from real completion, raising
    a dedicated, structured GraphDeadlockError / graph_deadlock
    fail_class instead of silently succeeding."""

    def test_the_exact_reported_cycle_is_caught_by_the_validator(self):
        from app.flow_validator import validate_flow
        nodes = [
            {"id": "trigger", "type": "trigger", "config": {}},
            {"id": "a", "type": "template", "config": {"template": "a"}},
            {"id": "b", "type": "template", "config": {"template": "b"}},
        ]
        edges = [
            {"id": "e1", "source": "trigger", "target": "a", "source_handle": None},
            {"id": "e2", "source": "a", "target": "b", "source_handle": None},
            {"id": "e3", "source": "b", "target": "a", "source_handle": None},
        ]
        result = validate_flow(nodes, edges, app_type="workflow")
        cycle_errors = [i for i in result.errors if i.code == "graph_cycle"]
        assert len(cycle_errors) == 1
        assert "a -> b -> a" in cycle_errors[0].message

    def test_a_cycle_inside_a_loop_containers_own_body_is_also_caught(self):
        from app.flow_validator import validate_flow
        nodes = [
            {"id": "trigger", "type": "trigger", "config": {}},
            {"id": "loop1", "type": "loop", "config": {"max_loops": 5}},
            {"id": "c1", "type": "template", "config": {"template": "c1"}, "parent_id": "loop1"},
            {"id": "c2", "type": "template", "config": {"template": "c2"}, "parent_id": "loop1"},
        ]
        edges = [
            {"id": "e1", "source": "trigger", "target": "loop1", "source_handle": None},
            {"id": "e2", "source": "c1", "target": "c2", "source_handle": None},
            {"id": "e3", "source": "c2", "target": "c1", "source_handle": None},
        ]
        result = validate_flow(nodes, edges, app_type="workflow")
        cycle_errors = [i for i in result.errors if i.code == "graph_cycle"]
        assert len(cycle_errors) == 1
        assert "loop1" in cycle_errors[0].message

    def test_a_legitimate_diamond_fan_out_fan_in_is_not_a_false_positive(self):
        """A common, entirely valid pattern — one node branching into
        two parallel paths that both converge on a third — must never
        be mistaken for a cycle."""
        from app.flow_validator import validate_flow
        nodes = [
            {"id": "trigger", "type": "trigger", "config": {}},
            {"id": "a", "type": "template", "config": {"template": "a"}},
            {"id": "b", "type": "template", "config": {"template": "b"}},
            {"id": "c", "type": "template", "config": {"template": "c"}},
            {"id": "out", "type": "output", "config": {}},
        ]
        edges = [
            {"id": "e1", "source": "trigger", "target": "a", "source_handle": None},
            {"id": "e2", "source": "a", "target": "b", "source_handle": None},
            {"id": "e3", "source": "a", "target": "c", "source_handle": None},
            {"id": "e4", "source": "b", "target": "out", "source_handle": None},
            {"id": "e5", "source": "c", "target": "out", "source_handle": None},
        ]
        result = validate_flow(nodes, edges, app_type="workflow")
        assert not any(i.code == "graph_cycle" for i in result.errors)

    def test_publishing_a_flow_with_a_real_cycle_is_blocked(self):
        r = client.post("/api/agents", json={
            "name": f"Cycle Publish Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "trigger", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "a", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": "A"}},
                    {"id": "b", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "B"}},
                ],
                "edges": [
                    {"id": "e1", "source": "trigger", "target": "a", "source_handle": None},
                    {"id": "e2", "source": "a", "target": "b", "source_handle": None},
                    {"id": "e3", "source": "b", "target": "a", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/publish")
            assert r2.status_code == 400
            issues = r2.json()["detail"]["issues"]
            assert any(i["code"] == "graph_cycle" for i in issues)
        finally:
            cleanup_agent(agent_id)

    def test_the_runtime_scheduler_raises_a_deadlock_error_not_a_silent_success(self):
        """Direct, unit-level proof of the exact real mechanism —
        run_graph_concurrent (the sync scheduler) genuinely raises
        GraphDeadlockError, with the real, correct blocked node ids,
        rather than returning None (its own real "completed
        normally" signal)."""
        from app.harness import run_graph_concurrent, GraphDeadlockError
        nodes_by_id = {
            "trigger": {"id": "trigger", "type": "trigger", "config": {}},
            "a": {"id": "a", "type": "template", "config": {"template": "A"}},
            "b": {"id": "b", "type": "template", "config": {"template": "B"}},
        }
        edges = [
            {"id": "e1", "source": "trigger", "target": "a", "source_handle": None},
            {"id": "e2", "source": "a", "target": "b", "source_handle": None},
            {"id": "e3", "source": "b", "target": "a", "source_handle": None},
        ]

        def run_one_node(node, ctx):
            ctx["output"] = node["id"]

        ctx = {"vars": {}}
        steps_state = {"count": 0}
        with pytest.raises(GraphDeadlockError) as exc_info:
            run_graph_concurrent("trigger", nodes_by_id, edges, run_one_node, ctx, steps_state)
        assert set(exc_info.value.blocked_nodes) == {"a", "b"}
        assert set(exc_info.value.unresolved_edges) == {"e2", "e3"}

    def test_a_draft_run_with_a_real_cycle_fails_with_graph_deadlock_not_a_silent_success(self):
        """The full, real, end-to-end path — an unpublished draft's
        own /run (which never goes through publish-time validation)
        must not silently succeed with the cycle's own nodes never
        having executed."""
        r = client.post("/api/agents", json={
            "name": f"Cycle Runtime Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "trigger", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "a", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": "A"}},
                    {"id": "b", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "B"}},
                ],
                "edges": [
                    {"id": "e1", "source": "trigger", "target": "a", "source_handle": None},
                    {"id": "e2", "source": "a", "target": "b", "source_handle": None},
                    {"id": "e3", "source": "b", "target": "a", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            run_r = client.post(f"/api/agents/{agent_id}/run", json={"input": "test", "invoking_user": "user:you"})
            assert run_r.status_code == 200, run_r.text
            body = run_r.json()
            assert body["status"] == "failed"
            assert body["status"] != "success"
            assert body["fail_class"] == "graph_deadlock"
            assert set(body["deadlock_blocked_nodes"]) == {"a", "b"}
        finally:
            cleanup_agent(agent_id)


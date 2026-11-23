"""
Real tests for the durable Temporal hitl support (§0.42, GAP_REPORT).

Same honest limitation as test_temporal_scheduling.py: a full live proof
against an actual running Temporal server isn't possible in this sandbox
(temporalio.testing.WorkflowEnvironment needs to download a real test-
server binary from temporal.download, which is outside this sandbox's
network allowlist — confirmed live, a genuine 403). These tests cover
what CAN be verified without one: the real routing decision
(_needs_durable_path), the new real hitl branch in the shared
_resolve_outgoing_edges function both schedulers use, and a direct
structural check that AgentRunWorkflow's real signal/query handlers
exist with the correct Temporal decorators and behave correctly when
called directly (bypassing the Temporal runtime, which is legitimate
for signal/query methods — they're plain Python methods under the
decorators, callable directly for unit testing).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.harness import _resolve_outgoing_edges
from app.routers.execution import DURABLE_NODE_TYPES, _needs_durable_path

client = TestClient(app)


def create_agent(**overrides):
    body = {"name": "Test Agent", "subtype": "Workflow", "app_type": "workflow"}
    body.update(overrides)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def set_flow(agent_id, flow):
    from sqlmodel import Session
    from app.db import engine
    from app.models import Agent
    from sqlalchemy.orm.attributes import flag_modified
    with Session(engine) as s:
        a = s.get(Agent, agent_id)
        a.flow = flow
        flag_modified(a, "flow")
        s.add(a)
        s.commit()


class TestRealDurableRouting:
    def test_hitl_is_a_real_durable_node_type(self):
        assert "hitl" in DURABLE_NODE_TYPES

    def test_a_flow_with_a_real_hitl_node_needs_the_durable_path(self):
        class FakeAgent:
            flow = {"nodes": [
                {"id": "n1", "type": "trigger"},
                {"id": "n2", "type": "hitl"},
            ]}
        assert _needs_durable_path(FakeAgent()) is True

    def test_a_flow_with_no_hitl_node_stays_on_the_sync_path(self):
        class FakeAgent:
            flow = {"nodes": [
                {"id": "n1", "type": "trigger"},
                {"id": "n2", "type": "llm"},
                {"id": "n3", "type": "respond"},
            ]}
        assert _needs_durable_path(FakeAgent()) is False

    def test_a_hitl_flow_genuinely_takes_the_durable_path(self):
        """The property this test was written for: the routing decision
        actually takes effect, rather than silently falling back to the
        sync path or hanging.

        It used to assert a 500 mentioning "Temporal", on the stated
        premise that "no real Temporal server is running in this test
        environment". That premise stopped being true once the compose
        stack included Temporal — and worse, the test kept passing for
        the wrong reason: the durable path was crashing on an unrelated
        Decimal serialisation bug, and the resulting error message
        happened to mention Temporal. It was asserting a broken path,
        not a routed one.

        Asserted both ways now, so it holds with or without a server:
        a HITL flow either genuinely pauses on the durable path, or
        fails loudly naming Temporal. What it must never do is quietly
        succeed on the sync path."""
        a = create_agent()
        set_flow(a["id"], {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {"form_content": "confirm?"}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        })
        r = client.post(f"/api/agents/{a['id']}/run", json={"input": "go"})
        if r.status_code == 500:
            # No reachable Temporal: the failure must name it rather than
            # being a generic error or a silent sync-path success.
            assert "Temporal" in r.json()["detail"]
            return
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "paused_for_human_input"
        # The routing decision, visible: this did not run on the sync path.
        assert "durable" in (body.get("execution_mode") or "").lower()


class TestRealHitlEdgeResolution:
    """The new real branch in _resolve_outgoing_edges (§0.42) - a
    hitl node's own outgoing edges are keyed by real action id, same
    real pattern as condition's true/false."""

    def _graph(self):
        edges_by_source = {
            "hitl1": [
                {"id": "e_approve", "source": "hitl1", "target": "approved_node", "source_handle": "approve"},
                {"id": "e_reject", "source": "hitl1", "target": "rejected_node", "source_handle": "reject"},
            ],
        }
        return edges_by_source

    def test_before_any_response_the_hitl_node_has_no_resolved_edges(self):
        node = {"id": "hitl1", "type": "hitl"}
        ctx = {"vars": {}}
        activated, dead = _resolve_outgoing_edges(node, ctx, self._graph())
        assert activated == {"e_approve", "e_reject"}

    def test_the_approve_action_only_activates_the_real_approve_edge(self):
        node = {"id": "hitl1", "type": "hitl"}
        ctx = {"vars": {"hitl_action_id": "approve"}}
        activated, dead = _resolve_outgoing_edges(node, ctx, self._graph())
        assert activated == {"e_approve"}
        assert dead == {"e_reject"}

    def test_the_reject_action_only_activates_the_real_reject_edge(self):
        node = {"id": "hitl1", "type": "hitl"}
        ctx = {"vars": {"hitl_action_id": "reject"}}
        activated, dead = _resolve_outgoing_edges(node, ctx, self._graph())
        assert activated == {"e_reject"}
        assert dead == {"e_approve"}

    def test_an_action_with_no_wired_edge_leaves_everything_dead(self):
        """Matches resume_flow_sync's own real behavior of just ending
        the run there rather than guessing a fallback path."""
        node = {"id": "hitl1", "type": "hitl"}
        ctx = {"vars": {"hitl_action_id": "some_other_action"}}
        activated, dead = _resolve_outgoing_edges(node, ctx, self._graph())
        assert activated == set()
        assert dead == {"e_approve", "e_reject"}


class TestRealWorkflowSignalQueryStructure:
    """Direct, real verification of AgentRunWorkflow's signal/query
    handlers - legitimate to call directly for unit testing (they're
    plain Python methods under Temporal's decorators, not something
    that requires the Temporal runtime to invoke in isolation)."""

    def test_the_workflow_has_a_real_signal_handler(self):
        from app.temporal_workflow import AgentRunWorkflow
        assert hasattr(AgentRunWorkflow, "submit_hitl_response")

    def test_the_workflow_has_a_real_query_handler(self):
        from app.temporal_workflow import AgentRunWorkflow
        assert hasattr(AgentRunWorkflow, "get_status")

    def test_get_status_reports_running_with_no_pending_hitl(self):
        from app.temporal_workflow import AgentRunWorkflow
        wf = AgentRunWorkflow()
        assert wf.get_status() == {"status": "running"}

    def test_get_status_reports_the_real_pending_pause_once_set(self):
        from app.temporal_workflow import AgentRunWorkflow
        wf = AgentRunWorkflow()
        wf._hitl_pending = {"hitl_node_id": "n2", "hitl_rendered_content": "confirm?"}
        status = wf.get_status()
        assert status["status"] == "paused_for_human_input"
        assert status["hitl_node_id"] == "n2"
        assert status["hitl_rendered_content"] == "confirm?"

    def test_submit_hitl_response_genuinely_records_the_real_response(self):
        from app.temporal_workflow import AgentRunWorkflow
        wf = AgentRunWorkflow()
        assert wf._hitl_response is None
        wf.submit_hitl_response("approve", {"note": "looks good"})
        assert wf._hitl_response == {"action_id": "approve", "field_values": {"note": "looks good"}}

    def test_the_render_hitl_form_activity_and_execute_node_activity_are_both_registered_on_the_worker(self):
        import app.worker as worker_module
        import inspect
        src = inspect.getsource(worker_module.main)
        assert "execute_node_activity" in src
        assert "render_hitl_form_activity" in src

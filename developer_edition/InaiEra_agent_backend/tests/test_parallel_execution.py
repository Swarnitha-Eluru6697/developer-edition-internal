"""
Real tests for parallel/serial graph execution (§0.40, GAP_REPORT),
matching Dify's own documented orchestration model
(https://docs.dify.ai/en/cloud/use-dify/build/orchestrate-node): nodes
with no dependency on each other run concurrently; a downstream node
that depends on more than one upstream branch waits for all of them;
an unchosen condition branch (and everything exclusively downstream of
it) is correctly skipped, not run anyway or left hanging.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

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


class TestRealEdgeFanOut:
    """add_edge's own real fan-out support — a source can now feed more
    than one target, not just replace the prior edge."""

    def test_a_second_edge_from_the_same_port_is_genuinely_added_not_replacing_the_first(self):
        a = create_agent()
        set_flow(a["id"], {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [],
        })
        r1 = client.post(f"/api/agents/{a['id']}/edges", json={"source": "n1", "target": "n2"})
        assert r1.status_code == 200
        r2 = client.post(f"/api/agents/{a['id']}/edges", json={"source": "n1", "target": "n3"})
        assert r2.status_code == 200
        edges = r2.json()["flow"]["edges"]
        assert len(edges) == 2
        targets = {e["target"] for e in edges}
        assert targets == {"n2", "n3"}

    def test_drawing_the_exact_same_edge_twice_does_not_create_a_real_duplicate(self):
        a = create_agent()
        set_flow(a["id"], {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [],
        })
        client.post(f"/api/agents/{a['id']}/edges", json={"source": "n1", "target": "n2"})
        r2 = client.post(f"/api/agents/{a['id']}/edges", json={"source": "n1", "target": "n2"})
        assert len(r2.json()["flow"]["edges"]) == 1


@pytest.fixture()
def parallel_flow_agent():
    a = create_agent()
    set_flow(a["id"], {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "gov", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n3", "type": "code", "x": 0, "y": 0, "sub": "", "config": {
                "code": "import time\ndef main(**kwargs):\n    time.sleep(0.6)\n    return {'result': 'A'}",
                "output_name": "branch_a",
            }},
            {"id": "n4", "type": "code", "x": 0, "y": 0, "sub": "", "config": {
                "code": "import time\ndef main(**kwargs):\n    time.sleep(0.6)\n    return {'result': 'B'}",
                "output_name": "branch_b",
            }},
            {"id": "n5", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "A={{branch_a}} B={{branch_b}}"}},
            {"id": "n6", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            {"id": "e3", "source": "n2", "target": "n4", "source_handle": None},
            {"id": "e4", "source": "n3", "target": "n5", "source_handle": None},
            {"id": "e5", "source": "n4", "target": "n5", "source_handle": None},
            {"id": "e6", "source": "n5", "target": "n6", "source_handle": None},
        ],
    })
    return a["id"]


class TestRealConcurrentExecution:
    def test_two_independent_branches_genuinely_run_concurrently_not_sequentially(self, parallel_flow_agent):
        start = time.time()
        r = client.post(f"/api/agents/{parallel_flow_agent}/run", json={"input": "go"})
        elapsed = time.time() - start
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "success"
        # Two real 0.6s branches: sequential would take >=1.2s; real
        # concurrent execution should land close to 0.6s plus overhead.
        # A generous 1.0s ceiling still clearly distinguishes the two.
        assert elapsed < 1.0, f"took {elapsed:.2f}s — looks sequential, not concurrent"

    def test_the_real_join_node_reads_both_upstream_branches(self, parallel_flow_agent):
        r = client.post(f"/api/agents/{parallel_flow_agent}/run", json={"input": "go"})
        result = r.json()
        assert "A={'result': 'A'}" in result["output"]
        assert "B={'result': 'B'}" in result["output"]

    def test_every_node_gets_its_own_real_span_even_under_concurrent_execution(self, parallel_flow_agent):
        r = client.post(f"/api/agents/{parallel_flow_agent}/run", json={"input": "go"})
        node_ids = {s["node_id"] for s in r.json()["spans"]}
        assert node_ids == {"n1", "n2", "n3", "n4", "n5", "n6"}


class TestRealConditionDeadBranchPropagation:
    def _make_agent(self, expression):
        a = create_agent()
        set_flow(a["id"], {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "gov", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n3", "type": "condition", "x": 0, "y": 0, "sub": "", "config": {"expression": expression}},
                {"id": "n4", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "TRUE_A"}},
                {"id": "n5", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "TRUE_B"}},
                {"id": "n6", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "FALSE"}},
                {"id": "n7", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                {"id": "e3", "source": "n3", "target": "n4", "source_handle": "true"},
                {"id": "e4", "source": "n3", "target": "n5", "source_handle": "true"},
                {"id": "e5", "source": "n3", "target": "n6", "source_handle": "false"},
                {"id": "e6", "source": "n4", "target": "n7", "source_handle": None},
            ],
        })
        return a["id"]

    def test_true_branch_fanout_runs_both_real_nodes_false_branch_never_runs(self):
        agent_id = self._make_agent("1 == 1")
        r = client.post(f"/api/agents/{agent_id}/run", json={"input": "go"})
        node_ids = {s["node_id"] for s in r.json()["spans"]}
        assert "n4" in node_ids
        assert "n5" in node_ids
        assert "n6" not in node_ids  # the real, unchosen branch — genuinely skipped

    def test_false_branch_runs_true_branch_fanout_never_runs(self):
        agent_id = self._make_agent("1 == 2")
        r = client.post(f"/api/agents/{agent_id}/run", json={"input": "go"})
        node_ids = {s["node_id"] for s in r.json()["spans"]}
        assert "n6" in node_ids
        assert "n4" not in node_ids
        assert "n5" not in node_ids


class TestRealSequentialDependencyPreserved:
    """A genuinely dependent chain must still run in the real correct
    order — the scheduler must not accidentally parallelize nodes that
    actually depend on each other."""

    def test_a_strict_chain_produces_the_real_correct_final_value(self):
        a = create_agent()
        set_flow(a["id"], {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "step1:{{trigger_input}}"}},
                {"id": "n3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "step2:{{template_output}}"}},
                {"id": "n4", "type": "output", "x": 0, "y": 0, "sub": "", "config": {"variables": [{"name": "final", "expr": "{{template_output}}"}]}},
                {"id": "n5", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                {"id": "e3", "source": "n3", "target": "n4", "source_handle": None},
                {"id": "e4", "source": "n4", "target": "n5", "source_handle": None},
            ],
        })
        r = client.post(f"/api/agents/{a['id']}/run", json={"input": "X"})
        result = r.json()
        # A real, unambiguous check of real sequential data flow — n3's
        # own real render genuinely depended on n2's real prior output,
        # captured explicitly by name (sidesteps ctx["output"]'s own
        # documented first-wins-across-nodes convention, a separate,
        # pre-existing span-display detail unrelated to this test).
        assert result.get("structured_output", {}).get("final") == "step2:step1:X"

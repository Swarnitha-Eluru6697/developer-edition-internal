"""
Real tests for the Guardrails overview endpoint (section 0.49, GAP_REPORT).
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.harness import SUB_AGENT_HOP_CAP, MAX_GRAPH_STEPS
from app.ucs_client import resolve_entitlement_plan

client = TestClient(app)


def create_agent(**overrides):
    body = {"name": f"Guardrails Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "workflow"}
    body.update(overrides)
    r = client.post("/api/agents", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def set_flow(agent_id, flow):
    from sqlmodel import Session
    from app.db import engine
    from app.models import Agent as AgentModel
    from sqlalchemy.orm.attributes import flag_modified
    with Session(engine) as s:
        db_agent = s.get(AgentModel, agent_id)
        db_agent.flow = flow
        flag_modified(db_agent, "flow")
        s.add(db_agent)
        s.commit()


class TestRealPlatformConstants:
    def test_returns_the_real_platform_wide_safety_constants(self):
        r = client.get("/api/guardrails/overview")
        assert r.status_code == 200
        c = r.json()["platform_constants"]
        assert c["max_graph_steps"] == 50
        assert c["sub_agent_hop_cap"] == 5
        assert c["step_limit_default"] == 12
        assert c["max_loops_default"] == 10
        assert c["code_sandbox_timeout_seconds"] == 5
        assert c["code_sandbox_memory_mb"] == 128
        assert c["chat_history_max_turns"] == 20

    def test_sub_agent_hop_cap_is_the_real_single_shared_constant_not_a_second_literal(self):
        from sqlmodel import Session
        from app.db import engine
        with Session(engine) as s:
            plan = resolve_entitlement_plan(s, "a1", "Test Agent", "user:test", [])
        assert plan["chain_depth_cap"] == SUB_AGENT_HOP_CAP == 5


class TestRealPerAgentGuardrailStatus:
    def test_a_fresh_agent_shows_real_off_defaults(self):
        agent = create_agent()
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        row = rows[agent["id"]]
        assert row["moderation"]["enabled"] is False
        assert row["require_authenticated_user"] is False
        assert row["ucs"]["touches_checked_objects"] is False

    def test_enabling_moderation_is_genuinely_reflected(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/features", json={
            "features": {"moderation": {"enabled": True, "provider": "keywords", "keywords": ["spam"]}}
        })
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        row = rows[agent["id"]]
        assert row["moderation"]["enabled"] is True
        assert row["moderation"]["provider"] == "keywords"

    def test_enabling_require_authenticated_user_is_genuinely_reflected(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/features", json={
            "features": {"require_authenticated_user": True}
        })
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        assert rows[agent["id"]]["require_authenticated_user"] is True

    def test_a_flow_with_a_real_tool_node_shows_as_touching_ucs_checked_objects(self):
        agent = create_agent()
        flow = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "tool", "x": 0, "y": 0, "sub": "", "config": {"catalog_object": "finance.revenue_db"}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        set_flow(agent["id"], flow)
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        row = rows[agent["id"]]
        assert row["ucs"]["touches_checked_objects"] is True
        assert row["ucs"]["checked_object_count"] == 1

    def test_a_custom_step_limit_on_an_llm_node_is_genuinely_detected(self):
        agent = create_agent()
        flow = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "llm", "x": 0, "y": 0, "sub": "", "config": {"step_limit": 5}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        set_flow(agent["id"], flow)
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        row = rows[agent["id"]]
        assert row["safety_caps"]["llm_agent_node_count"] == 1
        assert row["safety_caps"]["llm_agent_nodes_with_custom_step_limit"] == 1

    def test_a_loop_node_without_a_custom_max_loops_is_not_counted_as_customized(self):
        agent = create_agent()
        flow = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "loop", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        set_flow(agent["id"], flow)
        r = client.get("/api/guardrails/overview")
        rows = {a["id"]: a for a in r.json()["agents"]}
        row = rows[agent["id"]]
        assert row["safety_caps"]["loop_node_count"] == 1
        assert row["safety_caps"]["loop_nodes_with_custom_max_loops"] == 0

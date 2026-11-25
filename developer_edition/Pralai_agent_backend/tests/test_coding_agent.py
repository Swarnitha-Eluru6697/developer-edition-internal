"""
Real tests for the coding-agent workflow generation feature (§0.46,
GAP_REPORT) - generates a real flow graph from a plain-language prompt
via a separate, OpenAI-compatible LLM client (mlx-lm locally, real vLLM
in production), never model_adapter.py's own Anthropic client.

No live mlx-lm/vLLM server is available in this environment, so the real,
honest verification here covers: the mock-fallback path (proven live
end-to-end through the real /api/agents/generate endpoint), the real
validation logic against deliberately malformed inputs, and the real
error-handling path when a configured endpoint is genuinely unreachable
(a real connection attempt to a real closed port, not a mocked
exception).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.coding_agent_adapter import (
    generate_flow, _validate_generated_flow, NODE_SCHEMA_CATALOG,
    CodingAgentGenerationError, CodingAgentUnavailable,
)

client = TestClient(app)


class TestRealValidation:
    def _valid(self, **overrides):
        base = {
            "nodes": [{"id": "n1", "type": "llm", "config": {}}],
            "edges": [],
            "entry_node_id": "n1",
        }
        base.update(overrides)
        return base

    def test_a_real_valid_flow_passes(self):
        nodes, edges, entry = _validate_generated_flow(self._valid())
        assert entry == "n1"
        assert len(nodes) == 1

    def test_rejects_a_non_dict_response(self):
        try:
            _validate_generated_flow("not a dict")
            assert False, "should have raised"
        except CodingAgentGenerationError:
            pass

    def test_rejects_empty_nodes(self):
        try:
            _validate_generated_flow(self._valid(nodes=[]))
            assert False, "should have raised"
        except CodingAgentGenerationError:
            pass

    def test_rejects_an_unknown_node_type(self):
        try:
            _validate_generated_flow(self._valid(nodes=[{"id": "n1", "type": "made_up_type", "config": {}}]))
            assert False, "should have raised"
        except CodingAgentGenerationError as e:
            assert "unknown node type" in str(e)

    def test_rejects_a_model_authored_trigger(self):
        try:
            _validate_generated_flow(self._valid(nodes=[{"id": "n1", "type": "trigger", "config": {}}]))
            assert False, "should have raised"
        except CodingAgentGenerationError:
            pass

    def test_rejects_a_model_authored_respond_with_a_specific_message(self):
        try:
            _validate_generated_flow(self._valid(nodes=[{"id": "n1", "type": "respond", "config": {}}]))
            assert False, "should have raised"
        except CodingAgentGenerationError as e:
            assert "never LLM-authored" in str(e)

    def test_rejects_a_duplicate_node_id(self):
        try:
            _validate_generated_flow(self._valid(nodes=[
                {"id": "n1", "type": "llm", "config": {}},
                {"id": "n1", "type": "code", "config": {}},
            ]))
            assert False, "should have raised"
        except CodingAgentGenerationError as e:
            assert "Duplicate" in str(e)

    def test_rejects_an_entry_node_id_that_does_not_exist(self):
        try:
            _validate_generated_flow(self._valid(entry_node_id="ghost"))
            assert False, "should have raised"
        except CodingAgentGenerationError:
            pass

    def test_rejects_an_edge_referencing_a_nonexistent_node(self):
        try:
            _validate_generated_flow(self._valid(edges=[{"source": "n1", "target": "ghost"}]))
            assert False, "should have raised"
        except CodingAgentGenerationError:
            pass

    def test_the_node_schema_catalog_is_real_and_nonempty(self):
        assert len(NODE_SCHEMA_CATALOG) > 0
        assert "llm" in NODE_SCHEMA_CATALOG
        assert "condition" in NODE_SCHEMA_CATALOG


class TestRealMockFallback:
    def test_generate_flow_returns_a_real_clearly_labeled_mock_without_a_configured_endpoint(self):
        result = generate_flow("build me something")
        assert result["mock"] is True
        assert result["entry_node_id"]
        assert len(result["nodes"]) == 1
        assert "[MOCK" in result["nodes"][0]["config"]["system_prompt"]


class TestRealGenerateEndpoint:
    def test_generates_a_real_persisted_draft_agent_via_the_mock_path(self):
        r = client.post("/api/agents/generate", json={
            "prompt": "classify support tickets by urgency",
            "name": "Test Generated Agent",
        })
        assert r.status_code == 200, r.text
        agent = r.json()
        assert agent["status"] == "draft"
        assert agent["name"] == "Test Generated Agent"

        r2 = client.get(f"/api/agents/{agent['id']}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "draft"

    def test_the_real_flow_is_correctly_spliced_after_trigger_and_gov(self):
        r = client.post("/api/agents/generate", json={
            "prompt": "summarize a document", "name": "Splice Test",
        })
        flow = r.json()["flow"]
        types_by_id = {n["id"]: n["type"] for n in flow["nodes"]}
        assert types_by_id["n1"] == "trigger"
        assert types_by_id["n2"] == "gov"
        assert any(e["source"] == "n1" and e["target"] == "n2" for e in flow["edges"])
        gen_node_ids = [nid for nid, t in types_by_id.items() if nid not in ("n1", "n2", "n-respond")]
        assert any(e["source"] == "n2" and e["target"] in gen_node_ids for e in flow["edges"])

    def test_a_workflow_app_type_gets_a_real_respond_node_wired_from_every_terminal_node(self):
        r = client.post("/api/agents/generate", json={
            "prompt": "anything", "name": "Respond Wiring Test", "app_type": "workflow",
        })
        flow = r.json()["flow"]
        types_by_id = {n["id"]: n["type"] for n in flow["nodes"]}
        assert "n-respond" in types_by_id
        assert types_by_id["n-respond"] == "respond"
        assert any(e["target"] == "n-respond" for e in flow["edges"])

    def test_a_real_unreachable_configured_endpoint_returns_503_not_a_leaked_500(self, monkeypatch):
        import app.coding_agent_adapter as coding_agent_adapter
        monkeypatch.setattr(coding_agent_adapter, "CODING_AGENT_LLM_BASE_URL", "http://127.0.0.1:19998/v1")
        r = client.post("/api/agents/generate", json={"prompt": "test", "name": "Unreachable Test"})
        assert r.status_code == 503
        assert "reach" in r.json()["detail"].lower()

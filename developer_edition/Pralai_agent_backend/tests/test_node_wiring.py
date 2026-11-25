"""
Real tests for section 0.115, GAP_REPORT — the LLM node's own config
panel presenting several controls as fully operational even though
handle_llm's own docstring already, honestly states it never reads
them at all.

The fix: node_wiring.py is a new, real, machine-readable "runtime
schema" — the single source of truth for which config keys a node's
real handler doesn't yet consume — exposed via a real API endpoint the
Builder's own config panel now queries to genuinely DISABLE (not
merely warn about) each such control, implementing the report's own
recommended `Node JSON schema -> runtime schema -> frontend generated
from the same schema` architecture.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from app.node_wiring import UNWIRED_CONTROLS, unwired_controls_for

client = TestClient(app)


class TestRealUnwiredControlsRegistry:
    def test_every_control_the_report_names_is_listed_for_the_llm_node(self):
        """The report's own explicit examples — every one of them must
        be a real, listed entry, not merely implied.

        agentic_strategy and allowed_tools were in this list and are
        deliberately no longer: handle_llm now runs the same
        run_react_loop / run_function_calling_loop the Agent node runs,
        and allowed_tools is the tool list that loop offers. This
        module's own top comment requires a control to be removed in the
        same change that wires it — leaving it would make the Builder
        disable a control the runtime genuinely reads, which is the same
        bug inverted. See test_llm_agentic_strategy.py for the proof
        that they are read."""
        llm_unwired = UNWIRED_CONTROLS["llm"]
        for control in ("structured_output", "guardrail_profile",
                         "reasoning_tag_separation", "vision", "vision_detail"):
            assert control in llm_unwired, f"{control} is one of the report's own named examples but isn't listed"
        for wired in ("agentic_strategy", "allowed_tools"):
            assert wired not in llm_unwired, (
                f"{wired} is wired now — leaving it here makes the Builder disable a working control"
            )

    def test_unwired_controls_for_returns_an_empty_list_for_an_unknown_node_type(self):
        assert unwired_controls_for("condition") == []

    def test_unwired_controls_for_llm_matches_the_registry_directly(self):
        assert unwired_controls_for("llm") == UNWIRED_CONTROLS["llm"]

    def test_every_listed_control_has_a_real_matching_disclosure_in_handle_llms_own_docstring(self):
        """Direct proof this registry is a real, structured transcription
        of handle_llm's own prose, not a second, independent claim that
        could silently drift from it."""
        import inspect
        from app import harness
        docstring = inspect.getsource(harness.handle_llm)
        for control in UNWIRED_CONTROLS["llm"]:
            assert control in docstring, f"{control} is listed as unwired but handle_llm's own docstring never mentions it"


class TestRealUnwiredControlsEndpoint:
    def test_the_real_endpoint_returns_the_same_registry(self):
        r = client.get("/api/agents/node-schema/unwired-controls")
        assert r.status_code == 200
        assert r.json() == UNWIRED_CONTROLS

    def test_the_real_endpoint_is_not_shadowed_by_the_agent_id_route(self):
        """A real, direct regression guard — this static route must be
        registered before the dynamic /{agent_id} route, or FastAPI
        would try to resolve "node-schema" as a real agent id instead."""
        r = client.get("/api/agents/node-schema/unwired-controls")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)
        assert "llm" in r.json()

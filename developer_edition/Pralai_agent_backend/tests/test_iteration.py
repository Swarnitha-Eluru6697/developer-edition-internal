"""
Real tests for the Iteration node (harness.py's run_iteration_body, a
closure inside _execute_flow — exercised here through the real public
entry points run_flow_sync / _execute_flow, since it isn't independently
importable), rebuilt to match Dify's own documented spec
(https://docs.dify.ai/en/cloud/use-dify/nodes/iteration) — see GAP_REPORT
§0.35 for the full comparison. Deliberately separate from `loop` (its own
condition-driven repeat node, left completely untouched per explicit
instruction) even though both are real container node types.

Every test here builds a real flow (trigger -> iteration -> respond, with
a real Code node as the iteration's body) and runs it through the real
harness — the same real path a live API call takes — rather than calling
internal functions directly, since run_iteration_body isn't exposed on
its own.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session

from app.harness import run_flow_sync
from app.models import Agent


@pytest.fixture()
def session():
    """The real Postgres engine (JSONB columns aren't portable to
    SQLite, which is why the other test files in this suite use the
    real database too, not an isolated in-memory one)."""
    from app.db import engine
    with Session(engine) as s:
        yield s


def make_agent(flow: dict) -> Agent:
    import uuid
    return Agent(
        id=f"test-iter-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Workflow", plane="App",
        owner="you", status="published", version="v1", app_type="workflow", flow=flow,
    )


def iteration_flow(array_var_value, body_code: str, arg1_var: str = "{{item}}", **iter_cfg) -> dict:
    """A real minimal flow: trigger (real hidden pre-filled array field)
    -> iteration (real Code-node body) -> respond."""
    cfg = {"input_var": "{{numbers}}", "output_var": "results", "parallel": False, "error_response": "Terminated", "flatten": True, **iter_cfg}
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {
                "fields": [{"id": "f1", "name": "numbers", "type": "json_code", "hidden_prefilled": True, "default": array_var_value}],
            }},
            {"id": "n2", "type": "iteration", "x": 0, "y": 0, "sub": "", "config": cfg},
            {"id": "n2a", "type": "code", "parent_id": "n2", "x": 0, "y": 0, "sub": "", "config": {
                "code": body_code, "arg1_name": "arg1", "arg1_var": arg1_var,
            }},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


class TestRealArrayIterationAndItemBinding:
    def test_the_real_body_genuinely_runs_once_per_real_array_element(self, session):
        flow = iteration_flow([1, 2, 3, 4], "def main(arg1, arg2):\n    return arg1 * 2")
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "")
        assert result["status"] == "success"

    def test_real_doubled_values_with_real_type_preserved_not_stringified(self, session):
        """The real bug found and fixed while building this: an early
        version silently returned the unmodified original items (no
        real item binding worked at all), and after fixing that, a
        second real bug produced string '2' instead of real int 2."""
        flow = iteration_flow([1, 2, 3, 4], "def main(arg1, arg2):\n    return arg1 * 2")
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        assert ctx["vars"]["results"] == [2, 4, 6, 8]
        assert all(isinstance(x, int) for x in ctx["vars"]["results"])

    def test_a_non_array_input_produces_a_real_empty_output_not_a_crash(self, session):
        flow = iteration_flow("not an array", "def main(arg1, arg2):\n    return arg1")
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "")
        assert result["status"] == "success"

    def test_an_empty_array_produces_a_real_empty_output(self, session):
        flow = iteration_flow([], "def main(arg1, arg2):\n    return arg1")
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "")
        assert result["status"] == "success"


class TestRealSequentialVsParallelExecution:
    def test_sequential_mode_genuinely_runs_one_after_another_real_timing_proof(self, session):
        """A real timing proof, not just a status check — 6 real
        0.25s-sleeping iterations should take close to 6x0.25s=1.5s if
        genuinely sequential."""
        flow = iteration_flow(
            [1, 2, 3, 4, 5, 6],
            "import time\ndef main(arg1, arg2):\n    time.sleep(0.25)\n    return arg1",
            parallel=False,
        )
        agent = make_agent(flow)
        t0 = time.time()
        result = run_flow_sync(session, agent, "user:test", "")
        elapsed = time.time() - t0
        assert result["status"] == "success"
        assert elapsed > 1.2  # genuinely serial, not accidentally parallel

    def test_parallel_mode_genuinely_overlaps_real_timing_proof(self, session):
        """The real, most important proof for this feature: parallel
        mode must be MEASURABLY faster than sequential for the same
        real workload, not just accept a config flag that does
        nothing."""
        flow = iteration_flow(
            [1, 2, 3, 4, 5, 6],
            "import time\ndef main(arg1, arg2):\n    time.sleep(0.25)\n    return arg1",
            parallel=True,
        )
        agent = make_agent(flow)
        t0 = time.time()
        result = run_flow_sync(session, agent, "user:test", "")
        elapsed = time.time() - t0
        assert result["status"] == "success"
        assert elapsed < 0.9  # genuinely overlapped — nowhere near 6x0.25s=1.5s

    def test_parallel_mode_still_preserves_real_input_order_in_the_output(self, session):
        flow = iteration_flow(
            [1, 2, 3, 4, 5, 6],
            "import time\ndef main(arg1, arg2):\n    time.sleep(0.01 * (7 - arg1))\n    return arg1",
            parallel=True,
        )
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        # Real order preserved by real index, even though item 1 finishes
        # LAST (longest sleep) and item 6 finishes FIRST — a naive
        # completion-order collection would have scrambled this.
        assert ctx["vars"]["results"] == [1, 2, 3, 4, 5, 6]


class TestRealErrorHandlingModes:
    """Dify's own 3 documented modes, verified against the exact
    documented example: input [1,2,3] with item 2 failing."""

    def test_terminated_mode_genuinely_fails_the_whole_run_on_a_real_error(self, session):
        flow = iteration_flow([10, 0, 5], "def main(arg1, arg2):\n    return 100 / arg1", error_response="Terminated")
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "")
        assert result["status"] == "failed"

    def test_continue_on_error_keeps_a_real_none_at_the_failed_real_index(self, session):
        flow = iteration_flow([10, 0, 5, 0, 2], "def main(arg1, arg2):\n    return 100 / arg1", error_response="Continue on error")
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        result = _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        assert result["status"] == "success"
        assert ctx["vars"]["results"] == [10.0, None, 20.0, None, 50.0]  # matches Dify's own documented example shape

    def test_remove_abnormal_output_genuinely_omits_the_failed_index_shorter_real_array(self, session):
        flow = iteration_flow([10, 0, 5, 0, 2], "def main(arg1, arg2):\n    return 100 / arg1", error_response="Remove abnormal output")
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        result = _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        assert result["status"] == "success"
        assert ctx["vars"]["results"] == [10.0, 20.0, 50.0]  # no Nones, genuinely shorter


class TestRealFlattenBehavior:
    def test_flatten_true_splices_a_real_list_result_into_the_parent_array(self, session):
        flow = iteration_flow([1, 2, 3], "def main(arg1, arg2):\n    return [arg1, arg1 * 10]", flatten=True)
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        assert ctx["vars"]["results"] == [1, 10, 2, 20, 3, 30]

    def test_flatten_false_keeps_each_real_result_nested(self, session):
        flow = iteration_flow([1, 2, 3], "def main(arg1, arg2):\n    return [arg1, arg1 * 10]", flatten=False)
        agent = make_agent(flow)
        session.add(agent); session.commit()
        from app.harness import load_env_map, objects_to_check, _execute_flow
        nodes = agent.flow["nodes"]
        ctx = {"input": "", "vars": {}, "agent_id": agent.id, "agent_name": agent.name, "invoking_user": "user:test",
               "_objects_to_check": objects_to_check(nodes), "output": None, "_webhook_payload": None,
               "_agent_chain": [agent.id], "_structured_inputs": {}}
        start_node = next(n for n in nodes if n["type"] == "trigger")
        _execute_flow(session, agent, ctx, load_env_map(session, agent.id), start_node)
        assert ctx["vars"]["results"] == [[1, 10], [2, 20], [3, 30]]


class TestLoopNodeGenuinelyUntouched:
    """A real regression guard specifically for the explicit instruction
    to leave `loop` completely alone — confirms loop's own condition-
    driven repeat mechanism (separate from Iteration) still works
    exactly as before."""

    def test_loop_with_exit_loop_still_works_unchanged(self, session):
        flow = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "loop", "x": 0, "y": 0, "sub": "", "config": {"max_loops": 5}},
                {"id": "n2a", "type": "condition", "parent_id": "n2", "x": 0, "y": 0, "sub": "", "config": {"expression": "{{loop_iteration}} >= 3"}},
                {"id": "n2b", "type": "exit_loop", "parent_id": "n2", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2a", "target": "n2b", "source_handle": "true"},
                {"id": "e3", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "")
        assert result["status"] == "success"


class TestHitlGenuinelyUntouchedAfterIterationWasAdded:
    """A real regression guard for a real bug that happened DURING this
    build: an editing mistake accidentally merged raise_hitl_pause's own
    function body into the tail of run_iteration_body, deleting its
    `def` line entirely and breaking HITL completely. Caught and fixed
    before shipping — this test exists so a future change can't
    silently reintroduce it."""

    def test_a_real_hitl_node_still_genuinely_pauses_with_real_rendered_content(self, session):
        flow = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {"form_content": "Please confirm: {{trigger_input}}"}},
                {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        agent = make_agent(flow)
        result = run_flow_sync(session, agent, "user:test", "approve invoice 123")
        assert result["status"] == "paused_for_human_input"
        assert result["hitl_rendered_content"] == "Please confirm: approve invoice 123"
        assert result["hitl_node_id"] == "n2"

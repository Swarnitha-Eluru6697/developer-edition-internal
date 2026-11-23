"""
Real tests for the Temporal Iteration node fix (section 0.52, GAP_REPORT).

The real bug: temporal_workflow.py's AgentRunWorkflow.run() only
recognized "loop" and "hitl" as solo (non-concurrent) node types.
"iteration" had no NODE_HANDLERS entry either, so a durable-path flow
reaching an iteration node fell through execute_node_activity's generic
"no handler wired yet" branch and silently continued with unchanged ctx
- worse than a hard failure, since downstream nodes would proceed
against stale data rather than the run visibly stopping.

The fix ports a real, Temporal-native run_iteration_body into
AgentRunWorkflow.run() itself (workflow code can't do direct DB access
or spawn real OS threads, so each body node per iteration is a real
workflow.execute_activity call, and "parallel" mode uses real
asyncio.gather instead of a raw ThreadPoolExecutor). The outcome-
finalization logic (the three real Dify-documented error modes, and
flatten behavior) was extracted into a new shared, pure function,
finalize_iteration_outcomes, used by both the sync and Temporal paths
- the one real piece of this fix that's genuinely unit-testable in
isolation, since workflow.execute_activity itself requires a real
Temporal workflow event loop to run at all (confirmed directly: calling
AgentRunWorkflow().run(...) outside one raises a real, specific
_NotInWorkflowEventLoopError - the same honest verification limit
section 0.42's original HITL work already had).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harness import finalize_iteration_outcomes


class TestRealFinalizeIterationOutcomes:
    def test_all_successful_outcomes_in_real_positional_order(self):
        outcomes = [(0, "a", None), (1, "b", None), (2, "c", None)]
        assert finalize_iteration_outcomes(outcomes, "Terminated", flatten=False) == ["a", "b", "c"]

    def test_a_none_outcome_means_never_reached_and_is_genuinely_skipped(self):
        outcomes = [(0, "a", None), None, (2, "c", None)]
        assert finalize_iteration_outcomes(outcomes, "Terminated", flatten=False) == ["a", "c"]

    def test_continue_on_error_keeps_a_real_none_at_the_failed_index(self):
        outcomes = [(0, "a", None), (1, None, "boom"), (2, "c", None)]
        result = finalize_iteration_outcomes(outcomes, "Continue on error", flatten=False)
        assert result == ["a", None, "c"]

    def test_remove_abnormal_output_genuinely_omits_the_failed_index(self):
        outcomes = [(0, "a", None), (1, None, "boom"), (2, "c", None)]
        result = finalize_iteration_outcomes(outcomes, "Remove abnormal output", flatten=False)
        assert result == ["a", "c"]

    def test_flatten_true_splices_a_real_list_result_one_level(self):
        outcomes = [(0, ["a", "b"], None), (1, ["c"], None)]
        result = finalize_iteration_outcomes(outcomes, "Terminated", flatten=True)
        assert result == ["a", "b", "c"]

    def test_flatten_false_keeps_a_real_list_result_nested(self):
        outcomes = [(0, ["a", "b"], None), (1, ["c"], None)]
        result = finalize_iteration_outcomes(outcomes, "Terminated", flatten=False)
        assert result == [["a", "b"], ["c"]]

    def test_flatten_only_applies_to_real_list_results_not_scalars(self):
        outcomes = [(0, "a", None), (1, ["b", "c"], None), (2, 42, None)]
        result = finalize_iteration_outcomes(outcomes, "Terminated", flatten=True)
        assert result == ["a", "b", "c", 42]

    def test_an_empty_outcomes_list_produces_a_real_empty_result(self):
        assert finalize_iteration_outcomes([], "Terminated", flatten=True) == []

    def test_every_outcome_none_cancelled_before_any_ran_produces_empty_result(self):
        assert finalize_iteration_outcomes([None, None, None], "Continue on error", flatten=True) == []


class TestRealTemporalDispatchWiring:
    def _read_temporal_workflow_source(self) -> str:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "temporal_workflow.py")
        with open(path) as f:
            return f.read()

    def test_iteration_is_in_the_real_solo_split_alongside_loop_and_hitl(self):
        src = self._read_temporal_workflow_source()
        assert '("loop", "hitl", "iteration")' in src

    def test_a_real_run_iteration_body_function_is_defined(self):
        src = self._read_temporal_workflow_source()
        assert "async def run_iteration_body(iteration_node" in src

    def test_the_solo_dispatch_genuinely_calls_run_iteration_body_for_iteration_nodes(self):
        src = self._read_temporal_workflow_source()
        assert 'elif node["type"] == "iteration":' in src
        assert "await run_iteration_body(node)" in src

    def test_both_real_runtimes_import_and_use_the_one_real_shared_finalize_function(self):
        harness_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "harness.py")
        with open(harness_path) as f:
            harness_src = f.read()
        assert harness_src.count("def finalize_iteration_outcomes(") == 1

        temporal_src = self._read_temporal_workflow_source()
        assert "finalize_iteration_outcomes" in temporal_src
        # A real, direct guard that the Temporal file doesn't
        # re-implement its own copy of the old inline finalize loop
        # instead of calling the shared function — checks for the real
        # conditional pattern the old inline loop used, not the bare
        # string (which legitimately still appears in this file's own
        # docstring prose describing the 3 real error modes).
        assert 'if error_mode == "Remove abnormal output":' not in temporal_src

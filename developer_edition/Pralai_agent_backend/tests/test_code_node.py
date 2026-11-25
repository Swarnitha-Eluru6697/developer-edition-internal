"""
Real tests for the Code Node (harness.py's handle_code + its real
interaction with compute_next_node_id's routing). The first backend
test file in this project — added specifically because a person asked
"is the Code node fully tested and working," and the honest answer
before this file existed was no: zero backend tests existed anywhere in
this codebase, verification had only ever happened live during builds
(curl calls whose evidence doesn't outlive the session). These tests are
real and re-runnable: `pytest tests/test_code_node.py` from backend/.

No mocking of the sandbox itself for the "happy path" tests below — they
genuinely spawn the real subprocess (app/sandbox.py's real isolation
mechanism), the same real code path a live run takes. A couple of tests
mock `run_sandboxed_code` directly where the point is to control exit
codes/call counts precisely (retry-on-failure) rather than depend on
crafting Python that reliably fails in a specific way.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import handle_code, compute_next_node_id, CodeExecutionError


def make_ctx(user_input: str = "") -> dict:
    return {"input": user_input, "vars": {}, "agent_id": "test", "agent_name": "test", "invoking_user": "user:test", "output": None}


class TestRealInputInjection:
    def test_main_is_genuinely_invoked_with_real_injected_values(self):
        ctx = make_ctx()
        cfg = {
            "arg1_name": "name", "arg1_var": "World",
            "arg2_name": "suffix", "arg2_var": "!!",
            "code": "def main(name: str, suffix: str):\n    return {'greeting': 'Hello, ' + name + suffix}",
        }
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"] == {"greeting": "Hello, World!!"}

    def test_input_var_resolves_a_real_var_style_reference_against_ctx(self):
        ctx = make_ctx()
        ctx["vars"]["topic"] = "AI safety"
        cfg = {
            "arg1_name": "t", "arg1_var": "{{topic}}",
            "code": "def main(t, arg2):\n    return t.upper()",
        }
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"] == "AI SAFETY"

    def test_input_var_resolves_the_real_input_special_case(self):
        ctx = make_ctx(user_input="hello")
        cfg = {"arg1_name": "x", "arg1_var": "{{input}}", "code": "def main(x, arg2):\n    return x + ' world'"}
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"] == "hello world"

    def test_an_invalid_python_identifier_falls_back_to_a_safe_default_name_rather_than_producing_broken_code(self):
        ctx = make_ctx()
        cfg = {"arg1_name": "not a valid name!", "arg1_var": "x", "code": "def main(arg1, arg2):\n    return arg1"}
        result = handle_code(cfg, ctx)  # must not raise a SyntaxError from bad injected code
        assert result["vars"]["result"] == "x"

    def test_no_main_defined_falls_back_to_the_scripts_own_real_stdout_not_a_fabricated_value(self):
        ctx = make_ctx()
        cfg = {"code": "print('plain script output')"}
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"].strip() == "plain script output"


class TestRealOutputNaming:
    def test_result_lands_under_the_real_configured_output_name(self):
        ctx = make_ctx()
        cfg = {"code": "def main(arg1, arg2):\n    return 42", "output_name": "my_custom_var"}
        handle_code(cfg, ctx)
        assert ctx["vars"]["my_custom_var"] == 42
        assert "code_result" in ctx["vars"]  # the fixed, second real copy respond() reads

    def test_defaults_to_result_when_no_output_name_configured(self):
        ctx = make_ctx()
        cfg = {"code": "def main(arg1, arg2):\n    return 'x'"}
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"] == "x"

    def test_the_internal_sentinel_never_leaks_into_the_parsed_result(self):
        ctx = make_ctx()
        cfg = {"code": "def main(arg1, arg2):\n    return 'clean value'"}
        handle_code(cfg, ctx)
        assert "__INAIERA_RESULT__" not in str(ctx["vars"]["result"])
        assert "__INAIERA_RESULT__" not in str(ctx["vars"]["code_result"])


class TestRealErrorHandling:
    def test_a_genuine_crash_raises_a_real_codeexecutionerror_by_default(self):
        ctx = make_ctx()
        cfg = {"code": "raise ValueError('boom')"}
        with pytest.raises(CodeExecutionError) as exc_info:
            handle_code(cfg, ctx)
        assert "ValueError" in str(exc_info.value)
        assert "boom" in str(exc_info.value)

    def test_the_raw_sandbox_result_is_still_recorded_even_on_a_real_failure(self):
        ctx = make_ctx()
        cfg = {"code": "raise ValueError('boom')"}
        with pytest.raises(CodeExecutionError):
            handle_code(cfg, ctx)
        assert ctx["vars"]["code_output"]["exit_code"] != 0
        assert "boom" in ctx["vars"]["code_output"]["stderr"]

    def test_default_value_mode_genuinely_catches_the_failure_and_continues(self):
        ctx = make_ctx()
        cfg = {"code": "raise ValueError('boom')", "error_handling": "Default value", "default_value": "fallback"}
        result = handle_code(cfg, ctx)  # must not raise
        assert result["vars"]["result"] == "fallback"
        assert result["vars"]["code_result"] == "fallback"

    def test_default_value_mode_with_no_configured_default_falls_back_to_empty_string_not_none(self):
        ctx = make_ctx()
        cfg = {"code": "raise ValueError('boom')", "error_handling": "Default value"}
        result = handle_code(cfg, ctx)
        assert result["vars"]["result"] == ""

    def test_fail_branch_mode_genuinely_sets_the_real_routing_flag_instead_of_raising(self):
        ctx = make_ctx()
        cfg = {"code": "raise ValueError('boom')", "error_handling": "Fail branch"}
        result = handle_code(cfg, ctx)  # must not raise
        assert result["vars"]["_code_failed"] is True

    def test_a_real_success_clears_the_failed_flag_so_a_later_reused_ctx_cant_carry_a_stale_failure(self):
        ctx = make_ctx()
        ctx["vars"]["_code_failed"] = True  # simulate a stale flag from an earlier node
        cfg = {"code": "def main(arg1, arg2):\n    return 'ok'"}
        result = handle_code(cfg, ctx)
        assert result["vars"]["_code_failed"] is False


class TestRealFailBranchRouting:
    """compute_next_node_id's real routing for a code node in Fail
    branch mode — the same real multi-port mechanism `condition` nodes
    use for true/false, applied to success/error."""

    def test_routes_to_the_real_error_edge_when_the_code_genuinely_failed(self):
        node = {"id": "n2", "type": "code"}
        ctx = {"vars": {"_code_failed": True}}
        edges_by_source = {"n2": [
            {"source": "n2", "target": "success_node", "source_handle": "success"},
            {"source": "n2", "target": "error_node", "source_handle": "error"},
        ]}
        assert compute_next_node_id(node, ctx, edges_by_source) == "error_node"

    def test_routes_to_the_real_success_edge_when_the_code_genuinely_succeeded(self):
        node = {"id": "n2", "type": "code"}
        ctx = {"vars": {"_code_failed": False}}
        edges_by_source = {"n2": [
            {"source": "n2", "target": "success_node", "source_handle": "success"},
            {"source": "n2", "target": "error_node", "source_handle": "error"},
        ]}
        assert compute_next_node_id(node, ctx, edges_by_source) == "success_node"

    def test_a_legacy_single_none_handle_edge_still_works_as_the_success_path(self):
        """Backward compatibility: a code node saved before Fail branch
        existed has one edge with source_handle=None — must still be
        treated as "success", not silently broken."""
        node = {"id": "n2", "type": "code"}
        ctx = {"vars": {"_code_failed": False}}
        edges_by_source = {"n2": [{"source": "n2", "target": "next_node", "source_handle": None}]}
        assert compute_next_node_id(node, ctx, edges_by_source) == "next_node"

    def test_an_unwired_error_port_ends_the_run_there_rather_than_guessing(self):
        node = {"id": "n2", "type": "code"}
        ctx = {"vars": {"_code_failed": True}}
        edges_by_source = {"n2": [{"source": "n2", "target": "success_node", "source_handle": "success"}]}
        assert compute_next_node_id(node, ctx, edges_by_source) is None


class TestRealRetryOnFailure:
    def test_retry_on_failure_genuinely_retries_once_before_giving_up(self, monkeypatch):
        import app.harness as harness_mod
        call_count = {"n": 0}

        def failing(code):
            call_count["n"] += 1
            return {"stdout": "", "stderr": "boom", "exit_code": 1, "sandboxed": True}

        monkeypatch.setattr(harness_mod, "run_sandboxed_code", failing)
        ctx = make_ctx()
        cfg = {"code": "raise ValueError()", "retry_on_failure": True, "error_handling": "Default value", "default_value": "gave up"}
        result = handle_code(cfg, ctx)
        assert call_count["n"] == 2  # the real initial attempt + one real retry, not just one
        assert result["vars"]["result"] == "gave up"

    def test_without_retry_on_failure_only_calls_the_sandbox_once(self, monkeypatch):
        import app.harness as harness_mod
        call_count = {"n": 0}

        def failing(code):
            call_count["n"] += 1
            return {"stdout": "", "stderr": "boom", "exit_code": 1, "sandboxed": True}

        monkeypatch.setattr(harness_mod, "run_sandboxed_code", failing)
        ctx = make_ctx()
        cfg = {"code": "raise ValueError()", "error_handling": "Default value"}
        handle_code(cfg, ctx)
        assert call_count["n"] == 1

    def test_a_retry_that_genuinely_succeeds_the_second_time_is_used(self, monkeypatch):
        import app.harness as harness_mod
        call_count = {"n": 0}

        def flaky(code):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"stdout": "", "stderr": "transient", "exit_code": 1, "sandboxed": True}
            return {"stdout": "recovered", "stderr": "", "exit_code": 0, "sandboxed": True}

        monkeypatch.setattr(harness_mod, "run_sandboxed_code", flaky)
        ctx = make_ctx()
        cfg = {"code": "whatever", "retry_on_failure": True}
        result = handle_code(cfg, ctx)
        assert call_count["n"] == 2
        assert result["vars"]["_code_failed"] is False
        assert result["vars"]["result"].strip() == "recovered"


class TestRealSandboxIsolation:
    """Real process isolation, exercised through the real handle_code
    path — not re-testing sandbox.py's own rlimits directly (that's
    real, separate coverage), just confirming handle_code's wrapping
    doesn't defeat it."""

    def test_no_network_access_is_genuinely_available_to_injected_code(self):
        ctx = make_ctx()
        cfg = {"code": (
            "def main(arg1, arg2):\n"
            "    import socket\n"
            "    try:\n"
            "        socket.create_connection(('8.8.8.8', 53), timeout=2)\n"
            "        return 'network reachable'\n"
            "    except Exception as e:\n"
            "        return 'blocked: ' + type(e).__name__\n"
        )}
        handle_code(cfg, ctx)
        assert ctx["vars"]["result"] != "network reachable"

    def test_wall_clock_timeout_genuinely_kills_a_real_infinite_loop(self):
        ctx = make_ctx()
        cfg = {"code": "while True:\n    pass"}
        with pytest.raises(CodeExecutionError):
            handle_code(cfg, ctx)

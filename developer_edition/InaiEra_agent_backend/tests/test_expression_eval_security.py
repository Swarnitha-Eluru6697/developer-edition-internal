"""
Security regression tests for the eval() sandbox escape in
condition/loop expressions (section 0.102, GAP_REPORT).

The real, critical vulnerability, exactly as reported: eval_expr
(harness.py) called Python's own real eval() with an empty
__builtins__ dict, believing that was a valid sandbox. It never was —
an empty __builtins__ dict blocks direct calls to builtin FUNCTIONS,
but does nothing at all to stop attribute traversal through Python's
own, always-reachable runtime object graph. Confirmed directly, live,
before writing this fix: `().__class__.__mro__[1].__subclasses__()`
genuinely enumerated 830 real, live classes in this process, including
subprocess.Popen — a complete path to arbitrary code execution in the
Agent API process or the Temporal worker process, entirely outside the
real, dedicated Code Node sandbox this codebase otherwise invests
considerable effort hardening.

The fix: eval() is not used anywhere in eval_expr anymore. The
expression is parsed with ast.parse(..., mode="eval") and the tree is
walked by a new, explicit allow-list (_safe_eval_ast_node) that only
permits Compare/BoolOp/UnaryOp/BinOp/Name/Constant/List/Tuple and their
own specific operators — every other real AST node type, named
individually below, is rejected outright.

These tests exercise eval_expr directly (the exact, real function the
report cites) AND through a real, running flow's own condition node,
proving the fix holds at both the unit level and the full,
end-to-end execution level.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.harness import eval_expr

client = TestClient(app)


class TestRealEvalExprNoLongerUsesPythonEval:
    """The report's own exact, specific attack, and every explicitly
    named variant it asked for regression coverage of."""

    def test_the_exact_reported_mro_subclasses_attack_is_blocked(self):
        # The report's own, exact, verified attack expression.
        assert eval_expr("().__class__.__mro__[1].__subclasses__() and True", {}) is False

    def test_dunder_class_access_is_blocked(self):
        assert eval_expr("(1).__class__", {}) is False
        assert eval_expr('"x".__class__', {}) is False

    def test_dunder_mro_access_is_blocked(self):
        assert eval_expr("(1).__class__.__mro__", {}) is False

    def test_dunder_subclasses_call_is_blocked(self):
        assert eval_expr("(1).__class__.__subclasses__()", {}) is False

    def test_lambda_is_blocked(self):
        assert eval_expr("(lambda: 1)()", {}) is False
        assert eval_expr("(lambda x: x)(1) == 1", {}) is False

    def test_function_calls_are_blocked(self):
        assert eval_expr("print(1)", {}) is False
        assert eval_expr('__import__("os")', {}) is False
        assert eval_expr('open("/etc/passwd")', {}) is False

    def test_generic_attribute_traversal_is_blocked(self):
        assert eval_expr("[].__class__.__bases__[0]", {}) is False
        assert eval_expr("().__doc__", {}) is False

    def test_list_comprehensions_are_blocked(self):
        assert eval_expr("[x for x in range(10)]", {}) is False

    def test_dict_comprehensions_are_blocked(self):
        assert eval_expr("{x: x for x in range(10)}", {}) is False

    def test_generator_expressions_are_blocked(self):
        assert eval_expr("any(x for x in range(10))", {}) is False

    def test_walrus_named_expr_is_blocked(self):
        assert eval_expr("(x := 5) == 5", {}) is False

    def test_subscript_is_blocked(self):
        # A bare subscript on a name (not attribute traversal) is also
        # outside the allow-list — Subscript is not a permitted node.
        assert eval_expr("{{items}}[0] == 1", {"items": [1, 2, 3]}) is False

    def test_a_malicious_expression_never_raises_out_of_eval_expr(self):
        """A rejected/malicious expression fails closed (returns
        False) exactly like a syntax error would — it must never
        propagate an exception out to the caller."""
        for expr in [
            "().__class__.__mro__[1].__subclasses__()",
            "(lambda: __import__('os').system('id'))()",
            "exec('import os')",
        ]:
            result = eval_expr(expr, {})
            assert result is False


class TestRealEvalExprLegitimateExpressionsStillWork:
    """The fix must not regress the actual, intended expression
    language real flows already depend on."""

    def test_simple_numeric_comparison(self):
        assert eval_expr("{{score}} >= 70", {"score": 85}) is True
        assert eval_expr("{{score}} >= 70", {"score": 50}) is False

    def test_string_equality(self):
        assert eval_expr('{{status}} == "approved"', {"status": "approved"}) is True
        assert eval_expr('{{status}} == "approved"', {"status": "pending"}) is False

    def test_boolean_and_or(self):
        assert eval_expr("{{a}} > 0 and {{b}} < 10", {"a": 5, "b": 3}) is True
        assert eval_expr("{{a}} > 0 and {{b}} < 10", {"a": -1, "b": 3}) is False
        assert eval_expr("{{a}} > 100 or {{b}} < 10", {"a": 5, "b": 3}) is True

    def test_not(self):
        assert eval_expr("not ({{a}} > 100)", {"a": 5}) is True

    def test_in_and_not_in(self):
        assert eval_expr('{{status}} in ("approved", "pending")', {"status": "pending"}) is True
        assert eval_expr('{{status}} not in ("approved", "pending")', {"status": "denied"}) is True

    def test_safe_arithmetic(self):
        assert eval_expr("{{score}} + 5 >= 70", {"score": 66}) is True
        assert eval_expr("{{score}} - 5 >= 70", {"score": 66}) is False

    def test_missing_variable_defaults_to_zero(self):
        assert eval_expr("{{missing}} == 0", {}) is True

    def test_malformed_expression_fails_closed(self):
        assert eval_expr("{{a}} >=", {"a": 1}) is False
        assert eval_expr("", {}) is False


class TestRealConditionNodeThroughFullFlowExecution:
    """The exact, real, end-to-end path a malicious flow author would
    actually use — a condition node's own `expression` config, run
    through the real running API, not just the unit-level function."""

    def test_a_malicious_condition_expression_in_a_real_flow_is_safely_blocked(self):
        r = client.post("/api/agents", json={
            "name": f"Eval Escape Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "condition", "x": 100, "y": 0, "sub": "",
                     "config": {"expression": "().__class__.__mro__[1].__subclasses__() and True"}},
                    {"id": "n3", "type": "template", "x": 200, "y": -50, "sub": "", "config": {"template": "TRUE"}},
                    {"id": "n4", "type": "template", "x": 200, "y": 50, "sub": "", "config": {"template": "FALSE"}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": "true"},
                    {"id": "e3", "source": "n2", "target": "n4", "source_handle": "false"},
                ],
            },
        })
        agent = r.json()
        try:
            run_r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "test", "invoking_user": "user:you"})
            assert run_r.status_code == 200, run_r.text
            body = run_r.json()
            assert body["status"] == "success"
            # The malicious expression must evaluate falsy (blocked),
            # routing to the FALSE branch — never crashing the run,
            # never silently truthy, never leaking a real class list.
            assert body["output"] == "FALSE"
        finally:
            with Session(engine) as s:
                for t in ("run", "trace", "agentlog", "agentversion"):
                    s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent["id"]})
                s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent["id"]})
                s.commit()

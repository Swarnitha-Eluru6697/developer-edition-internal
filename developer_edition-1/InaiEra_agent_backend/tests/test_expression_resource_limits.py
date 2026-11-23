"""The condition evaluator needs resource limits, not just an allow-list.

The AST allow-list closed the code-execution hole. It did not bound
cost, and a valid-but-pathological expression could consume the process
without escaping the sandbox. Measured on this build before these
limits, in a 2 GB container:

    "'a' * 200000000 == ''"   ->  False in 0.04s, peak RSS +191 MB

Twenty-one characters. It returns False — the allocation succeeds and
the comparison is simply false — so nothing in the result hints that a
fifth of a gigabyte was just allocated and freed. A handful of
concurrent runs exhausts the container while every run looks ordinary.
After the limits, the same expression allocates nothing.

Two properties matter and both are tested here: the pathological cases
are refused, AND ordinary conditions still evaluate exactly as before. A
limit that broke real expressions would just move the problem.
"""
import os
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import (MAX_CONTAINER_ELEMENTS, MAX_EXPRESSION_AST_NODES,
                         MAX_EXPRESSION_CHARS, MAX_EXPRESSION_DEPTH,
                         MAX_INT_MAGNITUDE, MAX_RESULT_STRING_CHARS,
                         MAX_STRING_LITERAL_CHARS, eval_expr, validate_expression)


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform == "linux" else 1024 * 1024)


class TestOrdinaryConditionsStillWork:
    """The guard on the guards. Every one of these is the kind of
    expression a condition node exists for."""

    @pytest.mark.parametrize("expr,variables,expected", [
        ("{{score}} > 0.8", {"score": 0.91}, True),
        ("{{score}} > 0.8", {"score": 0.42}, False),
        ("{{status}} == 'approved'", {"status": "approved"}, True),
        ("{{status}} != 'approved'", {"status": "rejected"}, True),
        ("{{count}} + 2 >= 5 and {{flag}}", {"count": 4, "flag": True}, True),
        ("{{count}} - 1 < 0", {"count": 0}, True),
        ("{{a}} * 3 == 9", {"a": 3}, True),
        ("{{total}} / 2 == 5.0", {"total": 10}, True),
        ("{{total}} % 2 == 0", {"total": 10}, True),
        ("{{name}} in ['alice', 'bob']", {"name": "bob"}, True),
        ("{{name}} not in ['alice', 'bob']", {"name": "carol"}, True),
        ("not {{flag}}", {"flag": False}, True),
        ("-{{n}} < 0", {"n": 5}, True),
        ("{{a}} < {{b}} < {{c}}", {"a": 1, "b": 2, "c": 3}, True),
        ("'urgent' in {{subject}}", {"subject": "an urgent request"}, True),
    ])
    def test_it_evaluates_normally(self, expr, variables, expected):
        assert eval_expr(expr, variables) is expected

    def test_a_normal_expression_passes_publish_validation(self):
        assert validate_expression("{{score}} > 0.8 and {{status}} == 'ok'") is None


class TestSequenceMultiplicationIsGone:
    """The cheapest attack, removed rather than merely bounded: nothing
    in a condition is decided by repeating a string."""

    @pytest.mark.parametrize("expr", [
        "'a' * 200000000 == ''",
        "200000000 * 'a' == ''",
        "[0] * 20000000 == []",
        "(0,) * 20000000 == ()",
        "'ab' * 1000 * 1000 == ''",
    ])
    def test_it_is_refused(self, expr):
        assert eval_expr(expr, {}) is False

    def test_it_is_refused_for_a_variable_too(self):
        """The literal check alone would miss this: the sequence arrives
        through a flow variable."""
        assert eval_expr("{{s}} * 100000000 == ''", {"s": "ab"}) is False

    def test_publish_explains_why(self):
        problem = validate_expression("'a' * 1000000 == ''")
        assert problem and "multiplying a string" in problem

    def test_numeric_multiplication_still_works(self):
        """Only SEQUENCE multiplication is gone — arithmetic is the
        point of having operators at all."""
        assert eval_expr("{{a}} * 3 == 12", {"a": 4}) is True


class TestSizeLimits:
    def test_an_oversized_string_literal_is_refused(self):
        expr = "'" + "x" * (MAX_STRING_LITERAL_CHARS + 1) + "' == ''"
        assert eval_expr(expr, {}) is False

    def test_a_literal_at_the_limit_is_accepted(self):
        """Bounds checked from both sides, so the limit is the limit and
        not an approximation.

        Asserted through validate_expression rather than eval_expr,
        because eval_expr returns False both for "refused" and for
        "evaluated to false" — which is exactly the ambiguity that makes
        publish-time validation worth having. Note the limits interact
        by design: MAX_STRING_LITERAL_CHARS is half of
        MAX_EXPRESSION_CHARS, so a literal at the limit leaves room for
        one more of its size and no more."""
        at_limit = "x" * MAX_STRING_LITERAL_CHARS
        assert validate_expression(f"{{{{s}}}} == '{at_limit}'") is None

        over_limit = "x" * (MAX_STRING_LITERAL_CHARS + 1)
        problem = validate_expression(f"{{{{s}}}} == '{over_limit}'")
        assert problem and "string literal" in problem

    def test_an_expression_longer_than_the_character_limit_is_refused(self):
        assert eval_expr(" + ".join(["1"] * MAX_EXPRESSION_CHARS) + " > 0", {}) is False

    def test_a_variable_holding_a_huge_string_cannot_inflate_the_expression(self):
        """The limit applies after substitution, because a short
        expression plus a megabyte variable is a megabyte expression."""
        assert eval_expr("{{blob}} == 'x'", {"blob": "y" * (MAX_EXPRESSION_CHARS + 500)}) is False

    def test_a_variable_holding_a_huge_string_is_bounded_on_its_own(self):
        assert eval_expr("{{blob}} != ''", {"blob": "y" * (MAX_RESULT_STRING_CHARS + 1)}) is False

    def test_too_many_ast_nodes_is_refused(self):
        assert eval_expr(" and ".join(["1 > 0"] * MAX_EXPRESSION_AST_NODES), {}) is False

    def test_an_oversized_container_literal_is_refused(self):
        assert eval_expr("1 in [" + ",".join(["0"] * (MAX_CONTAINER_ELEMENTS + 1)) + "]", {}) is False

    def test_a_container_at_the_limit_is_accepted(self):
        elements = ",".join(["0"] * (MAX_CONTAINER_ELEMENTS - 1)) + ",1"
        assert eval_expr(f"1 in [{elements}]", {}) is True


class TestNumericLimits:
    def test_an_oversized_integer_literal_is_refused(self):
        assert eval_expr(f"{MAX_INT_MAGNITUDE * 10} > 0", {}) is False

    def test_arithmetic_that_grows_past_the_limit_is_refused(self):
        """The RESULT is bounded, not only the operands: two acceptable
        numbers can multiply into an unacceptable one, and
        arbitrary-precision arithmetic is how a short expression becomes
        unbounded work."""
        big = MAX_INT_MAGNITUDE // 2
        assert eval_expr(f"{big} * 1000 > 0", {}) is False

    def test_a_variable_holding_a_huge_integer_is_refused(self):
        assert eval_expr("{{n}} > 0", {"n": MAX_INT_MAGNITUDE * 5}) is False

    def test_ordinary_numbers_are_unaffected(self):
        assert eval_expr("{{n}} * 1000 == 5000000", {"n": 5000}) is True


class TestNestingLimits:
    def test_deeply_nested_operations_are_refused(self):
        expr = "1"
        for _ in range(MAX_EXPRESSION_DEPTH + 10):
            expr = f"1+({expr})"
        assert eval_expr(expr + " > 0", {}) is False

    def test_moderate_nesting_still_works(self):
        expr = "1"
        for _ in range(5):
            expr = f"1+({expr})"
        assert eval_expr(expr + " > 0", {}) is True

    def test_parenthesis_only_nesting_is_free(self):
        """Parens do not create AST nodes, so they cost nothing and must
        not be refused — bounding them would be a limit on nothing."""
        assert eval_expr("(" * 100 + "1" + ")" * 100 + " > 0", {}) is True


class TestItActuallyBoundsResources:
    def test_the_reported_payload_allocates_nothing_measurable(self):
        """The specific measurement from the report's own concern. Before
        the limits this allocated ~191 MB; a tolerance of 25 MB here
        distinguishes "allocated nothing" from "allocated a fifth of a
        gigabyte" without being flaky about ordinary interpreter noise."""
        before = peak_rss_mb()
        assert eval_expr("'a' * 200000000 == ''", {}) is False
        assert eval_expr("[0] * 20000000 == []", {}) is False
        after = peak_rss_mb()
        assert after - before < 25, f"peak RSS grew by {after - before:.0f} MB"


class TestPublishTimeRejection:
    """A condition that cannot be evaluated must fail at publish, not
    take its false branch forever. eval_expr returning False is right at
    run time but indistinguishable from a condition simply not being
    met, which reads as a logic bug in the flow."""

    @pytest.mark.parametrize("expr,fragment", [
        ("'a' * 1000000 == ''", "multiplying a string"),
        ("{{x}} >", "not a valid expression"),
        ("__import__('os')", "does not allow"),
        ("open('/etc/passwd')", "does not allow"),
        # Attribute access on a NAME, so it parses: substituting {{x}}
        # would give "0.__class__", which is a syntax error and would
        # test the wrong branch.
        ("x.__class__", "does not allow"),
        ("", "empty"),
        (f"{MAX_INT_MAGNITUDE * 10} > 0", "integer beyond"),
    ])
    def test_validate_expression_explains_the_problem(self, expr, fragment):
        problem = validate_expression(expr)
        assert problem is not None, f"{expr!r} was accepted"
        assert fragment in problem, f"{problem!r} does not mention {fragment!r}"

    def test_the_flow_validator_refuses_such_a_condition(self):
        from app.flow_validator import validate_flow

        nodes = [
            {"id": "n1", "type": "trigger", "config": {}},
            {"id": "n2", "type": "condition", "config": {"expression": "'a' * 200000000 == ''"}},
            {"id": "n3", "type": "respond", "config": {}},
        ]
        edges = [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": "true"},
            {"id": "e3", "source": "n2", "target": "n3", "source_handle": "false"},
        ]
        result = validate_flow(nodes, edges, "chatflow")
        assert result.is_valid is False
        assert any("Expression cannot be evaluated" in i.message for i in result.errors)

    def test_a_valid_condition_still_publishes(self):
        from app.flow_validator import validate_flow

        nodes = [
            {"id": "n1", "type": "trigger", "config": {}},
            {"id": "n2", "type": "condition", "config": {"expression": "{{score}} > 0.8"}},
            {"id": "n3", "type": "respond", "config": {}},
        ]
        edges = [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": "true"},
            {"id": "e3", "source": "n2", "target": "n3", "source_handle": "false"},
        ]
        result = validate_flow(nodes, edges, "chatflow")
        assert not any("Expression" in i.message for i in result.errors), \
            [i.message for i in result.errors]


class TestTheSandboxIsStillClosed:
    """The limits must not have loosened the original allow-list."""

    @pytest.mark.parametrize("expr", [
        "__import__('os').system('id')",
        "().__class__.__bases__",
        "[x for x in range(10)]",
        "lambda: 1",
        "open('/etc/passwd').read()",
        "{'a': 1}",
        "{1, 2}",
        "1 if True else 2",
        "f'{1}'",
        "x := 1",
    ])
    def test_it_is_still_refused(self, expr):
        assert eval_expr(expr, {}) is False

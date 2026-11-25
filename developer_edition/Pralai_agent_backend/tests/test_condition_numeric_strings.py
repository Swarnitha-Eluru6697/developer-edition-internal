"""A condition must compare a form's value as a number.

Trigger fields produce STRINGS — a form input always does. So
`{{amount}} > 10000` arrives as `'25000' > 10000`, which raises
TypeError in Python 3. eval_expr caught every exception and returned
False, so the condition silently took its ELSE branch.

Live consequence on Showcase 2: the same amount escalated correctly when
passed as a number and auto-approved when passed as a string. An
approval threshold that fails OPEN is the worst direction for it to
fail, and nothing in the trace said why.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import eval_expr


@pytest.mark.parametrize("amount", ["25000", 25000, 25000.0, " 25000 "])
def test_a_value_over_the_threshold_is_over_it_however_it_arrives(amount):
    """The bug: only the int form worked."""
    assert eval_expr("{{amount}} > 10000", {"amount": amount}) is True


@pytest.mark.parametrize("amount", ["500", 500, "10000", 10000])
def test_a_value_at_or_under_the_threshold_is_not_over_it(amount):
    assert eval_expr("{{amount}} > 10000", {"amount": amount}) is False


def test_a_decimal_string_compares_as_a_number():
    assert eval_expr("{{score}} >= 0.8", {"score": "0.85"}) is True
    assert eval_expr("{{score}} >= 0.8", {"score": "0.75"}) is False


def test_a_negative_string_compares_as_a_number():
    assert eval_expr("{{delta}} < 0", {"delta": "-5"}) is True


def test_text_still_compares_as_text():
    """Coercion must not reach comparisons between two strings, or an
    identifier with leading zeros would stop matching itself."""
    assert eval_expr('{{code}} == "007"', {"code": "007"}) is True
    assert eval_expr('{{code}} == "7"', {"code": "007"}) is False
    assert eval_expr('{{name}} == "alice"', {"name": "alice"}) is True


def test_a_non_numeric_string_against_a_number_is_still_false():
    """There is no sensible ordering between "abc" and 10, so the branch
    is not taken — but it is now logged rather than silent."""
    assert eval_expr("{{amount}} > 10", {"amount": "abc"}) is False


def test_a_boolean_is_not_treated_as_a_number():
    """bool is an int in Python, so True > 0 would quietly be true.
    A flag is not a quantity."""
    assert eval_expr('{{flag}} == "yes"', {"flag": True}) is False


def test_a_missing_variable_still_defaults_to_zero():
    """Existing behaviour, kept: an unset variable substitutes 0."""
    assert eval_expr("{{nope}} > 10", {}) is False
    assert eval_expr("{{nope}} < 10", {}) is True


def test_the_showcase_2_threshold_as_a_form_would_send_it():
    """The exact reported case, end to end through the evaluator."""
    assert eval_expr("{{amount}} > 1000", {"amount": "25000"}) is True
    assert eval_expr("{{amount}} > 1000", {"amount": "500"}) is False

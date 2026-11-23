"""Typed rule parameters."""
import pytest

from app.renderers.params import ParameterError, validate_parameters


@pytest.mark.parametrize("rt,params", [
    ("range", {"min": "0 OR 1=1"}), ("range", {"min": 5, "max": 1}), ("range", {"min": True}),
    ("regex", {"pattern": "("}), ("regex", {"pattern": "a\nb"}),
    ("accepted_values", {"values": "abc"}), ("accepted_values", {"values": [{"a": 1}]}),
    ("freshness", {"max_age_hours": -1}), ("row_count", {"min_rows": 1.5}),
    ("not_null", {"unexpected": 1}), ("not_null", {"threshold": 2}),
    ("custom_python", {"expectation": "drop_table"}),
    ("custom_python", {"expectation": "expect_x", "kw": {"nested": 1}}),
    ("custom_sql", {"condition": "1=1; drop"}),
])
def test_bad_parameters_rejected(rt, params):
    with pytest.raises(ParameterError):
        validate_parameters(rt, params)


def test_template_parameters_ok_but_strict_requires_content():
    assert validate_parameters("accepted_values", {"values": []}) == {"values": []}
    with pytest.raises(ParameterError):
        validate_parameters("accepted_values", {"values": []}, strict=True)
    with pytest.raises(ParameterError):
        validate_parameters("range", {"min": None, "max": None}, strict=True)


def test_normalization_and_threshold():
    assert validate_parameters("row_count", {}) == {"min_rows": 1, "max_rows": None}
    assert validate_parameters("not_null", {"threshold": 0.05}) == {"threshold": 0.05}

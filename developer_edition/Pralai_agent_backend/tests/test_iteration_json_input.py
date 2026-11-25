"""An iteration must iterate the array a JSON trigger field holds.

A `json_code` trigger field arrives as a STRING of JSON text — that is
what the form produces. The iteration node tested
`isinstance(value, list)`, which a string fails, then set its output to
[] and returned without a word.

Showcase 3 therefore returned `labelled: []` for every input anyone
gave it, and an empty array reads as "nothing matched" rather than as a
mistake. The same guard sat in the list operator.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import _as_list


def test_a_json_string_from_a_form_is_an_array():
    """The reported case."""
    assert _as_list('["alpha","beta","gamma"]') == ["alpha", "beta", "gamma"]


def test_an_actual_list_passes_through():
    assert _as_list([1, 2, 3]) == [1, 2, 3]


def test_an_empty_json_array_is_an_empty_array_not_a_failure():
    """Distinct from unusable input: the caller iterates nothing, which
    is correct, rather than logging a warning about bad input."""
    assert _as_list("[]") == []
    assert _as_list([]) == []


def test_whitespace_around_the_json_is_tolerated():
    assert _as_list('  ["a"]  ') == ["a"]


def test_a_json_object_is_not_an_array():
    """{"a": 1} parses, but iterating it is not what was asked for."""
    assert _as_list('{"a": 1}') is None


def test_a_plain_sentence_is_not_an_array():
    assert _as_list("alpha, beta, gamma") is None


def test_malformed_json_is_refused_rather_than_guessed():
    """Bracketed but broken — splitting it by hand would invent data."""
    assert _as_list('["alpha", "beta"') is None
    assert _as_list("[not json]") is None


def test_a_number_is_not_an_array():
    assert _as_list(42) is None
    assert _as_list(None) is None


def test_nested_arrays_survive():
    assert _as_list('[["a", 1], ["b", 2]]') == [["a", 1], ["b", 2]]


def test_objects_inside_the_array_survive():
    """The list operator filters on properties, so the elements have to
    keep their shape."""
    assert _as_list('[{"line": "x", "length": 1}]') == [{"line": "x", "length": 1}]


class TestUnusableInputIsVisible:
    """An input that cannot be read must not look like an empty result.

    Returning [] for `[alpha, beta]` — unquoted, so not JSON — meant the
    screen showed `labelled: []` with the reason only in a server log.
    Four normal ways of typing a list did that: unquoted, single-quoted,
    a plain comma list, and a trailing comma.
    """

    def _iterate(self, raw):
        """Drive the same guard run_iteration_body uses."""
        from app.harness import _as_list
        value = _as_list(raw)
        if value is None and raw not in (None, ""):
            raise ValueError("not an array")
        return value if value is not None else []

    @pytest.mark.parametrize("raw", [
        "[alpha, beta]",          # unquoted
        "['alpha', 'beta']",      # single quotes
        "alpha, beta",            # no brackets
        '["alpha", "beta",]',     # trailing comma
        '{"a": 1}',               # an object, not an array
    ])
    def test_input_that_cannot_be_read_raises(self, raw):
        with pytest.raises(ValueError):
            self._iterate(raw)

    @pytest.mark.parametrize("raw", ["", None])
    def test_nothing_supplied_is_an_empty_iteration_not_an_error(self, raw):
        """Asking to iterate nothing is a legitimate thing to ask for."""
        assert self._iterate(raw) == []

    def test_an_explicitly_empty_array_is_also_fine(self):
        assert self._iterate("[]") == []

    def test_a_valid_array_still_iterates(self):
        assert self._iterate('["alpha","beta"]') == ["alpha", "beta"]

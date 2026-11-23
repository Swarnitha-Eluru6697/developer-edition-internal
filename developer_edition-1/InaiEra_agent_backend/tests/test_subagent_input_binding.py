"""What a subagent node actually passes to the agent it calls.

Two defects, both found by building an agent that calls another one:

  1. Only the literal string "{{output}}" was special-cased. Every other
     template — {{question}}, {{llm_output}} — was handed to the target
     as its own literal text, so the target answered a question that
     read "{{question}}". The node's help calls the field a template.

  2. The target was invoked with a plain string and no field bindings.
     An agent whose trigger declares named fields reads {{question}},
     not {{input}}, so it saw nothing and replied "No question was
     provided" — which reads as the agent being wrong when in fact it
     was never asked anything. batch-run and the eval runner already
     threaded this through; the subagent hop was the last channel that
     did not.

And one that fell out of the same run: a structured variable rendered
into text produced the dict's repr.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import harness


TRIGGER_ONE_FIELD = {"nodes": [{"type": "trigger", "config": {"fields": [
    {"id": "fq", "name": "question", "type": "paragraph"}]}}]}
TRIGGER_TWO_FIELDS = {"nodes": [{"type": "trigger", "config": {"fields": [
    {"id": "fa", "name": "city", "type": "text"},
    {"id": "fb", "name": "day", "type": "text"}]}}]}
TRIGGER_NO_FIELDS = {"nodes": [{"type": "trigger", "config": {}}]}


# --- binding a string onto the target's declared fields -------------------

def test_a_single_declared_field_takes_the_whole_input():
    assert harness.structured_inputs_for_flow(TRIGGER_ONE_FIELD, "How do I dispute a charge?") \
        == {"question": "How do I dispute a charge?"}


def test_a_trigger_with_no_declared_fields_binds_nothing():
    """Correct, not a gap: such a flow reads {{input}}, which is passed
    separately."""
    assert harness.structured_inputs_for_flow(TRIGGER_NO_FIELDS, "anything") == {}


def test_a_json_object_fills_a_multi_field_trigger():
    assert harness.structured_inputs_for_flow(
        TRIGGER_TWO_FIELDS, '{"city": "Berlin", "day": "Tuesday"}') \
        == {"city": "Berlin", "day": "Tuesday"}


def test_a_plain_string_cannot_fill_a_multi_field_trigger():
    """Guessing which of two fields a bare string belongs in would be
    wrong half the time; binding nothing is the honest outcome."""
    assert harness.structured_inputs_for_flow(TRIGGER_TWO_FIELDS, "Berlin") == {}


def test_malformed_json_falls_back_rather_than_raising():
    """A question that merely starts with a brace is still a question."""
    assert harness.structured_inputs_for_flow(TRIGGER_ONE_FIELD, '{not json') \
        == {"question": '{not json'}


def test_a_file_field_is_not_filled_from_text():
    flow = {"nodes": [{"type": "trigger", "config": {"fields": [
        {"id": "f1", "name": "doc", "type": "single_file"},
        {"id": "f2", "name": "question", "type": "text"}]}}]}
    assert harness.structured_inputs_for_flow(flow, "hello") == {"question": "hello"}


def test_a_hidden_prefilled_field_is_not_counted():
    """It is already filled, so it must not absorb the caller's input."""
    flow = {"nodes": [{"type": "trigger", "config": {"fields": [
        {"id": "f1", "name": "secret", "type": "text", "hidden_prefilled": True},
        {"id": "f2", "name": "question", "type": "text"}]}}]}
    assert harness.structured_inputs_for_flow(flow, "hello") == {"question": "hello"}


# --- rendering a structured variable into text ---------------------------

def test_llm_output_renders_as_its_text_not_its_dict():
    """The LLM node stores {"text": ..., "mock": ...}. Rendered with
    str() a template referencing {{llm_output}} emitted
    "{'text': 'Route: RESOLVED', 'mock': False}" — punctuation and all.
    Showcase 1's own template node references {{llm_output}}, so it had
    been emitting exactly that."""
    ctx = {"vars": {"llm_output": {"text": "Route: RESOLVED", "mock": False}}, "input": ""}
    assert harness._interp_vars("{{llm_output}}", ctx) == "Route: RESOLVED"


def test_an_llm_that_failed_renders_as_empty_not_as_none():
    ctx = {"vars": {"llm_output": {"text": None, "error": "boom"}}, "input": ""}
    assert harness._interp_vars("{{llm_output}}", ctx) == ""


def test_a_file_variable_still_renders_as_a_link():
    ctx = {"vars": {"doc": {"filename": "report.pdf", "url": "/f/1"}}, "input": ""}
    assert harness._interp_vars("{{doc}}", ctx) == "[report.pdf](/f/1)"


def test_an_ordinary_variable_is_unaffected():
    ctx = {"vars": {"topic": "billing", "n": 3}, "input": "raw"}
    assert harness._interp_vars("{{topic}}/{{n}}/{{input}}", ctx) == "billing/3/raw"


def test_an_unknown_variable_renders_empty():
    assert harness._interp_vars("[{{nope}}]", {"vars": {}, "input": ""}) == "[]"


@pytest.mark.parametrize("val,expected", [
    ({"text": "hi"}, "hi"),
    ({"filename": "a.pdf", "url": "/u"}, "[a.pdf](/u)"),
    ({"other": 1}, "{'other': 1}"),   # no known shape — repr is all there is
    (None, ""),
    (0, "0"),                          # falsy but real
    (False, "False"),
])
def test_var_to_text_shapes(val, expected):
    assert harness._var_to_text(val) == expected

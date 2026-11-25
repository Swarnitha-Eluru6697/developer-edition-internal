"""The Question Classifier classifies what `input_var` points at.

It used to classify ctx["input"] unconditionally and ignore input_var
completely — while the config panel asked for it and flow_validator
REFUSED to publish without it ("Input Variables is required"). A
required field therefore decided nothing, and a classifier placed after
a retrieval or an LLM node silently graded the original user text
instead of that node's output.

Found on a real flow: trigger -> gov -> retrieval -> classifier ->
output, with every {{var}} pointing at `{{record}}` — a name nothing in
the flow defined. Every span reported ok and the run returned
`{"result": ""}`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import harness


@pytest.fixture
def captured(monkeypatch):
    """Records the prompt the classifier actually sends."""
    seen = {}

    def fake_llm(ctx, prompt, model=None, system=None, **kw):
        seen["prompt"] = prompt
        seen["system"] = system
        seen["model"] = model
        return {"text": "billing", "model": model or "stub", "mock": False}

    monkeypatch.setattr(harness, "_call_llm_tracked", fake_llm)
    return seen


def _ctx(**vars_):
    return {"input": "the original user question", "vars": dict(vars_), "output": None}


class TestTheClassifierUsesInputVar:
    def test_it_classifies_the_referenced_variable(self, captured):
        ctx = _ctx(retrieved_chunks="a paragraph about duplicate invoice charges")
        harness.handle_classifier(
            {"class1": "account", "class2": "billing",
             "input_var": "{{retrieved_chunks}}"}, ctx)
        assert "duplicate invoice charges" in captured["prompt"]
        assert "the original user question" not in captured["prompt"], \
            "the classifier graded the run input instead of the variable it was pointed at"

    def test_it_falls_back_to_the_run_input(self, captured):
        """Unset means "the user's question", which is what it always did
        — so an existing flow with no input_var keeps working."""
        harness.handle_classifier({"class1": "account", "class2": "billing"}, _ctx())
        assert "the original user question" in captured["prompt"]

    def test_input_is_referenceable_explicitly(self, captured):
        harness.handle_classifier(
            {"class1": "a", "class2": "b", "input_var": "{{input}}"}, _ctx())
        assert "the original user question" in captured["prompt"]

    def test_a_variable_that_does_not_exist_yields_no_subject(self, captured):
        """The actual reported flow: {{record}} was defined nowhere, so
        there was nothing to classify. It must not silently fall back to
        the user input, or the field would look like it worked."""
        harness.handle_classifier(
            {"class1": "account", "class2": "billing", "input_var": "{{record}}"}, _ctx())
        assert "the original user question" not in captured["prompt"]

    def test_the_chosen_class_is_published(self, captured):
        ctx = _ctx()
        harness.handle_classifier({"class1": "account", "class2": "billing"}, ctx)
        assert ctx["vars"]["class_name"] == "billing"
        assert ctx["vars"]["class_label"] == "billing"

    def test_an_off_list_answer_falls_back_to_the_first_class(self, monkeypatch):
        monkeypatch.setattr(harness, "_call_llm_tracked",
                            lambda ctx, prompt, model=None, system=None, **kw:
                                {"text": "something else entirely", "model": "stub", "mock": False})
        ctx = _ctx()
        harness.handle_classifier({"class1": "account", "class2": "billing"}, ctx)
        assert ctx["vars"]["class_name"] == "account"

    def test_no_classes_means_no_classification(self, captured):
        ctx = _ctx()
        harness.handle_classifier({"input_var": "{{input}}"}, ctx)
        assert ctx["vars"]["class_name"] is None
        assert "prompt" not in captured, "no model call should be made with nothing to choose between"

    def test_the_instruction_is_used_as_the_system_prompt(self, captured):
        harness.handle_classifier(
            {"class1": "a", "class2": "b", "instruction": "Route by owning team."}, _ctx())
        assert captured["system"] == "Route by owning team."

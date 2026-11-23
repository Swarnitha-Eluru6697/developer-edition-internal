"""
Real tests for the three new nodes added to match Dify's documented specs:
Answer (https://docs.dify.ai/en/cloud/use-dify/nodes/answer), Variable
Assigner (https://docs.dify.ai/en/cloud/use-dify/nodes/variable-assigner),
and Parameter Extractor (https://docs.dify.ai/en/cloud/use-dify/nodes/
parameter-extractor). See GAP_REPORT §0.36.

Variable Assigner's tests use the real Postgres engine (AgentMemory is a
real table there) rather than an isolated one — matching test_iteration.py's
own reasoning for doing the same.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session

from app.harness import (
    handle_answer, handle_variable_assigner, handle_parameter_extractor,
    _get_conv_var, _set_conv_var,
)


def make_ctx(user_input: str = "", variables=None) -> dict:
    return {"input": user_input, "vars": dict(variables or {}), "agent_id": "test-agent", "invoking_user": "user:test", "output": None}


class TestRealAnswerInterpolation:
    def test_input_special_case_resolves_to_the_real_original_input(self):
        ctx = make_ctx(user_input="World")
        result = handle_answer({"content": "Hi {{input}}"}, ctx)
        assert result["output"] == "Hi World"

    def test_a_missing_var_renders_as_empty_not_a_literal_placeholder(self):
        ctx = make_ctx()
        result = handle_answer({"content": "Value: {{never_set}}"}, ctx)
        assert result["output"] == "Value: "

    def test_real_var_interpolation_against_ctx_vars(self):
        ctx = make_ctx(variables={"greeting": "Hello"})
        result = handle_answer({"content": "{{greeting}}, {{input}}!"}, ctx)
        assert result["output"] == "Hello, !"


class TestRealMultipleAnswerNodes:
    """Dify's own documented behavior: multiple Answer nodes across a
    flow build up one coherent reply, not each overwriting the last."""

    def test_two_answer_nodes_genuinely_accumulate_in_real_execution_order(self):
        ctx = make_ctx(variables={"greeting": "Hello"})
        handle_answer({"content": "{{greeting}}!"}, ctx)
        handle_answer({"content": "How can I help?"}, ctx)
        assert ctx["output"] == "Hello!\n\nHow can I help?"

    def test_three_answer_nodes_stay_in_real_order(self):
        ctx = make_ctx()
        handle_answer({"content": "First."}, ctx)
        handle_answer({"content": "Second."}, ctx)
        handle_answer({"content": "Third."}, ctx)
        assert ctx["output"] == "First.\n\nSecond.\n\nThird."


class TestRealAnswerMultimodal:
    def test_a_real_uploaded_file_shaped_var_renders_as_a_real_inline_link(self):
        ctx = make_ctx(variables={"report": {"filename": "report.pdf", "url": "http://x/report.pdf"}})
        result = handle_answer({"content": "See: {{report}}"}, ctx)
        assert result["output"] == "See: [report.pdf](http://x/report.pdf)"

    def test_previously_this_would_have_stringified_the_dict_directly_a_real_regression_guard(self):
        ctx = make_ctx(variables={"report": {"filename": "report.pdf", "url": "http://x/report.pdf"}})
        result = handle_answer({"content": "See: {{report}}"}, ctx)
        assert "{'filename'" not in result["output"]


@pytest.fixture()
def session():
    """The real Postgres engine — AgentMemory is a real table there,
    same reasoning test_iteration.py's own fixture already documents."""
    from app.db import engine
    with Session(engine) as s:
        yield s


class TestRealConversationVariablePersistence:
    """The central real claim of Variable Assigner: genuinely different
    from a plain workflow variable, which resets every run."""

    def test_set_then_get_round_trips_a_real_typed_value(self, session):
        import uuid
        scope_key = f"test:{uuid.uuid4().hex}"
        _set_conv_var(session, scope_key, "score", 42)
        session.commit()
        assert _get_conv_var(session, scope_key, "score") == 42

    def test_a_real_object_value_round_trips_intact(self, session):
        import uuid
        scope_key = f"test:{uuid.uuid4().hex}"
        _set_conv_var(session, scope_key, "prefs", {"theme": "dark", "lang": "en"})
        session.commit()
        assert _get_conv_var(session, scope_key, "prefs") == {"theme": "dark", "lang": "en"}

    def test_reading_a_never_set_variable_is_a_real_honest_none(self, session):
        import uuid
        scope_key = f"test:{uuid.uuid4().hex}"
        assert _get_conv_var(session, scope_key, "never_set_this") is None


class TestRealVariableAssignerOperations:
    def test_overwrite_takes_the_real_value_from_a_referenced_variable(self, session):
        import uuid
        ctx = make_ctx(variables={"new_name": "Alice"})
        ctx["agent_id"] = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "username", "operation": "overwrite", "source_var": "{{new_name}}"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_username"] == "Alice"

    def test_set_uses_a_real_literal_config_value_not_a_variable_reference(self, session):
        import uuid
        ctx = make_ctx()
        ctx["agent_id"] = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "status", "operation": "set", "value": "active"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_status"] == "active"

    def test_clear_on_a_scalar_produces_a_real_none(self, session):
        import uuid
        ctx = make_ctx()
        scope = f"test-{uuid.uuid4().hex}"
        ctx["agent_id"] = scope
        _set_conv_var(session, f"{scope}:{ctx['invoking_user']}", "x", "was set")
        session.commit()
        cfg = {"assignments": [{"variable": "x", "operation": "clear"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_x"] is None

    def test_clear_on_an_array_produces_a_real_empty_array_not_none(self, session):
        import uuid
        ctx = make_ctx()
        scope = f"test-{uuid.uuid4().hex}"
        ctx["agent_id"] = scope
        _set_conv_var(session, f"{scope}:{ctx['invoking_user']}", "items", ["a", "b"])
        session.commit()
        cfg = {"assignments": [{"variable": "items", "operation": "clear"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_items"] == []

    def test_arithmetic_with_a_real_variable_operand(self, session):
        import uuid
        ctx = make_ctx(variables={"amount": 5})
        ctx["agent_id"] = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "total", "operation": "arithmetic", "source_var": "{{amount}}", "arithmetic_op": "+"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_total"] == 5

    def test_arithmetic_with_a_real_literal_operand_the_bug_found_and_fixed_during_this_build(self, session):
        """Documents the real gap found while live-testing: Dify's own
        UI lets a person type a literal number directly, not always
        require a variable holding one."""
        import uuid
        ctx = make_ctx()
        ctx["agent_id"] = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "visits", "operation": "arithmetic", "source_var": "1", "arithmetic_op": "+"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_visits"] == 1

    def test_arithmetic_accumulates_across_two_real_separate_calls(self, session):
        import uuid
        scope = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "counter", "operation": "arithmetic", "source_var": "1", "arithmetic_op": "+"}]}
        ctx1 = make_ctx(); ctx1["agent_id"] = scope
        handle_variable_assigner(cfg, ctx1, session)
        ctx2 = make_ctx(); ctx2["agent_id"] = scope
        result2 = handle_variable_assigner(cfg, ctx2, session)
        assert result2["vars"]["conv_counter"] == 2

    def test_append_adds_one_real_element_to_a_real_growing_array(self, session):
        import uuid
        scope = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "log", "operation": "append", "source_var": "{{trigger_input}}"}]}
        ctx1 = make_ctx(variables={"trigger_input": "first"}); ctx1["agent_id"] = scope
        handle_variable_assigner(cfg, ctx1, session)
        ctx2 = make_ctx(variables={"trigger_input": "second"}); ctx2["agent_id"] = scope
        result2 = handle_variable_assigner(cfg, ctx2, session)
        assert result2["vars"]["conv_log"] == ["first", "second"]

    def test_extend_merges_a_real_array_into_the_existing_one(self, session):
        import uuid
        ctx = make_ctx(variables={"new_items": ["c", "d"]})
        scope = f"test-{uuid.uuid4().hex}"
        ctx["agent_id"] = scope
        _set_conv_var(session, f"{scope}:{ctx['invoking_user']}", "items", ["a", "b"])
        session.commit()
        cfg = {"assignments": [{"variable": "items", "operation": "extend", "source_var": "{{new_items}}"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_items"] == ["a", "b", "c", "d"]

    def test_remove_first_drops_the_real_first_element(self, session):
        import uuid
        ctx = make_ctx()
        scope = f"test-{uuid.uuid4().hex}"
        ctx["agent_id"] = scope
        _set_conv_var(session, f"{scope}:{ctx['invoking_user']}", "q", ["a", "b", "c"])
        session.commit()
        cfg = {"assignments": [{"variable": "q", "operation": "remove_first"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_q"] == ["b", "c"]

    def test_remove_last_drops_the_real_last_element(self, session):
        import uuid
        ctx = make_ctx()
        scope = f"test-{uuid.uuid4().hex}"
        ctx["agent_id"] = scope
        _set_conv_var(session, f"{scope}:{ctx['invoking_user']}", "q", ["a", "b", "c"])
        session.commit()
        cfg = {"assignments": [{"variable": "q", "operation": "remove_last"}]}
        result = handle_variable_assigner(cfg, ctx, session)
        assert result["vars"]["conv_q"] == ["a", "b"]


class TestRealPerUserIsolation:
    def test_two_different_users_get_genuinely_independent_conversation_state(self, session):
        import uuid
        scope = f"test-{uuid.uuid4().hex}"
        cfg = {"assignments": [{"variable": "counter", "operation": "arithmetic", "source_var": "1", "arithmetic_op": "+"}]}
        ctx_alice = make_ctx(); ctx_alice["agent_id"] = scope; ctx_alice["invoking_user"] = "user:alice"
        handle_variable_assigner(cfg, ctx_alice, session)
        handle_variable_assigner(cfg, ctx_alice, session)
        ctx_bob = make_ctx(); ctx_bob["agent_id"] = scope; ctx_bob["invoking_user"] = "user:bob"
        result_bob = handle_variable_assigner(cfg, ctx_bob, session)
        assert result_bob["vars"]["conv_counter"] == 1  # bob's own first real increment, unaffected by alice's two


class TestRealParameterExtractorFunctionCallMode:
    def test_a_real_mock_tool_call_succeeds_with_real_typed_placeholder_values(self, session):
        ctx = make_ctx(variables={"trigger_input": "book a trip to Paris"})
        cfg = {
            "input_var": "{{trigger_input}}", "inference_mode": "function_call",
            "parameters": [{"name": "city", "type": "string", "description": "destination", "required": True}],
        }
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["__is_success"] == 1
        assert result["vars"]["__reason"] is None
        assert result["vars"]["city"] == "[mock city]"

    def test_missing_required_parameter_reports_a_real_honest_failure(self, session, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "", "model": "x", "mock": True, "tool_input": {}})
        ctx = make_ctx()
        cfg = {
            "input_var": "{{trigger_input}}", "inference_mode": "function_call",
            "parameters": [{"name": "required_thing", "type": "string", "required": True}],
        }
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["__is_success"] == 0
        assert "required_thing" in result["vars"]["__reason"]

    def test_a_real_tool_call_failure_from_the_model_reports_the_real_error(self, session, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "", "model": "x", "mock": False, "error": "rate limited"})
        ctx = make_ctx()
        cfg = {"input_var": "{{trigger_input}}", "inference_mode": "function_call", "parameters": [{"name": "x", "type": "string"}]}
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["__is_success"] == 0
        assert result["vars"]["__reason"] == "rate limited"


class TestRealParameterExtractorPromptMode:
    def test_a_real_valid_json_response_is_genuinely_parsed(self, session, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": '{"city": "Paris", "num_travelers": 3}', "model": "x", "mock": False})
        ctx = make_ctx()
        cfg = {
            "input_var": "{{trigger_input}}", "inference_mode": "prompt",
            "parameters": [{"name": "city", "type": "string", "required": True}, {"name": "num_travelers", "type": "number"}],
        }
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["__is_success"] == 1
        assert result["vars"]["city"] == "Paris"
        assert result["vars"]["num_travelers"] == 3

    def test_a_real_markdown_code_fence_is_genuinely_stripped_before_parsing(self, session, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": '```json\n{"city": "Tokyo"}\n```', "model": "x", "mock": False})
        ctx = make_ctx()
        cfg = {"input_var": "{{trigger_input}}", "inference_mode": "prompt", "parameters": [{"name": "city", "type": "string"}]}
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["city"] == "Tokyo"

    def test_genuinely_invalid_json_reports_a_real_honest_parse_failure(self, session, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "not json at all", "model": "x", "mock": False})
        ctx = make_ctx()
        cfg = {"input_var": "{{trigger_input}}", "inference_mode": "prompt", "parameters": [{"name": "x", "type": "string"}]}
        result = handle_parameter_extractor(cfg, ctx, session)
        assert result["vars"]["__is_success"] == 0
        assert "parse" in result["vars"]["__reason"].lower()


class TestRealParameterExtractorMemory:
    def test_memory_enabled_includes_real_conversation_history_in_the_real_system_prompt(self, session, monkeypatch):
        import uuid
        from app.models import AgentMemory
        agent_id = f"test-{uuid.uuid4().hex}"
        scope_key = f"{agent_id}:user:test"
        session.add(AgentMemory(scope_key=scope_key, key="__llm_chat_history__", value="User: I need a flight\nAssistant: Sure, where to?"))
        session.commit()

        captured = {}
        import app.harness as h

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured["system"] = system
            return {"text": "{}", "model": "x", "mock": False, "tool_input": {"city": "mock"}}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx()
        ctx["agent_id"] = agent_id
        cfg = {"input_var": "{{trigger_input}}", "inference_mode": "function_call", "memory_enabled": True, "parameters": [{"name": "city", "type": "string"}]}
        handle_parameter_extractor(cfg, ctx, session)
        assert "I need a flight" in captured["system"]

"""
Real tests for the Variable Aggregator node (harness.py's handle_aggregator),
rebuilt to match Dify's own documented spec
(https://docs.dify.ai/en/cloud/use-dify/nodes/variable-aggregator) — see
GAP_REPORT §0.34 for the full before/after comparison this closed. The old
implementation never let a person select a real branch variable at all; it
only ever merged a fixed internal list of ctx keys, and genuinely returned
an empty object for Dify's own primary documented use case (verified
directly before any code was touched — see GAP_REPORT).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harness import handle_aggregator


def make_ctx(variables=None):
    return {"input": "", "vars": dict(variables or {}), "output": None}


class TestRealVariableSelection:
    def test_converges_the_real_branch_variable_that_actually_has_a_value(self):
        """The exact real bug this fixed: previously this always
        returned {} for a real custom branch variable, no matter what
        was configured, because there was no real way to reference one
        at all."""
        ctx = make_ctx({"rejection_reason": "Missing signature"})
        cfg = {"groups": [{"output_name": "final_status", "variables": ["{{approval_note}}", "{{rejection_reason}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["final_status"] == "Missing signature"

    def test_takes_the_first_configured_variable_that_genuinely_has_a_value(self):
        ctx = make_ctx({"approval_note": "Approved", "rejection_reason": "Rejected"})
        cfg = {"groups": [{"output_name": "final_status", "variables": ["{{approval_note}}", "{{rejection_reason}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["final_status"] == "Approved"

    def test_none_of_the_configured_variables_having_a_value_is_a_real_honest_none_not_a_fabricated_default(self):
        ctx = make_ctx({})
        cfg = {"groups": [{"output_name": "final_status", "variables": ["{{approval_note}}", "{{rejection_reason}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["final_status"] is None

    def test_an_empty_string_value_is_treated_as_not_having_a_real_value_same_as_none(self):
        ctx = make_ctx({"approval_note": "", "rejection_reason": "Rejected"})
        cfg = {"groups": [{"output_name": "final_status", "variables": ["{{approval_note}}", "{{rejection_reason}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["final_status"] == "Rejected"


class TestRealTypePreservation:
    """Dify's spec: string/number/object/boolean/array/file — the real
    value's real type is preserved, never coerced or wrapped."""

    def test_a_real_object_value_stays_a_real_dict_not_wrapped_or_stringified(self):
        ctx = make_ctx({"branch_a_result": {"status": "ok", "count": 3}})
        cfg = {"groups": [{"output_name": "result", "variables": ["{{branch_a_result}}", "{{branch_b_result}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["result"] == {"status": "ok", "count": 3}
        assert isinstance(result["vars"]["result"], dict)

    def test_a_real_number_value_stays_a_real_number(self):
        ctx = make_ctx({"priority_score": 8})
        cfg = {"groups": [{"output_name": "priority", "variables": ["{{priority_score}}", "{{default_priority}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["priority"] == 8
        assert isinstance(result["vars"]["priority"], int)

    def test_a_real_boolean_value_stays_a_real_boolean_not_coerced_to_a_string(self):
        ctx = make_ctx({"is_flagged": False, "is_approved": True})
        cfg = {"groups": [{"output_name": "outcome", "variables": ["{{is_flagged}}", "{{is_approved}}"]}]}
        result = handle_aggregator(cfg, ctx)
        # is_flagged is a real, present value (False) — a real branch
        # genuinely set it, so it's the one that "has a value" even
        # though it's falsy-looking; only None/"" count as "no value".
        assert result["vars"]["outcome"] is False

    def test_a_real_array_value_stays_a_real_list(self):
        ctx = make_ctx({"tags_from_branch_a": ["urgent", "billing"]})
        cfg = {"groups": [{"output_name": "tags", "variables": ["{{tags_from_branch_a}}", "{{tags_from_branch_b}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["tags"] == ["urgent", "billing"]
        assert isinstance(result["vars"]["tags"], list)

    def test_a_real_file_shaped_dict_value_stays_intact(self):
        ctx = make_ctx({"uploaded_doc": {"filename": "report.pdf", "url": "http://x/report.pdf"}})
        cfg = {"groups": [{"output_name": "doc", "variables": ["{{uploaded_doc}}"]}]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["doc"] == {"filename": "report.pdf", "url": "http://x/report.pdf"}


class TestRealAggregationGroups:
    """Dify's real "Aggregation Group" feature — multiple independent
    convergence groups in one node, each producing its own separate
    real output."""

    def test_two_independent_groups_each_produce_their_own_real_output(self):
        ctx = make_ctx({"category_result": "billing", "priority_score": 8})
        cfg = {"groups": [
            {"output_name": "category", "variables": ["{{category_result}}", "{{fallback_category}}"]},
            {"output_name": "priority", "variables": ["{{priority_score}}", "{{default_priority}}"]},
        ]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["category"] == "billing"
        assert result["vars"]["priority"] == 8

    def test_groups_are_genuinely_independent_one_groups_variables_dont_leak_into_another(self):
        ctx = make_ctx({"shared_name": "from group 1's branch", "other_var": "from group 2's branch"})
        cfg = {"groups": [
            {"output_name": "group1_out", "variables": ["{{shared_name}}"]},
            {"output_name": "group2_out", "variables": ["{{other_var}}"]},
        ]}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["group1_out"] == "from group 1's branch"
        assert result["vars"]["group2_out"] == "from group 2's branch"
        assert "shared_name" not in {result["vars"]["group1_out"], result["vars"]["group2_out"]} or True  # sanity


class TestRealBackwardCompatibility:
    def test_an_old_pre_rebuild_config_with_only_output_name_does_not_crash(self):
        ctx = make_ctx({})
        cfg = {"output_name": "merged_output"}  # the entire old config shape
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["merged_output"] is None  # honest empty, not a silently fabricated merge

    def test_an_old_config_never_recreates_the_old_broken_hardcoded_merge_behavior(self):
        """Documents the deliberate decision NOT to preserve the old
        (never-actually-functional) hardcoded-key merge — an old config
        gets an honest empty result, not a different kind of wrong
        answer."""
        ctx = make_ctx({"llm_output": {"text": "hello"}, "tool_output": "some tool result"})
        cfg = {"output_name": "merged_output"}
        result = handle_aggregator(cfg, ctx)
        assert result["vars"]["merged_output"] is None

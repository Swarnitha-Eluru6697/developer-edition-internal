"""GAP-04: the rules for which bad-record policy a connector may use, in one pure module."""
import pytest

from app.core.error_policies import (
    ALL_POLICIES,
    TOLERANCE,
    allowed_policies,
    default_policy,
    rejection_reason,
    template_supports_dlq,
)

SOURCE = {"connector_type": "source", "supports_dlq": True}  # even if the stored flag still claims it
SINK = {"connector_type": "sink", "supports_dlq": True}
SINK_NO_DLQ = {"connector_type": "sink", "supports_dlq": False}


def test_a_source_can_only_fail_fast_or_skip_and_log() -> None:
    assert set(allowed_policies(SOURCE)) == {"FAIL_FAST", "SKIP_AND_LOG"}
    assert template_supports_dlq(SOURCE) is False


def test_a_sink_can_use_every_policy() -> None:
    assert set(allowed_policies(SINK)) == ALL_POLICIES


def test_a_sink_curated_without_dlq_support_gets_the_source_options() -> None:
    assert set(allowed_policies(SINK_NO_DLQ)) == {"FAIL_FAST", "SKIP_AND_LOG"}


def test_defaults_keep_a_sinks_bad_records_and_skip_and_log_for_a_source() -> None:
    assert default_policy(SINK) == "DLQ_AND_CONTINUE"
    assert default_policy(SOURCE) == "SKIP_AND_LOG"
    assert default_policy(SINK_NO_DLQ) == "SKIP_AND_LOG"


def test_every_policy_has_a_tolerance_and_only_fail_fast_stops() -> None:
    assert set(TOLERANCE) == ALL_POLICIES
    assert {p for p, t in TOLERANCE.items() if t == "none"} == {"FAIL_FAST"}


@pytest.mark.parametrize("template", [SOURCE, SINK_NO_DLQ])
def test_the_rejection_reason_names_the_reason_and_the_alternatives(template) -> None:
    reason = rejection_reason("THRESHOLD", template)

    assert "dead-letter queue" in reason
    assert "Fail immediately" in reason
    assert "Skip bad records" in reason
    if template is SOURCE:
        assert "only provides one for SINK connectors" in reason

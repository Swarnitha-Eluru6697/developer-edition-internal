"""
GAP-04: PipelineCreate/PipelineUpdate's own field-level validation for
error_handling_policy and its THRESHOLD-only parameters.
"""

import pytest
from pydantic import ValidationError

from app.schemas.pipeline import (
    DEFAULT_ERROR_THRESHOLD_MAX_ERRORS,
    DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS,
    PipelineCreate,
    PipelineUpdate,
)


def _base_kwargs(**overrides) -> dict:
    base = {
        "name": "n", "template_id": "t", "connection_profile_id": "c",
        "zone": "raw", "domain": "d", "source_system": "s", "entity": "e", "mode": "cdc", "version": "v1",
    }
    base.update(overrides)
    return base


def test_no_policy_is_left_unset_here_and_resolved_by_the_connector_at_creation() -> None:
    """The schema can't know whether the connector is a source or a sink, so it doesn't guess: an unset policy stays
    None and validate_pipeline_references resolves it (sink -> DLQ_AND_CONTINUE, source -> SKIP_AND_LOG)."""
    pipeline = PipelineCreate(**_base_kwargs())

    assert pipeline.error_handling_policy is None
    assert pipeline.error_threshold_max_errors is None
    assert pipeline.error_threshold_window_seconds is None


def test_threshold_without_explicit_params_gets_the_documented_defaults() -> None:
    pipeline = PipelineCreate(**_base_kwargs(error_handling_policy="THRESHOLD"))

    assert pipeline.error_threshold_max_errors == DEFAULT_ERROR_THRESHOLD_MAX_ERRORS == 100
    assert pipeline.error_threshold_window_seconds == DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS == 300


def test_threshold_with_explicit_params_keeps_them() -> None:
    pipeline = PipelineCreate(**_base_kwargs(
        error_handling_policy="THRESHOLD", error_threshold_max_errors=25, error_threshold_window_seconds=60,
    ))

    assert pipeline.error_threshold_max_errors == 25
    assert pipeline.error_threshold_window_seconds == 60


def test_fail_fast_and_dlq_and_continue_are_not_defaulted() -> None:
    for policy in ("FAIL_FAST", "DLQ_AND_CONTINUE"):
        pipeline = PipelineCreate(**_base_kwargs(error_handling_policy=policy))
        assert pipeline.error_threshold_max_errors is None
        assert pipeline.error_threshold_window_seconds is None


def test_rejects_an_unknown_policy() -> None:
    with pytest.raises(ValidationError, match="error_handling_policy"):
        PipelineCreate(**_base_kwargs(error_handling_policy="RETRY_FOREVER"))


def test_rejects_a_non_positive_threshold_param() -> None:
    with pytest.raises(ValidationError):
        PipelineCreate(**_base_kwargs(error_handling_policy="THRESHOLD", error_threshold_max_errors=0))


def test_pipeline_update_accepts_a_partial_policy_change() -> None:
    update = PipelineUpdate(error_handling_policy="FAIL_FAST")

    assert update.error_handling_policy == "FAIL_FAST"
    assert "error_threshold_max_errors" not in update.model_dump(exclude_unset=True)


def test_pipeline_update_rejects_an_unknown_policy() -> None:
    with pytest.raises(ValidationError, match="error_handling_policy"):
        PipelineUpdate(error_handling_policy="RETRY_FOREVER")


def test_pipeline_update_omitting_the_policy_field_entirely_is_still_valid() -> None:
    """PATCH semantics — untouched fields simply aren't in model_dump(exclude_unset=True)."""
    update = PipelineUpdate(name="renamed")

    assert "error_handling_policy" not in update.model_dump(exclude_unset=True)


def test_skip_and_log_is_a_valid_policy_and_an_unknown_one_is_still_rejected() -> None:
    assert PipelineCreate(**_base_kwargs(error_handling_policy="SKIP_AND_LOG")).error_handling_policy == "SKIP_AND_LOG"
    with pytest.raises(ValueError, match="error_handling_policy must be one of"):
        PipelineCreate(**_base_kwargs(error_handling_policy="TOLERATE_EVERYTHING"))

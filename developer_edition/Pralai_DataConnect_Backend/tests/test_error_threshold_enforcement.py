"""
GAP-04's THRESHOLD policy has no native Kafka Connect equivalent, so
RuntimeReconciler._enforce_error_threshold is this app's own enforcement:
called once per tick for a running THRESHOLD-policy pipeline (see
runtime_reconciler.py's _reconcile_row), it counts the pipeline's
connector-DLQ topic volume in its configured window and pauses it
(durably, via desired_state) if that exceeds max_errors.

Tested in isolation from the full reconcile tick — count_recent_connector_errors
and record_audit are faked directly rather than standing up a fake Kafka
Connect client + Postgres session just to reach this one method.
"""

import pytest

import app.services.runtime_reconciler as runtime_reconciler_module
from app.services.runtime_reconciler import RuntimeReconciler

PIPELINE = {
    "id": "pipe_1", "domain": "crm", "source_system": "salesforce", "entity": "accounts", "version": "v1",
    "error_handling_policy": "THRESHOLD", "error_threshold_max_errors": 100, "error_threshold_window_seconds": 300,
}


WORKER = "process-a"


def _state_row(fake_postgres, *, desired_state: str = "running", desired_generation: int = 1, leased_by: str = WORKER):
    """The threshold pause is a conditional write against the real runtime-state row (GAP-08), so these tests give it
    a row in the fake database instead of a bare object."""
    fake_postgres.create(
        "pipeline_runtime_states",
        {
            "org_id": "org_1", "pipeline_id": "pipe_1", "connector_name": "c", "desired_state": desired_state,
            "desired_generation": desired_generation, "leased_by": leased_by,
        },
    )
    return next(iter(fake_postgres.pipeline_runtime_states.values()))


async def _noop_record_audit(**kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_pauses_and_audits_when_the_count_exceeds_max_errors(fake_postgres, monkeypatch) -> None:
    row = _state_row(fake_postgres)
    audit_calls = []

    async def _fake_count(topic: str, window_seconds: int) -> int:
        return 150

    async def _fake_audit(**kwargs) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(runtime_reconciler_module, "record_audit", _fake_audit)

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(row, PIPELINE)

    assert row.desired_state == "paused"
    assert row.desired_generation == 2
    assert audit_calls
    assert audit_calls[0]["action"] == "paused_threshold_exceeded"
    assert audit_calls[0]["resource_id"] == "pipe_1"
    assert audit_calls[0]["after_state"]["error_count"] == 150
    assert audit_calls[0]["after_state"]["max_errors"] == 100


@pytest.mark.asyncio
async def test_does_nothing_when_the_count_is_within_bounds(fake_postgres, monkeypatch) -> None:
    row = _state_row(fake_postgres)

    async def _fake_count(topic: str, window_seconds: int) -> int:
        return 5

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _fake_count)

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(row, PIPELINE)

    assert row.desired_state == "running"
    assert row.desired_generation == 1


@pytest.mark.asyncio
async def test_uses_the_default_max_errors_and_window_when_the_pipeline_has_none_set(fake_postgres, monkeypatch) -> None:
    row = _state_row(fake_postgres)
    pipeline = {**PIPELINE, "error_threshold_max_errors": None, "error_threshold_window_seconds": None}
    seen: dict = {}

    async def _fake_count(topic: str, window_seconds: int) -> int:
        seen["window_seconds"] = window_seconds
        return 101  # one over the default max of 100

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(runtime_reconciler_module, "record_audit", _noop_record_audit)

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(row, pipeline)

    assert seen["window_seconds"] == 300
    assert row.desired_state == "paused"


@pytest.mark.asyncio
async def test_never_raises_or_mutates_anything_when_the_kafka_lookup_fails(fake_postgres, monkeypatch) -> None:
    """A count_recent_connector_errors failure (Kafka unreachable) means "we
    couldn't find out", never "assume the worst and pause anyway" — and it
    must never break reconciliation for this pipeline, let alone any other
    pipeline in the same tick (see _reconcile_row's caller)."""
    row = _state_row(fake_postgres)

    async def _raises(topic: str, window_seconds: int) -> int:
        raise TimeoutError("kafka unreachable")

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _raises)

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(row, PIPELINE)  # must not raise

    assert row.desired_state == "running"
    assert row.desired_generation == 1


@pytest.mark.asyncio
async def test_a_user_edit_recorded_during_the_check_wins_over_the_pause(fake_postgres, monkeypatch) -> None:
    """GAP-08: the pause is applied only if nothing newer was recorded since this tick read the row — an absolute write
    of "paused" + generation+1 from the tick's snapshot would erase the user's edit."""
    row = _state_row(fake_postgres, desired_generation=1)
    audit_calls = []

    async def _fake_count(topic: str, window_seconds: int) -> int:
        row.desired_generation = 2  # the user's edit lands while the DLQ volume is being counted
        row.desired_state = "running"
        return 500

    async def _fake_audit(**kwargs) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(runtime_reconciler_module, "record_audit", _fake_audit)
    in_memory = _FakeSnapshot(row)  # what this tick loaded before the edit

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(in_memory, PIPELINE)

    assert row.desired_state == "running"  # the stored intent is untouched
    assert row.desired_generation == 2
    assert in_memory.desired_state == "running"
    assert audit_calls == []  # nothing was paused, so nothing is audited as a pause


@pytest.mark.asyncio
async def test_does_not_pause_a_row_another_process_now_owns(fake_postgres, monkeypatch) -> None:
    row = _state_row(fake_postgres, leased_by="process-b")

    async def _fake_count(topic: str, window_seconds: int) -> int:
        return 500

    monkeypatch.setattr(runtime_reconciler_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(runtime_reconciler_module, "record_audit", _noop_record_audit)

    await RuntimeReconciler(worker_id=WORKER)._enforce_error_threshold(_FakeSnapshot(row), PIPELINE)

    assert row.desired_state == "running"
    assert row.desired_generation == 1


class _FakeSnapshot:
    """The in-memory copy a tick loaded earlier — a separate object from the stored row, as in production."""

    def __init__(self, stored) -> None:
        self.id = stored.id
        self.org_id = stored.org_id
        self.desired_state = stored.desired_state
        self.desired_generation = stored.desired_generation

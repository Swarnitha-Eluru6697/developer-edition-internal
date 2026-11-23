"""
FE-BUG-07: last_reconciled_at updates every tick a row is claimed, whether
or not that tick actually got a real answer from Kafka Connect —
last_observed_at is the distinct field that only advances on a genuine
observation (see PipelineRuntimeState's own docstring). These tests prove
that distinction actually holds inside _reconcile_row, using the same
hand-rolled fake harness as test_runtime_reconciler_convergence.py (GAP-09)
— _reconcile_row isn't exercised by any FakePostgres-backed fixture (its
generic _FakeSession.scalar() can't represent a live desired_generation).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.runtime_reconciler as reconciler_module
from app.services import pipeline_service as pipeline_service_module

PIPELINE = {
    "id": "pipe-1", "org_id": "org-1", "domain": "sales", "source_system": "pg",
    "entity": "orders", "version": "v1", "status": "running",
    "error_handling_policy": "DLQ_AND_CONTINUE",
}


class _FakeSession:
    def __init__(self, live_desired_generation: int):
        self.live_desired_generation = live_desired_generation

    async def scalar(self, *args, **kwargs):
        return self.live_desired_generation

    async def flush(self) -> None:
        pass


class _FakeRepo:
    def __init__(self, session=None):
        self.session = session

    async def delete(self, row) -> None:
        pass

    async def fence(self, row_id, worker_id, *, lock=False):
        # GAP-08: (live desired_generation, still the lease holder) — the session double carries the "live" value.
        return self.session.live_desired_generation, True


def _row(*, last_observed_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        id="row-1", org_id="org-1", pipeline_id="pipe-1", connector_name="conn-1",
        desired_state="running", desired_generation=1,
        config_hash="hash-1", observed_state="running", observed_generation=1,
        last_reconciled_at=None, last_observed_at=last_observed_at, last_runtime_error=None,
    )


def _install_common_fakes(monkeypatch) -> None:
    async def _fake_get_pipeline(org_id, pipeline_id):
        return dict(PIPELINE)

    async def _fake_set_status(org_id, pipeline_id, status):
        pass

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _fake_get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", _fake_set_status)

    async def _fake_resolve_config(pipeline):
        return {"connector.class": "x"}, "hash-1"

    monkeypatch.setattr(reconciler_module, "_resolve_config", _fake_resolve_config)


@pytest.mark.asyncio
async def test_a_successful_tick_advances_both_last_reconciled_at_and_last_observed_at(monkeypatch) -> None:
    _install_common_fakes(monkeypatch)

    async def _fake_get_status_retryable(client, connector_name):
        return {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _fake_get_status_retryable)

    row = _row(last_observed_at=None)
    reconciler = reconciler_module.RuntimeReconciler()

    await reconciler._reconcile_row(
        _session := _FakeSession(live_desired_generation=1), _FakeRepo(_session), client=None, existing={"conn-1"}, row=row,
    )

    assert row.last_reconciled_at is not None
    assert row.last_observed_at == row.last_reconciled_at
    assert row.last_runtime_error is None


@pytest.mark.asyncio
async def test_a_tick_that_cannot_reach_connect_for_this_connector_advances_last_reconciled_at_but_not_last_observed_at(
    monkeypatch,
) -> None:
    """The connector list call (reconcile_once, not exercised here) said
    this connector exists, but the per-connector status call itself fails —
    a real, if partial, "Kafka Connect can't be reached" scenario. The tick
    still ran (last_reconciled_at moves), but nothing genuine was learned
    about this pipeline's actual state (last_observed_at must NOT move)."""
    _install_common_fakes(monkeypatch)

    async def _fake_get_status_retryable_fails(client, connector_name):
        raise ConnectionError("Kafka Connect unreachable")

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _fake_get_status_retryable_fails)

    stale_observation = datetime(2020, 1, 1, tzinfo=UTC)
    row = _row(last_observed_at=stale_observation)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(
        _session := _FakeSession(live_desired_generation=1), _FakeRepo(_session), client=None, existing={"conn-1"}, row=row,
    )

    assert row.last_reconciled_at is not None  # the tick did run
    assert row.last_observed_at == stale_observation  # but learned nothing new — untouched
    assert "Kafka Connect unreachable" not in row.last_runtime_error  # raw text stays out of the stored error
    assert "KAFKA_CONNECT_UNAVAILABLE" in row.last_runtime_error
    assert result["observed_state"] == "unknown"


@pytest.mark.asyncio
async def test_connector_genuinely_absent_still_counts_as_a_real_observation(monkeypatch) -> None:
    """connector_exists=False is itself a real answer (the connector list
    call succeeded and said "not there") — last_observed_at must still
    advance even though there's no per-connector status call to make at all."""
    _install_common_fakes(monkeypatch)

    async def _fake_get_status_retryable_never_called(client, connector_name):
        raise AssertionError("must not be called when the connector doesn't exist")

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _fake_get_status_retryable_never_called)

    async def _fake_deploy_if_current(client, connector_name, config, *, still_current):
        return None  # not current -> skip, doesn't matter for this test

    monkeypatch.setattr(reconciler_module, "deploy_if_current", _fake_deploy_if_current)

    row = _row(last_observed_at=None)
    reconciler = reconciler_module.RuntimeReconciler()

    await reconciler._reconcile_row(
        _session := _FakeSession(live_desired_generation=1), _FakeRepo(_session), client=None, existing=set(), row=row,
    )

    assert row.last_observed_at is not None
    assert row.last_observed_at == row.last_reconciled_at

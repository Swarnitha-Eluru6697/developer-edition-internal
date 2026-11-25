"""
Hardening: Kafka Connect outages, across MANY reconciler ticks.

tests/test_runtime_reconciler_last_observed_at.py shows one tick that cannot reach Connect. An outage is a sequence, so this plays one through:
healthy -> down for several ticks -> back, and checks what an operator depends on:

  * an established pipeline's status does NOT flap to "unknown" because Connect is briefly unreachable;
  * nothing is acted on while Connect cannot be asked (no pause/resume/restart/deploy is fired blind);
  * what is stored and shown is a SAFE error — never the raw exception text (a hostname, a password in the driver's message) — and a condition
    that persists is logged ONCE, not once per pipeline per tick;
  * "last observed" stays at the last time Connect really answered, while "last reconciled" keeps moving;
  * on recovery the pipeline converges, the error clears, and the action that was due is issued exactly once;
  * the readiness probe says so (503, which pools) without leaking infrastructure detail, and goes back to 200.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import app.services.runtime_reconciler as reconciler_module
from app.core.config import settings
from app.main import app
from app.services import pipeline_service as pipeline_service_module

RAW = "HTTPConnectionPool(host='connect-cdc.internal', port=8083): refused password=hunter2"
PIPELINE = {"id": "pipe-1", "org_id": "org-1", "domain": "d", "source_system": "s", "entity": "e", "version": "v1",
            "status": "running", "error_handling_policy": "DLQ_AND_CONTINUE"}


class Connect:
    """A Kafka Connect that can be taken down and brought back, and that records every action fired at it."""

    def __init__(self) -> None:
        self.up = True
        self.state = "RUNNING"
        self.actions: list[str] = []          # actions that SUCCEEDED
        self.attempts: list[str] = []         # every action even tried — including ones that failed because Connect was down

    async def status(self, client, name):
        if not self.up:
            raise ConnectionError(RAW)
        return {"connector": {"state": self.state}, "tasks": [{"state": self.state}]}

    def action(self, name, new_state=None):
        async def run(client, connector):
            self.attempts.append(name)
            if not self.up:
                raise ConnectionError(RAW)
            self.actions.append(name)
            if new_state:
                self.state = new_state
        return run


class _Session:
    async def scalar(self, *a, **k):
        return 1

    async def flush(self) -> None:
        pass


class _Repo:
    def __init__(self, session):
        self.session = session

    async def delete(self, row) -> None:
        pass

    async def fence(self, row_id, worker_id, *, lock=False):
        return 1, True


def _row(desired="running"):
    return SimpleNamespace(
        id="row-1", org_id="org-1", pipeline_id="pipe-1", connector_name="conn-1", desired_state=desired, desired_generation=1, config_hash="hash-1",
        observed_state="running", observed_generation=1, last_reconciled_at=None,
        last_observed_at=datetime(2026, 1, 1, tzinfo=UTC), last_runtime_error=None,
        quality_blocked_since=None, quality_blocked_heartbeat_at=None,
    )


@pytest.fixture
def connect(monkeypatch):
    fake = Connect()
    statuses: list[str] = []

    async def get_pipeline(org_id, pipeline_id):
        return dict(PIPELINE)

    async def set_status(org_id, pipeline_id, status):
        statuses.append(status)

    async def resolve(pipeline):
        return {"connector.class": "x"}, "hash-1"

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", set_status)
    monkeypatch.setattr(reconciler_module, "_resolve_config", resolve)
    monkeypatch.setattr(reconciler_module, "get_status_retryable", fake.status)
    monkeypatch.setattr(reconciler_module, "pause_if_needed", fake.action("pause", "PAUSED"))
    monkeypatch.setattr(reconciler_module, "resume_if_needed", fake.action("resume", "RUNNING"))
    monkeypatch.setattr(reconciler_module, "restart_once", fake.action("restart", "RUNNING"))
    fake.statuses = statuses
    return fake


async def _tick(reconciler, row):
    session = _Session()
    return await reconciler._reconcile_row(session, _Repo(session), client=None, existing={"conn-1"}, row=row)


# ── an outage across several ticks ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_running_pipeline_rides_out_a_connect_outage_without_flapping_or_acting_blind(connect) -> None:
    reconciler = reconciler_module.RuntimeReconciler()
    row = _row()
    await _tick(reconciler, row)                                           # healthy: observed for real
    observed_at = row.last_observed_at
    assert (row.observed_state, row.last_runtime_error) == ("running", None)

    connect.up = False
    errors = []
    reconciled = []
    for _ in range(5):                                                     # five ticks of outage
        await _tick(reconciler, row)
        errors.append(row.last_runtime_error)
        reconciled.append(row.last_reconciled_at)

    assert connect.statuses == [] or "unknown" not in connect.statuses     # the pipeline's status never flapped to "unknown"
    assert connect.actions == []
    assert connect.attempts == []                                          # nothing was even TRIED against a Connect that could not be asked
    assert row.last_observed_at == observed_at                             # "last observed" = the last time Connect really answered
    assert reconciled == sorted(reconciled)
    assert reconciled[-1] > reconciled[0]                                  # ... while "last reconciled" kept moving
    assert row.observed_generation == 1                                    # and convergence was never claimed
    assert all(e is not None for e in errors)
    assert len(set(errors)) == 1                                           # the SAME safe text every tick (a stable reference), not a new one each time
    for leaked in ("connect-cdc.internal", "hunter2", "8083", "HTTPConnectionPool"):
        assert leaked not in errors[0], f"{leaked!r} leaked into the stored runtime error"

    connect.up = True
    await _tick(reconciler, row)                                           # recovery
    assert (row.observed_state, row.last_runtime_error) == ("running", None)
    assert row.last_observed_at > observed_at
    assert connect.actions == []                                           # a healthy, already-correct pipeline needs no action


@pytest.mark.asyncio
async def test_a_persistent_outage_is_logged_once_not_once_per_tick(connect) -> None:
    reconciler = reconciler_module.RuntimeReconciler()
    row = _row()
    connect.up = False

    with capture_logs() as logs:
        for _ in range(6):
            await _tick(reconciler, row)

    runtime_errors = [entry for entry in logs if entry["event"] == "reconciler.runtime_error"]
    assert len(runtime_errors) == 1
    # capture_logs sees the entry BEFORE the production processor chain (format_exc_info, then redact_error_text — app/core/logging.py);
    # run the same two steps the logger runs.
    import structlog

    from app.core.logging import redact_error_text

    rendered = redact_error_text(None, "error", structlog.processors.format_exc_info(None, "error", dict(runtime_errors[0])))
    assert "hunter2" not in str(rendered)                                  # the detailed entry is redacted too


@pytest.mark.asyncio
async def test_the_pause_due_during_an_outage_is_issued_once_on_recovery_and_the_pipeline_converges(connect) -> None:
    reconciler = reconciler_module.RuntimeReconciler()
    row = _row(desired="paused")                                           # the user paused it; Connect is unreachable when the tick runs
    connect.up = False
    for _ in range(3):
        await _tick(reconciler, row)
    assert connect.actions == []
    assert connect.attempts == []                                          # not fired blind, not even attempted
    assert row.desired_state == "paused"                                   # and the intent is not lost

    connect.up = True
    await _tick(reconciler, row)

    assert connect.actions == ["pause"]                                    # exactly one, now that Connect can be asked
    assert row.observed_state == "paused"
    assert row.last_runtime_error is None
    assert row.observed_generation == row.desired_generation               # converged
    await _tick(reconciler, row)
    assert connect.actions == ["pause"]                                    # and it does not keep re-pausing


@pytest.mark.asyncio
async def test_an_action_that_fails_because_connect_went_down_mid_tick_is_retried_after_recovery(connect) -> None:
    reconciler = reconciler_module.RuntimeReconciler()
    row = _row(desired="paused")
    real_status = connect.status

    async def status_up_then_pause_fails(client, name):
        return await real_status(client, name)

    connect.up = True
    original_pause = reconciler_module.pause_if_needed

    async def pause_fails_once(client, connector):
        reconciler_module.pause_if_needed = original_pause                 # only the first attempt fails
        raise ConnectionError(RAW)

    reconciler_module.pause_if_needed = pause_fails_once
    try:
        await _tick(reconciler, row)
    finally:
        reconciler_module.pause_if_needed = original_pause

    assert row.last_runtime_error is not None
    assert "hunter2" not in row.last_runtime_error
    assert connect.actions == []                                           # the failed attempt changed nothing

    await _tick(reconciler, row)                                           # next tick: Connect answers
    assert connect.actions == ["pause"]
    assert (row.observed_state, row.last_runtime_error) == ("paused", None)


# ── the readiness probe ────────────────────────────────────────────────────────────────────────────────────────────

client = TestClient(app)


def test_readiness_goes_503_naming_the_unreachable_pools_without_leaking_infrastructure_and_recovers(monkeypatch) -> None:
    import app.api.routes.health as health_routes
    import app.services.runtime_orchestrator as orchestrator

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def postgres_ok():
        return {"ok": True}

    async def secret_store_ok():
        return {"ok": True, "backend": "postgres"}

    monkeypatch.setattr(health_routes, "_check_postgres", postgres_ok)
    monkeypatch.setattr(health_routes, "_check_secret_store", secret_store_ok)
    state = {"up": False}

    async def list_connectors(self, *a, **k):
        if not state["up"]:
            raise ConnectionError(RAW)
        return []

    monkeypatch.setattr(orchestrator.KafkaConnectClient, "list_connectors", list_connectors)

    down = client.get("/api/v1/dataconnect/health/ready")
    assert down.status_code == 503
    body = down.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["kafka_connect"]["ok"] is False
    assert body["checks"]["kafka_connect"].get("unreachable_pools")        # WHICH pools, so an operator knows what to look at
    for leaked in ("connect-cdc.internal", "hunter2", "8083", "http://"):
        assert leaked not in down.text

    state["up"] = True
    up = client.get("/api/v1/dataconnect/health/ready")
    assert up.status_code == 200
    assert up.json()["checks"]["kafka_connect"]["ok"] is True


@pytest.mark.asyncio
async def test_the_diagnostics_bundle_reports_the_outage_without_cluster_urls_or_error_text(monkeypatch) -> None:
    import json

    import app.services.runtime_orchestrator as orchestrator
    from app.services import diagnostics

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def list_connectors(self, *a, **k):
        raise ConnectionError(RAW)

    async def noop(*a, **k):
        return {"ok": True}

    monkeypatch.setattr(orchestrator.KafkaConnectClient, "list_connectors", list_connectors)
    import app.api.routes.health as health_routes

    monkeypatch.setattr(health_routes, "_check_postgres", noop)
    monkeypatch.setattr(health_routes, "_check_secret_store", noop)

    readiness = await diagnostics._readiness(None)

    assert readiness["kafka_connect"]["ok"] is False
    assert readiness["kafka_connect"]["error_class"] == "ConnectionError"
    assert any(c["pools"] for c in readiness["kafka_connect"]["clusters"].values())
    text = json.dumps(readiness)
    for leaked in ("connect-cdc.internal", "hunter2", "8083"):
        assert leaked not in text

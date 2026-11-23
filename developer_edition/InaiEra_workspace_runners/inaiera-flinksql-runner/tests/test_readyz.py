"""Readiness reflects every ENABLED dependency (RUN-FLINK-010 + Helm
readiness hardening), not just Postgres: Flink Gateway/JobManager, the
background loops, and — when the quality consumer is enabled — its Kafka
connection/poll progress and UCS reachability (503 when enabled but not
working)."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import main
from app.core.config import Settings
from app.services import quality_consumer as qc


def _settings(**overrides) -> Settings:
    base = {"kafka_bootstrap_servers": "localhost:9092", "kafka_security_protocol": "PLAINTEXT",
            "readiness_check_flink": False, "background_tasks_enabled": False}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _reset():
    qc.reset_state()
    yield
    qc.reset_state()
    main.BACKGROUND_TASKS.clear()


def _use(monkeypatch, settings):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)


def _probe_results(monkeypatch, results: dict):
    async def _fake_probe(url, timeout):
        for frag, res in results.items():
            if frag in url:
                return res
        return None
    monkeypatch.setattr(main, "_probe", _fake_probe)


def test_ready_when_consumer_disabled_and_db_ok(client, monkeypatch):
    _use(monkeypatch, _settings())
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ucs_quality"] == "disabled"


def test_not_ready_when_enabled_without_ucs_token(client, monkeypatch):
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs.internal",
                                ucs_internal_token=None))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert "UCS_URL" in body["ucs_quality"] and "UCS_INTERNAL_TOKEN" in body["ucs_quality"]


def test_degraded_not_unready_when_the_quality_consumer_cannot_reach_kafka(client, monkeypatch):
    """FL-009: a secondary integration's outage must not take every API pod out of service."""
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs",
                                ucs_internal_token="t", ucs_quality_runs_enabled=True))
    _probe_results(monkeypatch, {})
    qc.CONSUMER_STATE.update(status="backoff", kafka_connected=False, last_error="NoBrokersAvailable broker-7.internal:9092")
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded" and body["degraded"] == ["quality_consumer"]
    assert body["quality_consumer"]["kafka_connected"] is False
    assert "broker-7.internal" not in resp.text                  # raw client text stays in the logs


def test_degraded_not_unready_when_ucs_health_fails(client, monkeypatch):
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs",
                                ucs_internal_token="t", ucs_quality_runs_enabled=True))
    _probe_results(monkeypatch, {"ucs": "unreachable"})
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True,
                             last_poll_at=time.monotonic(), ucs_reachable=True)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded" and resp.json()["quality_consumer"]["ucs_health"] == "unreachable"


def test_a_crashed_quality_consumer_task_is_degraded_not_unready(client, monkeypatch):
    _use(monkeypatch, _settings(quality_consumer_enabled=True, background_tasks_enabled=True, ucs_url="http://ucs",
                                ucs_internal_token="t", ucs_quality_runs_enabled=True))
    _probe_results(monkeypatch, {})
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True, last_poll_at=time.monotonic(), ucs_reachable=True)
    loop = asyncio.new_event_loop()

    async def _boom():
        raise RuntimeError("secret-host.internal exploded")

    async def _alive():
        await asyncio.sleep(3600)
    try:
        crashed = loop.create_task(_boom())
        loop.run_until_complete(asyncio.sleep(0))
        main.BACKGROUND_TASKS["quality_consumer"] = crashed
        main.BACKGROUND_TASKS["metrics_poller"] = loop.create_task(_alive())
        main.BACKGROUND_TASKS["deployment_reconciler"] = loop.create_task(_alive())
        resp = client.get("/readyz")
    finally:
        for t in main.BACKGROUND_TASKS.values():
            t.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
    assert resp.status_code == 200 and resp.json()["degraded"] == ["quality_consumer"]
    assert resp.json()["quality_consumer"]["task"] == "crashed" and "secret-host" not in resp.text


def test_legacy_ucs_delivery_is_reported_as_degraded(client, monkeypatch):
    """FL-007: legacy mode drops lifecycle/gate events for UCS, so it must be visible, not a silent default."""
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t",
                                ucs_quality_runs_enabled=False))
    _probe_results(monkeypatch, {})
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True, last_poll_at=time.monotonic(), ucs_reachable=True)
    resp = client.get("/readyz")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "degraded"
    assert body["degraded"] == ["ucs_quality_delivery"]
    assert body["ucs_quality_delivery"]["mode"] == "legacy" and body["ucs_quality_delivery"]["degraded"] is True


def test_v2_ucs_delivery_is_not_degraded(client, monkeypatch):
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t",
                                ucs_quality_runs_enabled=True))
    _probe_results(monkeypatch, {})
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True, last_poll_at=time.monotonic(), ucs_reachable=True)
    body = client.get("/readyz").json()
    assert body["status"] == "ok" and body["degraded"] == [] and body["ucs_quality_delivery"]["mode"] == "v2"


def test_a_critical_dependency_still_makes_the_pod_unready_with_a_generic_reason(client, monkeypatch):
    _use(monkeypatch, _settings(readiness_check_flink=True))
    _probe_results(monkeypatch, {"v1/info": "unreachable"})
    resp = client.get("/readyz")
    assert resp.status_code == 503 and resp.json()["flink_gateway"] == "unreachable"


def test_database_errors_are_not_echoed(client, monkeypatch):
    _use(monkeypatch, _settings())

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("connection to server at db-primary.prod.internal (10.0.3.4) failed")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _Boom())
    resp = client.get("/readyz")
    assert resp.status_code == 503 and resp.json()["db"] == "error"
    assert "db-primary" not in resp.text and "10.0.3.4" not in resp.text


def test_ready_when_enabled_and_everything_works(client, monkeypatch):
    _use(monkeypatch, _settings(quality_consumer_enabled=True, ucs_url="http://ucs",
                                ucs_internal_token="t"))
    _probe_results(monkeypatch, {})
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True,
                             last_poll_at=time.monotonic(), ucs_reachable=True)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ucs_quality"] == "enabled"


def test_not_ready_when_flink_gateway_unreachable(client, monkeypatch):
    _use(monkeypatch, _settings(readiness_check_flink=True))
    _probe_results(monkeypatch, {"8083": "unreachable: connection refused"})
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["flink_gateway"].startswith("unreachable")
    assert body["flink_jobmanager"] == "ok"


def test_not_ready_when_a_background_loop_has_crashed(client, monkeypatch):
    _use(monkeypatch, _settings(background_tasks_enabled=True))
    loop = asyncio.new_event_loop()
    try:
        async def _boom():
            raise RuntimeError("poller died")

        async def _forever():
            await asyncio.sleep(3600)

        crashed = loop.create_task(_boom())
        loop.run_until_complete(asyncio.sleep(0))
        alive = loop.create_task(_forever())
        main.BACKGROUND_TASKS.update(metrics_poller=crashed, deployment_reconciler=alive)
        resp = client.get("/readyz")
        alive.cancel()
        loop.run_until_complete(asyncio.gather(alive, crashed, return_exceptions=True))
    finally:
        loop.close()
    assert resp.status_code == 503
    tasks = resp.json()["background_tasks"]
    assert tasks["metrics_poller"].startswith("crashed")
    assert tasks["deployment_reconciler"] == "ok"

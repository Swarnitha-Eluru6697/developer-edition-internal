"""RUN-FLINK-010 — quality consumer enablement, startup gates, retry and
readiness state.

  * It runs only when QUALITY_CONSUMER_ENABLED is true (never inferred).
  * Enabled without UCS_URL/UCS_INTERNAL_TOKEN = misconfigured (never mints
    tokens with an empty key) and consumer_health() reports unhealthy.
  * A Kafka connect failure is retried with exponential backoff — the old
    loop returned forever after the first failure.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.services import quality_consumer as qc
from app.services.quality_consumer import consumer_health, quality_consumer_loop


def _settings(**overrides) -> Settings:
    base = {"kafka_bootstrap_servers": "localhost:9092", "kafka_security_protocol": "PLAINTEXT",
            "quality_consumer_retry_backoff_seconds": 0.01,
            "quality_consumer_max_backoff_seconds": 0.05,
            "quality_consumer_flush_interval_seconds": 0.02}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _reset_state():
    qc.reset_state()
    yield
    qc.reset_state()


def _must_not_connect(settings):
    raise AssertionError("must not attempt to create a Kafka consumer")


async def test_loop_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(qc, "get_settings",
                        lambda: _settings(ucs_url="http://ucs", ucs_internal_token="t"))
    monkeypatch.setattr(qc, "_make_consumer", _must_not_connect)
    await quality_consumer_loop()
    assert qc.CONSUMER_STATE["status"] == "disabled"
    assert consumer_health(_settings())[0] is True


async def test_enabled_without_ucs_token_is_misconfigured_and_unhealthy(monkeypatch):
    s = _settings(quality_consumer_enabled=True, ucs_url="http://ucs.internal", ucs_internal_token=None)
    monkeypatch.setattr(qc, "get_settings", lambda: s)
    monkeypatch.setattr(qc, "_make_consumer", _must_not_connect)
    await quality_consumer_loop()
    assert qc.CONSUMER_STATE["status"] == "misconfigured"
    ok, detail = consumer_health(s)
    assert ok is False and detail["status"] == "misconfigured"


async def test_kafka_connect_failure_is_retried_with_backoff_not_abandoned(monkeypatch):
    s = _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t")
    monkeypatch.setattr(qc, "get_settings", lambda: s)
    attempts = []
    fake_consumer = MagicMock()
    fake_consumer.poll.return_value = {}

    def _flaky(settings):
        attempts.append(time.monotonic())
        if len(attempts) < 3:
            raise RuntimeError("NoBrokersAvailable")
        return fake_consumer

    async def _drain(client, settings):
        return (0, 0)

    monkeypatch.setattr(qc, "_make_consumer", _flaky)
    monkeypatch.setattr(qc, "_drain_outbox", _drain)

    task = asyncio.create_task(quality_consumer_loop())
    for _ in range(100):
        if qc.CONSUMER_STATE["status"] == "running" and qc.CONSUMER_STATE["last_poll_at"]:
            break
        await asyncio.sleep(0.02)
    assert len(attempts) == 3
    assert attempts[1] - attempts[0] >= 0.009     # first backoff
    assert attempts[2] - attempts[1] >= 0.019     # doubled
    assert consumer_health(s)[0] is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert qc.CONSUMER_STATE["status"] == "stopped"


async def test_health_is_unhealthy_while_kafka_is_down(monkeypatch):
    s = _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t")
    monkeypatch.setattr(qc, "get_settings", lambda: s)

    def _down(settings):
        raise RuntimeError("NoBrokersAvailable")

    monkeypatch.setattr(qc, "_make_consumer", _down)
    task = asyncio.create_task(quality_consumer_loop())
    await asyncio.sleep(0.1)
    ok, detail = consumer_health(s)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ok is False
    assert detail["kafka_connected"] is False
    assert "NoBrokersAvailable" in detail["last_error"]


def test_health_is_unhealthy_when_ucs_is_unreachable():
    s = _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t")
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True,
                             last_poll_at=time.monotonic(), ucs_reachable=False)
    assert consumer_health(s)[0] is False
    qc.CONSUMER_STATE.update(ucs_reachable=True)
    assert consumer_health(s)[0] is True


def test_health_is_unhealthy_when_polling_stalls():
    s = _settings(quality_consumer_enabled=True, ucs_url="http://ucs", ucs_internal_token="t",
                  quality_consumer_stale_after_seconds=1)
    qc.CONSUMER_STATE.update(status="running", kafka_connected=True,
                             last_poll_at=time.monotonic() - 5, ucs_reachable=True)
    assert consumer_health(s)[0] is False

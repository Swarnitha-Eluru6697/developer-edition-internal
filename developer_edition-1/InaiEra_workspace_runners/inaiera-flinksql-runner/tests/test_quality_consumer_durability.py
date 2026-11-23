"""DQ-RESULT-009 — the quality consumer must never lose a result: it must
commit Kafka offsets only after durably writing to the outbox, and a
failed UCS delivery must leave the record retryable, never dropped.

These test the LOOP'S ORCHESTRATION (call ordering, offset-commit gating,
idempotency-key derivation) with the Kafka consumer and the outbox
read/write functions mocked — no real Kafka or Postgres involved. The
outbox table's own SQL (uniqueness/ON CONFLICT/status transitions) is
Postgres-specific (JSONB, `ON CONFLICT DO NOTHING`) and is verified
separately against a real Postgres instance, not here — this repo's
default test DB is SQLite (see tests/conftest.py), which can't compile
that SQL, the same reason other DB-touching modules in this codebase are
also only unit-tested with mocks.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services import quality_consumer as qc


class _TP:
    def __init__(self, topic: str, partition: int):
        self.topic = topic
        self.partition = partition


class _Msg:
    def __init__(self, value, offset: int):
        self.value = value
        self.offset = offset


def _fake_settings(**overrides):
    from app.core.config import get_settings
    real = get_settings()
    base = {
        "ucs_url": "http://ucs.internal", "ucs_internal_token": "t",
        "org_id": real.org_id, "quality_contract_version": real.quality_contract_version,
        "quality_results_topic": real.quality_results_topic,
        "quality_gate_topic": real.quality_gate_topic,
        "quality_check_stale_after_seconds": 300.0,
        "quality_consumer_enabled": True, "quality_consumer_batch_size": 10,
        "quality_consumer_flush_interval_seconds": 0.02,
        "quality_consumer_retry_backoff_seconds": 0.01,
        "quality_consumer_max_backoff_seconds": 0.02,
    }
    base.update(overrides)
    return MagicMock(**base)


def test_make_consumer_disables_auto_commit():
    """DQ-RESULT-009: offsets must be committed explicitly, never on a
    timer independent of durable acceptance."""
    settings = MagicMock(
        kafka_bootstrap_servers="localhost:9092",
        kafka_security_protocol="PLAINTEXT",
        quality_results_topic="dq.quality.results.v2", quality_gate_topic="dq.quality.gate.v2",
        quality_consumer_group_id="flinksql-quality",
        quality_consumer_flush_interval_seconds=5,
    )
    with patch.object(qc, "KafkaConsumer") as mock_consumer_cls:
        mock_consumer_cls.return_value = MagicMock()
        qc._make_consumer(settings)
    assert mock_consumer_cls.call_args.kwargs["enable_auto_commit"] is False


async def _run_loop_briefly(fake_consumer, write_outbox_mock, drain_outbox_mock, settings=None):
    async def _no_sweep(settings):
        return 0

    with patch.object(qc, "_make_consumer", return_value=fake_consumer), \
         patch.object(qc, "_write_outbox", side_effect=write_outbox_mock), \
         patch.object(qc, "_drain_outbox", side_effect=drain_outbox_mock), \
         patch.object(qc, "_sweep_stale", side_effect=_no_sweep), \
         patch.object(qc, "get_settings", return_value=settings or _fake_settings()):
        task = asyncio.create_task(qc.quality_consumer_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_offset_is_committed_only_after_the_outbox_write_succeeds():
    from tests.quality_records import window_result
    calls: list = []
    poll_results = [{_TP("dq.quality.results.v2", 0): [_Msg(window_result(), offset=42)]}]

    fake_consumer = MagicMock()
    fake_consumer.poll.side_effect = lambda **kw: (poll_results.pop(0) if poll_results else {})
    fake_consumer.commit.side_effect = lambda: calls.append("commit")

    async def write_outbox(entries):
        calls.append(("write", entries))

    async def drain_outbox(client, settings):
        calls.append("drain")
        return (0, 0)

    await _run_loop_briefly(fake_consumer, write_outbox, drain_outbox)

    writes = [c for c in calls if isinstance(c, tuple) and c[0] == "write"]
    assert len(writes) == 1, "the polled record must be written to the outbox exactly once"
    (entry,) = writes[0][1]
    # Semantic idempotency key (aggregation key), not topic-partition-offset.
    assert entry.kind == "quality_run" and entry.key.startswith("qrun:")
    assert entry.payload["status"] == "completed_fail"
    assert entry.payload["evaluated_rows"] == 100 and entry.payload["violation_count"] == 3
    assert calls.index(writes[0]) < calls.index("commit")


@pytest.mark.asyncio
async def test_invalid_record_is_dead_lettered_before_commit_not_dropped():
    calls: list = []
    poll_results = [{_TP("dq.quality.results.v2", 0): [_Msg({"rule_name": "old-shape"}, offset=5)]}]
    fake_consumer = MagicMock()
    fake_consumer.poll.side_effect = lambda **kw: (poll_results.pop(0) if poll_results else {})
    fake_consumer.commit.side_effect = lambda: calls.append("commit")

    async def write_outbox(entries):
        calls.append(("write", entries))

    async def drain_outbox(client, settings):
        return (0, 0)

    await _run_loop_briefly(fake_consumer, write_outbox, drain_outbox)
    (entry,) = next(c for c in calls if isinstance(c, tuple))[1]
    assert entry.kind == "dead_letter" and entry.status == "rejected"
    assert calls.index(next(c for c in calls if isinstance(c, tuple))) < calls.index("commit")


@pytest.mark.asyncio
async def test_offset_is_never_committed_when_the_outbox_write_fails():
    """The core data-loss fix: if durable acceptance fails, the Kafka
    offset must NOT advance. RUN-FLINK-010: the loop no longer dies on it
    either — it reconnects with backoff and the uncommitted record is
    simply re-polled and re-attempted."""
    calls: list = []
    from tests.quality_records import window_result
    record = window_result()
    fake_consumer = MagicMock()
    # Never committed -> Kafka hands the same record back on every poll.
    fake_consumer.poll.side_effect = lambda **kw: {_TP("dq.quality.results.v2", 0): [_Msg(record, offset=1)]}
    fake_consumer.commit.side_effect = lambda: calls.append("commit")
    write_attempts = []

    async def failing_write_outbox(records):
        write_attempts.append(records)
        raise RuntimeError("database unreachable")

    async def drain_outbox(client, settings):
        return (0, 0)

    qc.reset_state()
    await _run_loop_briefly(fake_consumer, failing_write_outbox, drain_outbox)

    assert "commit" not in calls
    assert len(write_attempts) >= 2          # retried, not abandoned
    assert len({r[0].key for r in write_attempts}) == 1     # same semantic key every retry
    assert "database unreachable" in (qc.CONSUMER_STATE["last_error"] or "")


@pytest.mark.asyncio
async def test_shutdown_drains_the_outbox_instead_of_flushing_an_in_memory_batch():
    """On CancelledError there is no in-memory batch left to lose — the
    old bug's exact failure mode — so shutdown just drains the outbox
    once more."""
    calls: list = []
    fake_consumer = MagicMock()
    fake_consumer.poll.side_effect = lambda **kw: {}

    async def write_outbox(records):
        pass

    async def drain_outbox(client, settings):
        calls.append("drain")
        return (0, 0)

    await _run_loop_briefly(fake_consumer, write_outbox, drain_outbox)
    assert "drain" in calls

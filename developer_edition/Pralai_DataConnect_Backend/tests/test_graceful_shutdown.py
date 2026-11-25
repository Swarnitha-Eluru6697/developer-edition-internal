"""
Hardening: graceful shutdown of Kafka clients, workers and HTTP clients.

Guarantees under test:
  * every teardown step runs, in dependency order (users of a client stop before the client closes; the database engine is last);
  * a step that FAILS does not skip the ones after it, and one that HANGS is abandoned after a deadline instead of blocking shutdown forever;
  * each closer is itself robust — one HTTP client failing to close does not leak the others, a Kafka producer waits a bounded time for an
    unreachable broker, a Kafka consumer is not closed underneath its own still-running poll thread, and one worker failing to stop does not
    leave its siblings consuming.
"""

import asyncio
import threading
import time

import pytest

from app.core.config import settings
from app.core.shutdown import run_shutdown, run_step, stop_all_isolated
from app.main import lifespan


@pytest.fixture(autouse=True)
def _do_not_leak_global_logging_configuration():
    """The real lifespan calls configure_logging(), which sets structlog's GLOBAL configuration (with logger caching). Left in place it makes
    every later test's `capture_logs()` miss loggers that cached themselves under it — so restore the defaults afterwards."""
    import structlog

    yield
    structlog.reset_defaults()


# ── the runner ─────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_steps_run_in_order_and_a_clean_shutdown_reports_nothing_failed() -> None:
    calls: list[str] = []

    def step(name):
        async def run():
            calls.append(name)
        return name, run

    assert await run_shutdown([step("a"), step("b"), step("c")], timeout=1) == []
    assert calls == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_failing_step_does_not_skip_the_ones_after_it() -> None:
    calls: list[str] = []

    async def boom():
        calls.append("boom")
        raise RuntimeError("worker would not stop")

    async def after():
        calls.append("after")

    failed = await run_shutdown([("boom", boom), ("after", after)], timeout=1)

    assert failed == ["boom"]
    assert calls == ["boom", "after"]


@pytest.mark.asyncio
async def test_a_hung_step_is_abandoned_at_the_deadline_and_the_rest_still_run() -> None:
    calls: list[str] = []

    async def hang():
        await asyncio.sleep(60)

    async def after():
        calls.append("after")

    started = time.monotonic()
    failed = await run_shutdown([("hang", hang), ("after", after)], timeout=0.1)

    assert failed == ["hang"]
    assert calls == ["after"]
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed() -> None:
    """If the process is being cancelled again (a second signal), shutdown must stop being polite, not absorb it."""
    async def slow():
        await asyncio.sleep(60)

    task = asyncio.create_task(run_step("slow", slow, timeout=30))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_independent_stoppers_run_at_once_and_one_failure_or_hang_does_not_strand_the_rest() -> None:
    stopped: list[str] = []

    async def ok(name):
        stopped.append(name)

    async def boom():
        raise RuntimeError("x")

    async def hang():
        await asyncio.sleep(60)

    started = time.monotonic()
    failed = await stop_all_isolated(
        [("one", lambda: ok("one")), ("boom", boom), ("hang", hang), ("two", lambda: ok("two"))], timeout=0.2,
    )

    assert sorted(stopped) == ["one", "two"]
    assert sorted(failed) == ["boom", "hang"]
    assert time.monotonic() - started < 2          # concurrent: not 0.2s per stuck worker, one after another


# ── the real lifespan ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def instrumented(monkeypatch):
    """Replaces every closer the lifespan calls with a recorder, and every background loop with a cancellable sleeper."""
    calls: list[str] = []

    def recorder(name, *, raises=None, hang=False):
        async def run(*args, **kwargs):
            calls.append(name)
            if hang:
                await asyncio.sleep(60)
            if raises:
                raise raises
        return run

    async def sleeper(*args, **kwargs):
        await asyncio.sleep(3600)

    async def _init():
        return None

    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(settings, "shutdown_step_timeout_seconds", 0.3)
    monkeypatch.setattr("app.db.seed_templates.init_template_catalog", _init)

    import app.core.http_clients as http_clients
    import app.main as main_module
    import app.services.dlq_failure_counter as dlq
    import app.services.kafka_runtime as kafka_runtime
    import app.services.operation_runner as operation_runner
    import app.services.quality_job_runner as quality_jobs
    import app.services.quality_stream_worker as quality_stream
    import app.services.runtime_reconciler as reconciler
    from app.services import discovery_service

    class Engine:
        async def dispose(self):
            calls.append("database_engine")

    for module, name in ((operation_runner, "start_worker"), (quality_jobs, "start_worker"), (quality_jobs, "start_cleanup_task"),
                         (discovery_service, "start_cleanup_task")):
        monkeypatch.setattr(module, name, recorder(f"start:{module.__name__}.{name}"))
    monkeypatch.setattr(reconciler.runtime_reconciler, "run_forever", sleeper)
    monkeypatch.setattr(quality_stream.quality_stream_manager, "run_forever", sleeper)
    monkeypatch.setattr(dlq.dlq_counter_manager, "run_forever", sleeper)
    monkeypatch.setattr(main_module, "engine", Engine())

    def wire(**overrides):
        spec = {
            "operation_worker": (operation_runner, "stop_worker"),
            "quality_job_worker": (quality_jobs, "stop_worker"),
            "quality_job_cleanup": (quality_jobs, "stop_cleanup_task"),
            "discovery_cleanup": (discovery_service, "stop_cleanup_task"),
            "dlq_counters": (dlq.dlq_counter_manager, "stop_all"),
            "quality_streams": (quality_stream.quality_stream_manager, "stop_all"),
            "kafka_producer": (kafka_runtime, "close_kafka_producer"),
            "http_clients": (http_clients, "close_all"),
        }
        for name, (target, attr) in spec.items():
            monkeypatch.setattr(target, attr, recorder(name, **overrides.get(name, {})))
        # the lifespan imported these two by name at module load
        monkeypatch.setattr(main_module, "stop_discovery_cleanup", recorder("discovery_cleanup", **overrides.get("discovery_cleanup", {})), raising=False)

    return calls, wire


EXPECTED_ORDER = ["operation_worker", "quality_job_worker", "quality_job_cleanup", "dlq_counters", "quality_streams",
                  "kafka_producer", "http_clients", "database_engine"]


def _stops(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith("start:") and c != "discovery_cleanup"]


@pytest.mark.asyncio
async def test_shutdown_runs_every_closer_in_dependency_order_with_the_database_last(instrumented) -> None:
    calls, wire = instrumented
    wire()

    async with lifespan(app=None):
        pass

    assert _stops(calls) == EXPECTED_ORDER
    assert calls[-1] == "database_engine"
    assert calls.index("quality_streams") < calls.index("kafka_producer")      # the consumers/producers' users stop before the producer closes
    assert calls.index("kafka_producer") < calls.index("database_engine")


@pytest.mark.asyncio
async def test_a_failing_closer_does_not_stop_the_producer_the_http_clients_or_the_database_from_closing(instrumented) -> None:
    calls, wire = instrumented
    wire(operation_worker={"raises": RuntimeError("could not stop")}, quality_streams={"raises": RuntimeError("a consumer failed to close")})

    async with lifespan(app=None):
        pass

    assert _stops(calls) == EXPECTED_ORDER                                        # all of them ran, including the ones after the failures


@pytest.mark.asyncio
async def test_a_hung_closer_cannot_hold_shutdown_hostage(instrumented) -> None:
    calls, wire = instrumented
    wire(kafka_producer={"hang": True})                                           # an unreachable broker

    started = time.monotonic()
    async with lifespan(app=None):
        pass

    assert time.monotonic() - started < 5                                         # 0.3s per step deadline, not 60s
    assert "http_clients" in calls
    assert calls[-1] == "database_engine"


# ── the individual closers ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_http_client_failing_to_close_does_not_leak_the_others() -> None:
    import app.core.http_clients as http_clients

    class Client:
        def __init__(self, fail: bool) -> None:
            self.fail, self.closed = fail, False

        async def aclose(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("connection reset while closing")

    clients = {"a": Client(False), "b": Client(True), "c": Client(False)}
    http_clients._clients.clear()
    http_clients._clients.update(clients)

    await http_clients.close_all()                                                # must not raise

    assert all(c.closed for c in clients.values())
    assert http_clients._clients == {}


@pytest.mark.asyncio
async def test_a_kafka_producer_flushes_then_closes_and_never_waits_forever_for_a_dead_broker(monkeypatch) -> None:
    from app.services.kafka_runtime import NativeKafkaProducer

    monkeypatch.setattr(settings, "kafka_close_timeout_seconds", 0.2)
    order: list[str] = []

    class Healthy:
        def flush(self, timeout=None):
            order.append("flush")

        def close(self, timeout=None):
            order.append("close")

    healthy = NativeKafkaProducer.__new__(NativeKafkaProducer)
    healthy._client = Healthy()
    await healthy.close()
    assert order == ["flush", "close"]
    assert healthy._client is None

    class DeadBroker:
        def flush(self, timeout=None):
            time.sleep(3)                                                         # kafka-python blocking on an unreachable broker

        def close(self, timeout=None):
            pass

    dead = NativeKafkaProducer.__new__(NativeKafkaProducer)
    dead._client = DeadBroker()
    started = time.monotonic()
    await dead.close()                                                            # must return, not raise
    assert time.monotonic() - started < 2
    assert dead._client is None


@pytest.mark.asyncio
async def test_a_kafka_consumer_is_not_closed_underneath_its_own_running_poll_thread() -> None:
    from app.services.kafka_runtime import NativeKafkaConsumer

    events: list[tuple[str, float]] = []
    in_poll = threading.Event()

    class Client:
        def poll(self, timeout_ms=0, max_records=1):
            in_poll.set()
            time.sleep(0.4)                                                       # the thread is inside the (non-thread-safe) client
            events.append(("poll_returned", time.monotonic()))
            return {}

        def close(self):
            events.append(("closed", time.monotonic()))

    consumer = NativeKafkaConsumer(bootstrap_servers="x", topic="t", group_id="g")
    consumer._client = Client()
    polling = asyncio.create_task(consumer.poll())
    await asyncio.to_thread(in_poll.wait, 2)
    polling.cancel()                                                              # shutdown cancels the worker mid-poll
    with pytest.raises(asyncio.CancelledError):
        await polling

    await consumer.close()

    assert [name for name, _ in events] == ["poll_returned", "closed"]            # NOT closed while the poll thread was still using it
    assert consumer._client is None


@pytest.mark.asyncio
async def test_a_quality_worker_stopped_mid_poll_closes_its_consumer_once_and_leaves_nothing_registered(monkeypatch) -> None:
    from app.services.quality_stream_worker import QualityStreamWorker
    from app.services.quality_worker_health import quality_worker_health

    class Consumer:
        def __init__(self) -> None:
            self.closed = 0

        async def poll(self):
            await asyncio.sleep(60)

        async def commit(self) -> None:
            raise AssertionError("a stop must never commit")

        async def close(self) -> None:
            self.closed += 1

    consumer = Consumer()
    monkeypatch.setattr("app.services.quality_stream_worker.NativeKafkaConsumer", lambda **kw: consumer)
    worker = QualityStreamWorker(
        org_id="org_1", policy={"id": "p"},
        pipeline={"id": "pipe", "topic_name": "raw-d-s-e-batch-v1", "connector_config": {}},
    )
    worker.start()
    await asyncio.sleep(0.05)

    await worker.stop()

    assert consumer.closed == 1
    assert quality_worker_health.blocked_partitions() == 0
    assert quality_worker_health.oldest_uncommitted_age() == 0


@pytest.mark.asyncio
async def test_the_worker_managers_stop_every_worker_even_if_one_cannot() -> None:
    from app.services.dlq_failure_counter import DlqCounterManager
    from app.services.quality_stream_worker import QualityStreamManager

    stopped: list[str] = []

    class Worker:
        def __init__(self, name: str, behaviour: str) -> None:
            self.name, self.behaviour = name, behaviour

        async def stop(self) -> None:
            if self.behaviour == "fail":
                raise RuntimeError("stuck")
            if self.behaviour == "hang":
                await asyncio.sleep(60)
            stopped.append(self.name)

    settings.shutdown_step_timeout_seconds, original = 0.2, settings.shutdown_step_timeout_seconds
    try:
        for manager in (QualityStreamManager(), DlqCounterManager()):
            stopped.clear()
            manager._workers = {"a": Worker("a", "ok"), "b": Worker("b", "fail"), "c": Worker("c", "hang"), "d": Worker("d", "ok")}
            started = time.monotonic()
            await manager.stop_all()
            assert sorted(stopped) == ["a", "d"]
            assert manager._workers == {}
            assert time.monotonic() - started < 2
    finally:
        settings.shutdown_step_timeout_seconds = original

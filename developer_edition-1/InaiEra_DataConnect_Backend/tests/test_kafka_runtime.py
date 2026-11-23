import asyncio
import json

import httpx
import pytest

import app.services.kafka_runtime as kafka_runtime
from app.core.config import settings
from app.core.metrics import operations_total
from app.services.kafka_runtime import (
    DeliveryReport,
    InMemoryKafkaProducer,
    KafkaProduceError,
    NativeKafkaProducer,
    ProducerMetrics,
    RestProxyKafkaProducer,
    get_kafka_producer,
)


def _kafka_produce_count(status: str) -> float:
    try:
        return operations_total.labels(
            service="dataconnect", runtime_pool="none", connector_type="unknown",
            status=status, operation="kafka_produce",
        )._value.get()
    except KeyError:
        return 0.0


@pytest.fixture(autouse=True)
def _reset_native_producer_singleton():
    kafka_runtime._native_producer = None
    yield
    kafka_runtime._native_producer = None


def test_producer_metrics_snapshot() -> None:
    metrics = ProducerMetrics(produced_total=5, failed_total=1, retries_total=2, last_latency_ms=12.3)
    assert metrics.snapshot() == {
        "produced_total": 5,
        "failed_total": 1,
        "retries_total": 2,
        "last_latency_ms": 12.3,
    }


@pytest.mark.asyncio
async def test_in_memory_producer_records_and_acks() -> None:
    producer = InMemoryKafkaProducer()

    delivery = await producer.produce(topic="t1", value={"a": 1}, key="k1")

    assert len(producer.records) == 1
    assert isinstance(delivery, DeliveryReport)
    assert delivery.topic == "t1"
    assert producer.metrics.produced_total == 1


@pytest.mark.asyncio
async def test_rest_proxy_producer_succeeds_and_returns_delivery_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["records"][0]["key"] == "k1"
        return httpx.Response(200, json={"offsets": [{"partition": 2, "offset": 42, "error_code": None}]})

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082", transport=httpx.MockTransport(handler), backoff_seconds=0.001
    )

    delivery = await producer.produce(topic="t1", value={"a": 1}, key="k1")

    assert delivery.partition == 2
    assert delivery.offset == 42
    assert delivery.attempts == 1
    assert producer.metrics.produced_total == 1
    assert producer.metrics.failed_total == 0


@pytest.mark.asyncio
async def test_rest_proxy_producer_retries_transient_failures_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503, text="broker unavailable")
        return httpx.Response(200, json={"offsets": [{"partition": 0, "offset": 1, "error_code": None}]})

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082",
        transport=httpx.MockTransport(handler),
        max_retries=5,
        backoff_seconds=0.001,
    )

    delivery = await producer.produce(topic="t1", value={"a": 1})

    assert delivery.attempts == 3
    assert calls["count"] == 3
    assert producer.metrics.retries_total == 2
    assert producer.metrics.produced_total == 1


@pytest.mark.asyncio
async def test_rest_proxy_producer_treats_body_level_error_code_as_failure() -> None:
    """A 200 response with a per-record error_code must not be treated as a
    successful delivery — HTTP status alone is not proof of delivery."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"offsets": [{"error_code": 3, "error": "UNKNOWN_TOPIC_OR_PARTITION"}]})

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082",
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_seconds=0.001,
    )

    with pytest.raises(KafkaProduceError) as exc_info:
        await producer.produce(topic="missing-topic", value={"a": 1})

    assert exc_info.value.topic == "missing-topic"
    assert exc_info.value.attempts == 2
    assert producer.metrics.failed_total == 1


@pytest.mark.asyncio
async def test_rest_proxy_producer_raises_after_exhausting_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082",
        transport=httpx.MockTransport(handler),
        max_retries=3,
        backoff_seconds=0.001,
    )

    with pytest.raises(KafkaProduceError):
        await producer.produce(topic="t1", value={"a": 1})

    assert producer.metrics.failed_total == 1
    assert producer.metrics.retries_total == 3


@pytest.mark.asyncio
async def test_producer_records_a_low_cardinality_metric_on_success() -> None:
    """The metric never carries the topic name — only the bounded outcome."""
    before = _kafka_produce_count("success")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"offsets": [{"partition": 0, "offset": 1, "error_code": None}]})

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082", transport=httpx.MockTransport(handler), backoff_seconds=0.001
    )

    await producer.produce(topic="some-topic-that-must-never-become-a-label", value={"a": 1})

    assert _kafka_produce_count("success") == before + 1


@pytest.mark.asyncio
async def test_producer_records_a_low_cardinality_metric_on_terminal_failure() -> None:
    before = _kafka_produce_count("failed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    producer = RestProxyKafkaProducer(
        "http://rest-proxy:8082",
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_seconds=0.001,
    )

    with pytest.raises(KafkaProduceError):
        await producer.produce(topic="t1", value={"a": 1})

    assert _kafka_produce_count("failed") == before + 1


def test_get_kafka_producer_defaults_to_native_backend(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_producer_backend", "native")

    producer = asyncio.run(get_kafka_producer())

    assert isinstance(producer, NativeKafkaProducer)


def test_get_kafka_producer_selects_rest_proxy_backend(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_producer_backend", "rest_proxy")

    producer = asyncio.run(get_kafka_producer())

    assert isinstance(producer, RestProxyKafkaProducer)


@pytest.mark.asyncio
async def test_rest_proxy_producer_uses_the_shared_client_in_production(monkeypatch) -> None:
    """With no `transport` override (the real production path — see
    get_kafka_producer), produce() must go through the shared, pooled client
    rather than opening its own — this is the whole point of the fix."""
    calls = {"get_client": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"offsets": [{"partition": 0, "offset": 1, "error_code": None}]})

    shared_client = httpx.AsyncClient(
        base_url="http://rest-proxy:8082", transport=httpx.MockTransport(handler)
    )

    async def _fake_get_client(base_url: str):
        calls["get_client"] += 1
        assert base_url == "http://rest-proxy:8082"
        return shared_client

    monkeypatch.setattr("app.services.kafka_runtime.get_client", _fake_get_client)

    producer = RestProxyKafkaProducer("http://rest-proxy:8082")  # no transport= override

    try:
        delivery = await producer.produce(topic="t1", value={"a": 1})
        assert delivery.offset == 1
        assert calls["get_client"] == 1
    finally:
        await shared_client.aclose()

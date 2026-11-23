"""
Kafka producer implementations for the streaming quality path.

`InMemoryKafkaProducer` is a test double only — it never touches a network
and silently "succeeds" for every record, which is exactly what must never
happen in production (an API response of "processed" with nothing actually
written to the prepared/quarantine topic). Production code must obtain a
producer through `get_kafka_producer()` (a FastAPI dependency), never by
constructing `InMemoryKafkaProducer` directly.

Two real implementations are available, selected by
`settings.kafka_producer_backend`:

- `NativeKafkaProducer` (default: "native") talks directly to the Kafka
  brokers using kafka-python (already a project dependency — see
  runtime_metrics.py), with acks="all" and enable_idempotence=True: a real
  broker-acknowledged delivery (partition + offset) per record, the client's
  own internal retry/backoff for transient errors, a bounded request
  timeout, and true producer-level idempotence — not just same-key routing.
- `RestProxyKafkaProducer` ("rest_proxy") goes through Confluent's Kafka REST
  Proxy instead, for environments where the app can't reach the brokers
  directly. It gets its own retry/backoff loop here (the REST Proxy has no
  producer-level idempotence to lean on) and inspects the per-record
  `error_code` in the response body, since REST Proxy can return HTTP 200
  while still failing to write the record — HTTP status alone is not proof
  of delivery.

Both raise `KafkaProduceError` (never swallow a failure) once retries are
exhausted, and record the same `ProducerMetrics` counters plus a
low-cardinality Prometheus operation outcome (see app/core/metrics.py) —
topic/key/error strings stay in the structured log line right next to each
of these calls, never in a metric label.
"""

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

from app.core.config import settings
from app.core.http_clients import get_client
from app.core.metrics import record_operation

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class KafkaRecord:
    topic: str
    key: str | None
    value: dict[str, Any]
    headers: dict[str, str] | None = None
    partition: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class DeliveryReport:
    """What a successful produce() call acknowledges was actually written."""

    topic: str
    partition: int | None
    offset: int | None
    key: str | None
    attempts: int
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "key": self.key,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 2),
        }


class KafkaProduceError(RuntimeError):
    """Raised when a record could not be durably delivered after retries —
    callers must treat this as an explicit failure, never as "processed"."""

    def __init__(self, topic: str, key: str | None, attempts: int, cause: Exception):
        super().__init__(f"Failed to produce to '{topic}' after {attempts} attempt(s): {cause}")
        self.topic = topic
        self.key = key
        self.attempts = attempts
        self.cause = cause


class KafkaProducer(Protocol):
    async def produce(
        self,
        *,
        topic: str,
        value: dict[str, Any] | None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DeliveryReport:
        ...


@dataclass
class ProducerMetrics:
    """Minimal in-process counters, in the same spirit as the derived metrics
    monitoring_service.py exposes elsewhere — not tied to a specific metrics
    backend, just enough to see produce health without a live broker query."""

    produced_total: int = 0
    failed_total: int = 0
    retries_total: int = 0
    last_latency_ms: float = 0.0

    def snapshot(self) -> dict[str, float]:
        return {
            "produced_total": self.produced_total,
            "failed_total": self.failed_total,
            "retries_total": self.retries_total,
            "last_latency_ms": self.last_latency_ms,
        }


class InMemoryKafkaProducer:
    """Test double ONLY. Records what would have been produced, with no
    network call, no retry, no delivery ack, and no failure mode to exercise —
    do not wire this into any request path."""

    def __init__(self) -> None:
        self.records: list[KafkaRecord] = []
        self.metrics = ProducerMetrics()

    async def produce(
        self,
        *,
        topic: str,
        value: dict[str, Any] | None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DeliveryReport:
        if key is not None and not isinstance(key, str):
            # The real producer's key serializer does `key.encode(...)`, so a non-text key raises there. This double must not be more
            # lenient than what it stands in for, or a test passes on a key production would reject.
            raise TypeError(f"Kafka message key must be text or None, got {type(key).__name__}: {key!r}")
        self.records.append(KafkaRecord(topic=topic, key=key, value=value, headers=headers))
        self.metrics.produced_total += 1
        return DeliveryReport(
            topic=topic, partition=0, offset=len(self.records) - 1, key=key, attempts=1, latency_ms=0.0
        )


class NativeKafkaProducer:
    """Produces directly to the Kafka brokers via kafka-python. Its client is
    synchronous, so calls are offloaded to a worker thread; kafka-python's
    KafkaProducer is documented as thread-safe for exactly this kind of shared,
    concurrent use, so one instance is built lazily and reused across requests.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        timeout_seconds: float = 15.0,
        retries: int = 3,
        acks: str = "all",
        enable_idempotence: bool = True,
    ):
        self.bootstrap_servers = bootstrap_servers
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.acks = acks
        self.enable_idempotence = enable_idempotence
        self.metrics = ProducerMetrics()
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get_client(self):
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    from kafka import KafkaProducer as _SyncKafkaProducer

                    self._client = await asyncio.to_thread(
                        _SyncKafkaProducer,
                        bootstrap_servers=self.bootstrap_servers,
                        acks=self.acks,
                        retries=self.retries,
                        enable_idempotence=self.enable_idempotence,
                        request_timeout_ms=int(self.timeout_seconds * 1000),
                        # None stays None: a tombstone (the quality worker forwards a CDC delete; quality_stream_worker._pass_tombstone).
                        value_serializer=lambda v: None if v is None else json.dumps(v).encode("utf-8"),
                        key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
                    )
        return self._client

    async def produce(
        self,
        *,
        topic: str,
        value: dict[str, Any] | None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DeliveryReport:
        from kafka.errors import KafkaError

        client = await self._get_client()
        kafka_headers = [(k, v.encode("utf-8")) for k, v in (headers or {}).items()] or None
        start = time.monotonic()

        def _send_and_wait():
            future = client.send(topic, key=key, value=value, headers=kafka_headers)
            return future.get(timeout=self.timeout_seconds)

        try:
            metadata = await asyncio.to_thread(_send_and_wait)
        except KafkaError as exc:
            self.metrics.failed_total += 1
            # The topic name is exactly the kind of unbounded identifier that
            # must stay in the (structured) log line, never become a metric
            # label — see app/core/metrics.py.
            record_operation(runtime_pool=None, connector_type=None, status="failed", operation="kafka_produce")
            logger.error("kafka.produce_failed", topic=topic, key=key, error=str(exc))
            # kafka-python already retried internally (per `retries`) before
            # this future failed — no point retrying again at this layer.
            raise KafkaProduceError(topic, key, self.retries + 1, exc) from exc

        latency_ms = (time.monotonic() - start) * 1000
        self.metrics.produced_total += 1
        self.metrics.last_latency_ms = latency_ms
        record_operation(runtime_pool=None, connector_type=None, status="success", operation="kafka_produce")
        logger.info(
            "kafka.produced", topic=topic, key=key, partition=metadata.partition,
            offset=metadata.offset, latency_ms=round(latency_ms, 2),
        )
        return DeliveryReport(
            topic=topic, partition=metadata.partition, offset=metadata.offset,
            key=key, attempts=1, latency_ms=latency_ms,
        )

    async def close(self) -> None:
        """Flush what is in flight and release the broker connections — BOUNDED. kafka-python's close() with no timeout blocks until every
        pending request completes, which never happens against an unreachable broker; so both steps get a deadline, and on expiry the
        client is abandoned (its thread finishes on its own) rather than holding application shutdown hostage."""
        client, self._client = self._client, None
        if client is None:
            return
        timeout = settings.kafka_close_timeout_seconds
        try:
            await asyncio.wait_for(asyncio.to_thread(client.flush, timeout), timeout=timeout + 1)
            await asyncio.wait_for(asyncio.to_thread(client.close, timeout), timeout=timeout + 1)
        except (TimeoutError, Exception):  # noqa: BLE001 - shutdown must continue whatever the broker does
            logger.warning("kafka_producer.close_abandoned", timeout_seconds=timeout, exc_info=True)


class RestProxyKafkaProducer:
    """Produces through Confluent's Kafka REST Proxy — an approved Kafka
    gateway for environments without direct broker access. Retries transient
    failures (connection errors, timeouts, 5xx, and a body-level
    `error_code`) with exponential backoff, and always raises
    KafkaProduceError rather than reporting success once retries are
    exhausted. Same-key records land on the same partition (ordering), but
    unlike NativeKafkaProducer this gateway has no producer-level
    exactly-once/idempotence to enable — that's the tradeoff for not needing
    direct broker connectivity.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self._transport = transport  # test hook only; None uses real networking
        self.metrics = ProducerMetrics()

    async def produce(
        self,
        *,
        topic: str,
        value: dict[str, Any] | None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DeliveryReport:
        payload = {
            "records": [
                {
                    "key": key,
                    "value": value,
                    "headers": [{"key": k, "value": v} for k, v in (headers or {}).items()],
                }
            ]
        }
        body = json.dumps(payload)
        start = time.monotonic()
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if self._transport is not None:
                    # Test hook (httpx.MockTransport): an isolated, throwaway
                    # client — never shared, since tests reuse the same
                    # base_url with different transports.
                    async with httpx.AsyncClient(
                        base_url=self.base_url, timeout=self.timeout_seconds, transport=self._transport
                    ) as client:
                        response = await client.post(
                            f"/topics/{topic}",
                            content=body,
                            headers={"Content-Type": "application/vnd.kafka.json.v2+json"},
                        )
                else:
                    # Production path: the shared, pooled client for this
                    # endpoint (see app/core/http_clients.py) — not closed
                    # here, reused by every subsequent call.
                    client = await get_client(self.base_url)
                    response = await client.post(
                        f"/topics/{topic}",
                        content=body,
                        headers={"Content-Type": "application/vnd.kafka.json.v2+json"},
                        timeout=self.timeout_seconds,
                    )
                response.raise_for_status()
                record_result = (response.json().get("offsets") or [{}])[0]
                error_code = record_result.get("error_code")
                if error_code:
                    # REST Proxy can accept the HTTP request but fail to write
                    # the record — a 200 here is not proof of delivery.
                    raise RuntimeError(
                        f"REST Proxy rejected the record (error_code={error_code}): "
                        f"{record_result.get('error')}"
                    )
            except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure retries
                last_error = str(exc)
                self.metrics.retries_total += 1
                logger.warning(
                    "kafka.produce_retry", topic=topic, key=key,
                    attempt=attempt, max_retries=self.max_retries, error=last_error,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                continue
            else:
                latency_ms = (time.monotonic() - start) * 1000
                self.metrics.produced_total += 1
                self.metrics.last_latency_ms = latency_ms
                record_operation(runtime_pool=None, connector_type=None, status="success", operation="kafka_produce")
                logger.info(
                    "kafka.produced", topic=topic, key=key,
                    partition=record_result.get("partition"), offset=record_result.get("offset"),
                    attempt=attempt, latency_ms=round(latency_ms, 2),
                )
                return DeliveryReport(
                    topic=topic, partition=record_result.get("partition"),
                    offset=record_result.get("offset"), key=key, attempts=attempt, latency_ms=latency_ms,
                )

        self.metrics.failed_total += 1
        # Final outcome only — the retry loop above deliberately doesn't
        # record an operation per attempt, only the terminal result, same as
        # ProducerMetrics.failed_total itself (retries_total is separate).
        record_operation(runtime_pool=None, connector_type=None, status="failed", operation="kafka_produce")
        logger.error("kafka.produce_failed", topic=topic, key=key, attempts=self.max_retries, error=last_error)
        raise KafkaProduceError(topic, key, self.max_retries, RuntimeError(last_error or "unknown error"))


# Kafka's legal characters for ONE topic name. A comma is not among them, which is what makes "a,b,c" illegal.
_KAFKA_TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]{1,249}$")


class KafkaConsumer(Protocol):
    async def poll(self) -> KafkaRecord | None:
        ...


class NativeKafkaConsumer:
    """Consumes from Kafka via kafka-python's synchronous client, offloaded
    to a worker thread the same way NativeKafkaProducer offloads its sends.

    Offsets are committed manually, from `commit()`, and only after the
    caller has finished handling the record it returned from `poll()` —
    never auto-commit. Auto-commit acknowledges a record as soon as it's
    fetched, before the caller has actually acted on it; a crash between
    fetch and handling would then silently drop that record instead of
    redelivering it on restart.

    One real client per instance, matching one Kafka consumer group per
    pipeline (see QualityStreamWorker) — kafka-python's KafkaConsumer is not
    documented thread-safe for concurrent use the way its KafkaProducer is,
    so unlike _get_native_producer this is never shared across callers.
    """

    def __init__(
        self, *, bootstrap_servers: str, group_id: str, topic: str | None = None, topics: Sequence[str] | None = None,
        poll_timeout_ms: int = 1000,
    ):
        """One topic (`topic=`) or several (`topics=[...]`), all read by the one consumer group. A topic name is exactly one Kafka topic: a
        comma-joined string is refused here, because Kafka rejects it as an illegal name on every poll and the caller would retry forever,
        reading nothing (NEW-02: a sink with three input topics was silently unchecked for that reason)."""
        names = list(topics) if topics is not None else ([topic] if topic is not None else [])
        names = list(dict.fromkeys(names))
        if not names:
            raise ValueError("A Kafka consumer needs at least one topic.")
        for name in names:
            if not isinstance(name, str) or not _KAFKA_TOPIC_RE.fullmatch(name):
                raise ValueError(f"{name!r} is not a single Kafka topic name; pass several topics as topics=[...], not as one comma-joined string.")
        self.bootstrap_servers = bootstrap_servers
        self.topics: tuple[str, ...] = tuple(names)
        self.group_id = group_id
        self.poll_timeout_ms = poll_timeout_ms
        self._client: Any = None
        self._inflight_poll: asyncio.Future[Any] | None = None

    async def _get_client(self):
        if self._client is None:
            from kafka import KafkaConsumer as _SyncKafkaConsumer

            self._client = await asyncio.to_thread(
                _SyncKafkaConsumer,
                *self.topics,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_id,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                key_deserializer=lambda k: k.decode("utf-8") if k is not None else None,
                # Deserialize to raw bytes here, not JSON: a single malformed
                # message must not blow up the whole poll() batch. The
                # caller (QualityStreamWorker) decodes JSON per-record so it
                # can isolate and skip just that one record.
                value_deserializer=lambda v: v,
            )
        return self._client

    async def poll(self) -> KafkaRecord | None:
        client = await self._get_client()

        def _poll_one() -> Any:
            batches = client.poll(timeout_ms=self.poll_timeout_ms, max_records=1)
            for records in batches.values():
                for rec in records:
                    return rec
            return None

        # The poll runs in a worker thread that cannot be interrupted. Awaiting it through a SHIELDED future means that cancelling the
        # caller (shutdown) does not abandon a thread that is still using the client: close() waits for it (see below).
        self._inflight_poll = asyncio.ensure_future(asyncio.to_thread(_poll_one))
        rec = await asyncio.shield(self._inflight_poll)
        if rec is None:
            return None
        headers = {k: v.decode("utf-8", "replace") for k, v in (rec.headers or [])}
        raw_value = rec.value.decode("utf-8", "replace") if rec.value is not None else None
        return KafkaRecord(
            topic=rec.topic, key=rec.key, value={"_raw": raw_value}, headers=headers, partition=rec.partition, offset=rec.offset,
        )

    @property
    def topic(self) -> str:
        """The topic(s) this consumer reads, joined with "," for logs only — never pass it back to Kafka."""
        return ",".join(self.topics)

    async def end_offsets(self) -> dict[tuple[str, int], int]:
        """{(topic, partition): the offset just past the newest record} for every partition of every topic this consumer reads. Call before
        polling starts: the underlying client is not thread-safe, and a poll thread may be inside it."""
        client = await self._get_client()

        def _read() -> dict[tuple[str, int], int]:
            from kafka import TopicPartition

            partitions = [TopicPartition(t, p) for t in self.topics for p in (client.partitions_for_topic(t) or set())]
            return {(tp.topic, tp.partition): int(end) for tp, end in client.end_offsets(partitions).items()} if partitions else {}

        return await asyncio.to_thread(_read)

    def assigned_partition_count(self) -> int:
        """How many partitions this consumer currently owns. A worker handles one record at a time, so while it is stuck on one
        record none of these advance — which is what the "blocked partitions" metric counts. 0 until the group assigns some."""
        client = self._client
        if client is None:
            return 0
        try:
            return len(client.assignment())
        except Exception:  # noqa: BLE001 - a metric must never break the worker
            return 0

    async def commit(self) -> None:
        client = await self._get_client()
        await asyncio.to_thread(client.commit)

    async def close(self) -> None:
        """kafka-python's KafkaConsumer is NOT thread-safe, and a cancelled poll's thread may still be inside it (up to poll_timeout_ms).
        Closing the client underneath it is a race; so wait for that thread first — but only for a bounded time."""
        pending = self._inflight_poll
        if pending is not None and not pending.done():
            await asyncio.wait({pending}, timeout=self.poll_timeout_ms / 1000 + settings.kafka_close_timeout_seconds)
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(client.close), timeout=settings.kafka_close_timeout_seconds)
        except (TimeoutError, Exception):  # noqa: BLE001 - shutdown must continue
            logger.warning("kafka_consumer.close_abandoned", topic=self.topic, exc_info=True)


_native_producer: NativeKafkaProducer | None = None
_native_producer_lock = asyncio.Lock()


async def _get_native_producer() -> NativeKafkaProducer:
    global _native_producer
    if _native_producer is None:
        async with _native_producer_lock:
            if _native_producer is None:
                _native_producer = NativeKafkaProducer(
                    settings.kafka_bootstrap,
                    timeout_seconds=settings.kafka_produce_timeout_seconds,
                    retries=settings.kafka_produce_max_retries,
                )
    return _native_producer


async def get_kafka_producer() -> KafkaProducer:
    """FastAPI dependency for the production Kafka producer — inject this
    (`Depends(get_kafka_producer)`) rather than constructing a producer
    directly. Tests should override this dependency
    (`app.dependency_overrides[get_kafka_producer] = lambda: InMemoryKafkaProducer()`)
    instead of relying on its default."""
    if settings.kafka_producer_backend == "rest_proxy":
        return RestProxyKafkaProducer(
            settings.kafka_rest_proxy_url,
            timeout_seconds=settings.kafka_produce_timeout_seconds,
            max_retries=settings.kafka_produce_max_retries,
            backoff_seconds=settings.kafka_produce_backoff_seconds,
        )
    return await _get_native_producer()


async def close_kafka_producer() -> None:
    """Release the native producer's broker connections on app shutdown."""
    global _native_producer
    if _native_producer is not None:
        await _native_producer.close()
        _native_producer = None

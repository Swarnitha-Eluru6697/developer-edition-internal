"""
Prometheus metrics — a single, deliberately small, restricted surface.

The full policy — what may and may not be a label, the value vocabularies, the series budget and how to add a metric — is
docs/METRICS_POLICY.md, and tests/test_metrics_policy.py enforces it (label names, value vocabularies, call sites, a series budget).

Rule: every metric here uses ONLY five labels — service, runtime_pool,
connector_type, status, operation — and nothing else. There is no escape
hatch for an arbitrary extra label: a connector name, topic name, entity
name, pipeline_id, or a raw exception/error message must never become a
label value. Prometheus creates one time series per unique combination of
label values; an unbounded label (a name that's different per connector, per
topic, or per error) turns "a few hundred time series" into "one new series
for every connector/topic/error message this deployment has ever seen" — a
cardinality blow-up that never shrinks back down, and that a remote-write
backend typically bills per series. High-cardinality identifiers belong in
structured logs (app/core/logging.py already renders JSON in every
non-development env) or a trace span, never here.

`record_operation()`'s signature enforces this structurally rather than by
convention: it only accepts the five safe dimensions as keyword arguments, so
a caller cannot pass a connector_name/topic_name/error string as a label
without editing this function's signature — and, one hopes, reading this
docstring first.

Each dimension is a small, fixed enumeration, never a per-instance identifier:
  - service:        this deployment's own name (currently always "dataconnect";
                     kept as a label rather than baked into the metric name so
                     multiple DataConnect processes/services can share one
                     Prometheus instance without name-colliding).
  - runtime_pool:    one of the fixed worker pools DataConnect knows about
                     (see MonitoringService.list_connect_clusters) — "none"
                     when an operation isn't tied to a pool.
  - connector_type:  "source" | "sink" | "unknown".
  - status:          a bounded outcome string — e.g. one of
                     RuntimeReconciler's observed_state values (running /
                     paused / degraded / failed / absent / unknown), or
                     "success" / "failed" for a one-shot operation.
  - operation:       a fixed, small set of things this service does (e.g.
                     "reconcile", "kafka_produce") — never a per-request or
                     per-entity identifier.
"""

from collections.abc import Callable

from prometheus_client import Counter, Gauge, generate_latest

SERVICE_NAME = "dataconnect"

# ── Bounded label vocabularies (docs/METRICS_POLICY.md) ────────────────────────────────────────────────────────────────
# A label VALUE must come from a small, fixed set, or Prometheus creates one time series per distinct value, forever. These are the sets.
# A value outside its set is recorded as OTHER (and logged once, rate-limited) rather than becoming a new series — so a typo, or a caller that
# passes something derived from a pipeline/connector/error, costs one visible "other" bucket instead of unbounded growth.
ALLOWED_LABEL_NAMES = frozenset({"service", "runtime_pool", "connector_type", "status", "operation"})
CONNECTOR_TYPES = frozenset({"source", "sink", "unknown"})
# RuntimeReconciler's observed_state values, plus the two one-shot outcomes.
STATUSES = frozenset({"running", "paused", "degraded", "failed", "absent", "unknown", "success"})
OPERATIONS = frozenset({"reconcile", "kafka_produce"})
QUALITY_STEPS = frozenset({"stats", "quarantine", "prepared"})
NONE = "none"
OTHER = "other"
_SEEN_COALESCED: set[tuple[str, str]] = set()
_MAX_LOGGED_COALESCED = 20


def _bounded(label: str, value: str | None, allowed: frozenset[str], *, default: str) -> str:
    """`value` if it is in `allowed`; `default` for None; otherwise OTHER. Never raises — a metric must not break the code it observes."""
    if value is None:
        return default
    if value in allowed:
        return value
    if len(_SEEN_COALESCED) < _MAX_LOGGED_COALESCED and (label, str(value)[:40]) not in _SEEN_COALESCED:
        _SEEN_COALESCED.add((label, str(value)[:40]))
        import structlog

        structlog.get_logger(__name__).warning("metrics.label_value_not_in_vocabulary", label=label, value=str(value)[:40], recorded_as=OTHER)
    return OTHER


def _runtime_pool(value: str | None) -> str:
    if value is None:
        return NONE
    from app.core.connect_pools import canonical_pool_id

    pool = canonical_pool_id(value)
    return pool if pool is not None else _bounded("runtime_pool", value, frozenset(), default=NONE)

operations_total = Counter(
    "dataconnect_operations_total",
    "Count of DataConnect runtime operations, by outcome.",
    ["service", "runtime_pool", "connector_type", "status", "operation"],
)


def record_operation(*, runtime_pool: str | None, connector_type: str | None, status: str, operation: str) -> None:
    """Increment the shared operations counter. `runtime_pool`/`connector_type`
    of None are recorded as "none"/"unknown" — Prometheus label values can't
    be null. Every value is checked against its fixed vocabulary above: one that
    is not in it is recorded as "other", so this can never add unbounded series."""
    operations_total.labels(
        service=SERVICE_NAME,
        runtime_pool=_runtime_pool(runtime_pool),
        connector_type=_bounded("connector_type", connector_type, CONNECTOR_TYPES, default="unknown"),
        status=_bounded("status", status, STATUSES, default="unknown"),
        operation=_bounded("operation", operation, OPERATIONS, default=OTHER),
    ).inc()


# ── Quality stream worker (GAP-03) ───────────────────────────────────────────────────────────────────────────────────
# Same rule as above: no pipeline id, topic or error text as a label. Which pipeline is stuck is in the logs and on the pipeline's
# own status (DEGRADED + its runtime error); these answer "is anything stuck, how many partitions, for how long" for an alert.

quality_retries_total = Counter(
    "dataconnect_quality_retries_total",
    "Retries of a quality-stream step (the stats write or the quarantine copy) after a failed attempt.",
    ["service", "connector_type", "operation"],
)
quality_dlq_failures_total = Counter(
    "dataconnect_quality_dlq_failures_total",
    "Failed attempts to write a record to the worker dead-letter topic. Each one leaves the record uncommitted.",
    ["service", "connector_type"],
)
dlq_unattributed_records_total = Counter(
    "dataconnect_dlq_unattributed_records_total",
    "Records on a SHARED connector dead-letter topic that could not be attributed to one pipeline (no connector name header, or a connector "
    "that is not one of the topic's pipelines) and so were counted for none.",
    ["service"],
)
quality_replays_skipped_total = Counter(
    "dataconnect_quality_replays_skipped_total",
    "Records the quality worker was handed again (after a crash between recording them and committing the Kafka offset) whose statistics "
    "were therefore NOT counted a second time.",
    ["service", "connector_type"],
)
quality_blocked_partitions = Gauge(
    "dataconnect_quality_blocked_partitions",
    "Partitions that cannot advance because their quality worker is stuck on a record it cannot commit past.",
    ["service"],
)
quality_oldest_uncommitted_age_seconds = Gauge(
    "dataconnect_quality_oldest_uncommitted_age_seconds",
    "Age of the oldest record a quality worker has taken and not yet committed (0 when none is in flight).",
    ["service"],
)


# ── Operation results (GAP-04) ───────────────────────────────────────────────────────────────────────────────────────
# `operation` is the operation TYPE (pipeline_deploy, ...): a fixed, registered set, never a per-request value.

operation_result_invalid_total = Counter(
    "dataconnect_operation_result_invalid_total",
    "Operations whose action succeeded but whose result did not satisfy its contract (OPERATION_RESULT_INVALID).",
    ["service", "operation"],
)


def record_operation_result_invalid(*, operation_type: str, known_types: frozenset[str] | None = None) -> None:
    """`known_types` is the set of REGISTERED operation types (the caller owns that registry); anything else is recorded as "other"."""
    label = operation_type if known_types is None or operation_type in known_types else OTHER
    operation_result_invalid_total.labels(service=SERVICE_NAME, operation=label).inc()


def record_quality_retry(*, connector_type: str | None, step: str) -> None:
    """`step` is one of the worker's two retried steps — "stats" or "quarantine"."""
    quality_retries_total.labels(
        service=SERVICE_NAME, connector_type=_bounded("connector_type", connector_type, CONNECTOR_TYPES, default="unknown"),
        operation=_bounded("operation", step, QUALITY_STEPS, default=OTHER),
    ).inc()


def record_quality_dlq_failure(*, connector_type: str | None) -> None:
    quality_dlq_failures_total.labels(
        service=SERVICE_NAME, connector_type=_bounded("connector_type", connector_type, CONNECTOR_TYPES, default="unknown"),
    ).inc()


def record_quality_replay_skipped(*, connector_type: str | None) -> None:
    quality_replays_skipped_total.labels(
        service=SERVICE_NAME, connector_type=_bounded("connector_type", connector_type, CONNECTOR_TYPES, default="unknown"),
    ).inc()


def record_dlq_unattributed(count: int) -> None:
    if count > 0:
        dlq_unattributed_records_total.labels(service=SERVICE_NAME).inc(count)


def bind_quality_gauges(*, blocked_partitions: Callable[[], float], oldest_uncommitted_age: Callable[[], float]) -> None:
    """Both gauges are computed at scrape time from the live worker registry (app/services/quality_worker_health.py), so a worker
    that finished or died can never leave a stale non-zero value behind."""
    quality_blocked_partitions.labels(service=SERVICE_NAME).set_function(blocked_partitions)
    quality_oldest_uncommitted_age_seconds.labels(service=SERVICE_NAME).set_function(oldest_uncommitted_age)


def render_metrics() -> bytes:
    """Prometheus text exposition format, from the default global registry —
    prometheus_client.Counter (and prometheus-fastapi-instrumentator's own
    metrics, when that optional dependency is installed — see app/main.py)
    both register there unless told otherwise, so one /metrics response
    covers both."""
    return generate_latest()

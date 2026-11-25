from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConnectCluster(BaseModel):
    """A worker pool — this list itself is genuinely org-independent
    infrastructure (every org shares the same fixed set of six pools), but
    `status` reflects only the calling org's own connectors assigned to
    that pool, never other orgs' — see MonitoringService.list_connect_clusters."""

    id: str
    name: str
    pool_type: str
    # What the pool is for (app/core/connect_pools.py) and the Connect cluster it runs on. `shared_cluster` is true when
    # other pools run on the same cluster too (a small or development deployment); production gives each pool its own.
    workload: str | None = None
    runtime_endpoint: str | None = None
    shared_cluster: bool = False
    # This org's connectors in the pool. `status` is healthy | degraded | unknown, or `idle` when there are none.
    connector_count: int = 0
    status: str
    observed_at: str | None = None


class RuntimeConnector(BaseModel):
    """One DataConnect pipeline's runtime connector, as seen on the shared
    Kafka Connect cluster — always carries the pipeline_id/topic_name it
    maps back to, never a bare connector name on its own."""

    pipeline_id: str
    connector_name: str
    topic_name: str | None = None
    runtime_pool_id: str | None = None
    observed_state: str
    observed_at: str | None = None
    runtime_error: str | None = None


class ConnectorTaskStatus(BaseModel):
    id: Any = None
    state: str = "UNKNOWN"
    worker_id: str | None = None
    trace: str | None = None


class ConnectorStatus(BaseModel):
    name: str
    pipeline_id: str
    topic_name: str | None = None
    connector: dict[str, Any] = Field(default_factory=dict)
    tasks: list[ConnectorTaskStatus] = Field(default_factory=list)
    observed_state: str
    observed_at: str | None = None
    runtime_endpoint: str | None = None
    error: str | None = None


class ConnectorTask(BaseModel):
    connector: str
    pipeline_id: str
    observed_at: str | None = None
    id: Any = None
    state: str = "UNKNOWN"
    worker_id: str | None = None
    trace: str | None = None


class RuntimeFlowConnector(BaseModel):
    name: str
    pipeline_id: str
    type: str
    state: str
    tasks: list[str | None] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class RuntimeFlowTopic(BaseModel):
    topic: str
    messages: int
    pipeline_id: str


class RuntimeFlowSink(BaseModel):
    connector: str
    consumed: int = 0
    lag: int = 0
    topics: list[str] = Field(default_factory=list)


class RuntimeFlow(BaseModel):
    """GET /runtime/flow's response (app/services/runtime_metrics.py) — a
    live, uncached probe of Kafka Connect + Kafka on every call, not a
    persisted/reconciled snapshot the way PipelineRuntimeState is. FE-BUG-16:
    `reachable=False` means Connect itself couldn't be reached for THIS
    request — `connectors`/`topics`/`sinks` are then honestly empty rather
    than a previous call's stale values. That's different from the HTTP
    request to this endpoint itself failing (DataConnect's own backend
    unreachable, a 5xx, a network drop) — the frontend is responsible for
    not treating the latter as equivalent to this endpoint's own honest
    "connect unreachable" answer; this schema only had an untyped `dict`
    return before, which meant `reachable`/`message` never even made it
    into the generated frontend types."""

    reachable: bool
    connect_url: str
    bootstrap: str
    connectors: list[RuntimeFlowConnector] = Field(default_factory=list)
    topics: list[RuntimeFlowTopic] = Field(default_factory=list)
    sinks: list[RuntimeFlowSink] = Field(default_factory=list)
    message: str = ""


class MetricsSummary(BaseModel):
    """Org-wide aggregate over this org's own connectors only — see
    MonitoringService.metrics_summary.

    FE-BUG-17: `quarantine_count` (a single always-0 stub) used to mix two
    fundamentally different failure classes into one undifferentiated
    number — a Kafka Connect ingestion failure (bad serialization, a
    converter error, the connector itself erroring on a malformed source
    message) and a DataConnect quality-rule validation failure. Replaced
    with the two real, independently-sourced counts below (both windowed
    over settings.monitoring_failure_window_seconds, so they're actually
    comparable to each other)."""

    connector_running_count: int = 0
    failed_connector_count: int = 0
    failed_task_count: int = 0
    records_read_per_second: float = 0
    records_written_per_second: float = 0
    error_rate: float = 0
    #: DataConnect quality-rule failures (missing/invalid field values,
    #: ...) — see MonitoringService._validation_failure_count.
    validation_failure_count: int = 0
    #: Kafka Connect connector-DLQ records (serialization/converter/
    #: connector errors) — see MonitoringService._ingestion_failure_count.
    ingestion_failure_count: int = 0
    consumer_lag: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


class RuleFailureCount(BaseModel):
    rule: str
    count: int


class CheckFailureCount(BaseModel):
    """Failing records for one quality check (counted once per record, however many of its rules the record broke)."""

    quality_policy_id: str | None = None
    name: str
    count: int
    #: The pipelines that run this check and had failures in the window.
    pipelines: list[str] = Field(default_factory=list)


class IngestionFailureCategory(BaseModel):
    category: str  # serialization | schema_conversion | connector
    label: str
    count: int


class IngestionFailureSample(BaseModel):
    pipeline_name: str
    category: str
    stage: str | None = None
    exception_class: str | None = None
    message: str | None = None
    at: datetime | None = None


class ValidationFailureBreakdown(BaseModel):
    total: int = 0
    by_rule: list[RuleFailureCount] = Field(default_factory=list)
    #: The same failures grouped by the quality check (policy) of the pipeline that had them — what the Quality screen shows. A check can have
    #: many rules; one row per check keeps this short. Each record counts once.
    by_check: list[CheckFailureCount] = Field(default_factory=list)


class IngestionFailureBreakdown(BaseModel):
    #: Exact count of records on the connector-DLQ topics in the window.
    total: int = 0
    #: Records already counted by cause (continuous background counters).
    classified: int = 0
    #: total - classified: records the counters haven't caught up to yet (e.g.
    #: just after startup, or a DLQ topic that is still being back-filled).
    unclassified: int = 0
    by_category: list[IngestionFailureCategory] = Field(default_factory=list)
    recent: list[IngestionFailureSample] = Field(default_factory=list)


class FailureBreakdown(BaseModel):
    """FE-BUG-17: the Quality screen's two separate groups — validation
    failures (a DataConnect quality rule rejected the record) and ingestion
    failures (Kafka Connect couldn't ingest it) — each broken down by cause.
    Never summed into one number."""

    window_seconds: int
    validation: ValidationFailureBreakdown
    ingestion: IngestionFailureBreakdown


class TopicReportItem(BaseModel):
    kind: str
    topic: str
    exists: bool
    partitions: int | None = None
    replication_factor: int | None = None
    cleanup_policy: str | None = None
    min_insync_replicas: int | None = None
    problems: list[str] = Field(default_factory=list)


class PoolTopologyReport(BaseModel):
    """One worker pool's Kafka Connect internal topics checked against the runtime design — see
    app/services/connect_topology.py. `cluster_mode` is `dedicated` (the pool has its own Connect cluster, topics checked)
    or `shared` (it runs on the shared cluster, not checked)."""

    pool_id: str
    cluster_mode: str
    ok: bool
    topics: list[TopicReportItem] = Field(default_factory=list)
    note: str | None = None
    error: str | None = None


class ConnectorMetricPoint(BaseModel):
    t: float  # unix seconds
    v: float | None = None


class ConnectorMetricSeries(BaseModel):
    id: str
    label: str
    #: records/s | ms | ratio (0..1) | count | errors/s | tasks
    unit: str
    description: str = ""
    #: Latest non-empty sample in the range; None = no samples (not yet scraped, or Prometheus unavailable) — never 0.
    current: float | None = None
    series: list[ConnectorMetricPoint] = Field(default_factory=list)
    #: False when `current` is read live from somewhere other than Prometheus (the sink's Consumer lag, from Kafka offsets): there is
    #: no history, so `series` is empty by design rather than "no samples yet".
    history: bool = True


class ConnectorMetricColumn(BaseModel):
    id: str
    label: str
    unit: str


class ConnectorTaskMetrics(BaseModel):
    task: str
    #: Task status as the worker reports it over JMX (running | paused | failed | unassigned | destroyed).
    status: str | None = None
    #: Which worker pool / scrape target the task's metrics came from.
    worker: str | None = None
    values: dict[str, float | None] = Field(default_factory=dict)


class ConnectorMetrics(BaseModel):
    """GET /runtime/connectors/{name}/metrics — Prometheus-sourced Kafka Connect metrics for ONE of this org's
    connectors (app/services/connector_prometheus_metrics.py). `prometheus_enabled=False`: no Prometheus configured.
    `reachable=False` with `error`: Prometheus was configured but couldn't answer — every value is then None, never 0."""

    connector_name: str
    pipeline_id: str
    pipeline_name: str | None = None
    direction: str  # source | sink
    runtime_pool_id: str | None = None
    range: str
    step_seconds: int
    observed_at: str
    prometheus_enabled: bool
    reachable: bool
    error: str | None = None
    metrics: list[ConnectorMetricSeries] = Field(default_factory=list)
    task_columns: list[ConnectorMetricColumn] = Field(default_factory=list)
    tasks: list[ConnectorTaskMetrics] = Field(default_factory=list)


class ConnectorRate(BaseModel):
    connector_name: str
    pipeline_id: str
    pipeline_name: str | None = None
    direction: str  # source | sink
    runtime_pool_id: str | None = None
    #: records/s — a source: written to Kafka; a sink: written to the destination. None = no samples yet (never 0).
    rate: float | None = None
    #: Records the sink's consumer is behind (sinks only). None = no samples.
    lag: float | None = None
    #: Task counts as the workers report them over JMX. None = Prometheus has no task series for this connector yet.
    tasks_running: int | None = None
    tasks_failed: int | None = None
    #: Records/s that failed in convert, transform or put over the last 5 minutes — whether they were then skipped, sent to the
    #: dead-letter queue or stopped the task. A RUNNING connector whose tasks are all healthy can still be failing every record
    #: (errors.tolerance=all skips them), so this is what tells it apart from a working one. None = no samples.
    error_rate: float | None = None


class ConnectorMetricsSummary(BaseModel):
    """GET /runtime/connectors/metrics/summary — the current rate / lag / task counts of EVERY connector this org owns,
    from one Prometheus query batch (the Dashboard and the Monitoring connector list use it instead of one
    /metrics call per connector). Same honesty rules as ConnectorMetrics: `prometheus_enabled=False` when not
    configured, `reachable=False` + `error` when Prometheus can't answer — every value is then None, never 0."""

    prometheus_enabled: bool
    reachable: bool
    error: str | None = None
    observed_at: str
    connectors: list[ConnectorRate] = Field(default_factory=list)

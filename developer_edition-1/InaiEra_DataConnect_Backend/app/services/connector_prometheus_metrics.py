"""
Per-connector Kafka Connect metrics, read from Prometheus.

How the numbers get there: every Kafka Connect worker runs the Prometheus JMX exporter as a Java agent
(infra/kafka-connect.Dockerfile) with the rules in infra/jmx-exporter/kafka-connect.yml, which turn Connect's own JMX
MBeans — `connector-task-metrics`, `source-task-metrics`, `sink-task-metrics`, `task-error-metrics` and the sink
consumer's fetch-manager metrics — into Prometheus series labelled `connector` and `task`. Prometheus scrapes every
worker pool (infra/prometheus/prometheus.yml). DataConnect never scrapes workers itself; it only runs PromQL against
`settings.prometheus_url`.

The metric names below and the exporter rules are one contract: rename one, rename the other
(tests/test_connector_prometheus_metrics.py checks that every series queried here is produced by a rule).

Org scoping is the same as the rest of /runtime (app/services/runtime_ownership.py): a connector that is not one of
the calling org's own pipelines is reported as not found (None -> 404), never queried — so a PromQL label value is
always a connector name this org owns, never caller-supplied text reaching Prometheus unchecked.

Honesty rules, as on the rest of the Monitoring page:
- Prometheus not configured -> `prometheus_enabled: false`, no queries attempted.
- Prometheus unreachable / query failed -> `reachable: false` + `error`, every value None — never a zero that would
  read as "the connector is idle".
- A metric with no samples yet (connector just deployed, worker not yet scraped) -> `current: None`, empty series.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.core.error_policies import is_sink
from app.core.http_clients import get_client
from app.services.connect_runtime import pool_of
from app.services.runtime_ownership import pipeline_by_connector_name

logger = structlog.get_logger(__name__)

# range key -> (window seconds, step seconds). Steps keep every series at ~60–100 points: enough to draw, cheap to ship.
RANGES: dict[str, tuple[int, int]] = {
    "15m": (15 * 60, 15),
    "1h": (60 * 60, 60),
    "6h": (6 * 60 * 60, 300),
    "24h": (24 * 60 * 60, 900),
}
DEFAULT_RANGE = "1h"
# How far back the connector summary looks for record failures (its error_rate): long enough to cover a few Prometheus scrapes and a
# quiet minute, short enough that a connector stops being flagged soon after it stops failing.
SUMMARY_ERROR_WINDOW = "5m"


@dataclass(frozen=True)
class MetricDef:
    id: str
    label: str
    unit: str  # records/s | ms | ratio | count | errors/s | tasks
    description: str
    # PromQL with `{sel}` for the connector label selector and `{win}` for a rate window, e.g. "[2m]".
    query: str
    # Per-task form (grouped `by (task)`) for the task table; None = not shown per task.
    task_query: str | None = None
    directions: tuple[str, ...] = ("source", "sink")
    # How one chart point summarises its time bucket when the step is wider than a scrape (1h/6h/24h): a rate or ratio is
    # averaged over the bucket, a peak (latency, lag, failure %) is its maximum, a count of running tasks its minimum (a task that
    # stopped for part of the bucket still shows). Without it a 5- or 15-minute step SAMPLES one instant and a short burst — a
    # bulk read that lasts a minute — falls between points and the chart reads 0.
    over_time: str = "avg"
    # True when the value does not come from Prometheus at all (see LIVE_FROM_KAFKA): no history to chart.
    live_from_kafka: bool = False


METRICS: tuple[MetricDef, ...] = (
    # ── throughput ───────────────────────────────────────────────────────────
    MetricDef(
        "source_poll_rate", "Records polled", "records/s",
        "Records the connector's tasks read from the source system per second.",
        "sum(kafka_connect_source_task_source_record_poll_rate{sel})",
        "sum by (task) (kafka_connect_source_task_source_record_poll_rate{sel})",
        ("source",),
    ),
    MetricDef(
        "source_write_rate", "Records written to Kafka", "records/s",
        "Records per second handed to the Kafka producer after transforms (what actually reaches the topic).",
        "sum(kafka_connect_source_task_source_record_write_rate{sel})",
        "sum by (task) (kafka_connect_source_task_source_record_write_rate{sel})",
        ("source",),
    ),
    MetricDef(
        "sink_read_rate", "Records read from Kafka", "records/s",
        "Records per second the sink tasks consumed from their topics.",
        "sum(kafka_connect_sink_task_sink_record_read_rate{sel})",
        "sum by (task) (kafka_connect_sink_task_sink_record_read_rate{sel})",
        ("sink",),
    ),
    MetricDef(
        "sink_send_rate", "Records written to sink", "records/s",
        "Records per second put to the destination system after transforms.",
        "sum(kafka_connect_sink_task_sink_record_send_rate{sel})",
        "sum by (task) (kafka_connect_sink_task_sink_record_send_rate{sel})",
        ("sink",),
    ),
    # ── backlog / latency ───────────────────────────────────────────────────
    MetricDef(
        "sink_consumer_lag", "Consumer lag", "count",
        "Records the sink has not yet committed: the end offset of its topics minus its consumer group's committed offset, summed "
        "over their partitions. Read live from Kafka (Connect's own records-lag-max reads 0 whenever everything has been fetched, "
        "even while it is still being written, and stops changing while the connector is paused), so there is no history to chart.",
        "",
        "",
        ("sink",),
    ),
    MetricDef(
        "source_poll_batch_ms", "Poll batch time", "ms",
        "Average time a task takes to poll one batch from the source (slowest task).",
        "max(kafka_connect_source_task_poll_batch_avg_time_ms{sel})",
        None,
        ("source",),
    ),
    MetricDef(
        "sink_put_batch_ms", "Put batch time", "ms",
        "Average time a task takes to write one batch to the destination (slowest task).",
        "max(kafka_connect_sink_task_put_batch_avg_time_ms{sel})",
        None,
        ("sink",),
    ),
    MetricDef(
        "offset_commit_ms", "Offset commit time", "ms",
        "Average time to commit offsets (slowest task).",
        "max(kafka_connect_connector_task_offset_commit_avg_time_ms{sel})",
        None,
    ),
    # ── health ──────────────────────────────────────────────────────────────
    MetricDef(
        "running_ratio", "Running ratio", "ratio",
        "Fraction of time the tasks spent RUNNING (vs paused/failed), averaged across tasks.",
        "avg(kafka_connect_connector_task_running_ratio{sel})",
        "avg by (task) (kafka_connect_connector_task_running_ratio{sel})",
    ),
    MetricDef(
        "tasks_running", "Tasks running", "tasks",
        "Tasks currently reporting status=running on their worker.",
        'count(kafka_connect_task_status{sel_running}) or vector(0)',
        None,
    ),
    MetricDef(
        "record_error_rate", "Record errors", "errors/s",
        "Records per second that failed in convert, transform or put — whether the error policy then skipped them, sent them to the "
        "dead-letter queue or stopped the task. (From Connect's failure counter: its separate error counter does not count records the "
        "connector reports as failed during put, so a sink's failed writes never showed.)",
        "sum(rate(kafka_connect_task_error_total_record_failures{sel}{win}))",
        None,
    ),
    MetricDef(
        "record_failures", "Failed records", "count",
        "Records that failed processing since the task started (cumulative).",
        "sum(kafka_connect_task_error_total_record_failures{sel})",
        "sum by (task) (kafka_connect_task_error_total_record_failures{sel})",
    ),
    MetricDef(
        "dlq_produced", "Sent to DLQ", "count",
        "Records the connector wrote to its dead-letter queue since the task started (cumulative).",
        "sum(kafka_connect_task_error_deadletterqueue_produce_requests{sel})",
        None,
        ("sink",),
    ),
    MetricDef(
        "offset_commit_failure_pct", "Offset commit failures", "ratio",
        "Share of offset commit attempts that failed (worst task).",
        "max(kafka_connect_connector_task_offset_commit_failure_percentage{sel})",
        None,
    ),
)

_OVER_TIME = {
    "sink_consumer_lag": "max", "source_poll_batch_ms": "max", "sink_put_batch_ms": "max", "offset_commit_ms": "max",
    "tasks_running": "min", "record_failures": "max", "dlq_produced": "max", "offset_commit_failure_pct": "max",
}
METRICS = tuple(
    MetricDef(**{**m.__dict__, "over_time": _OVER_TIME.get(m.id, "avg"), "live_from_kafka": m.id in ("sink_consumer_lag",)}) for m in METRICS
)

#: The exact Prometheus series names this module queries — the contract with infra/jmx-exporter/kafka-connect.yml.
QUERIED_SERIES: frozenset[str] = frozenset({
    "kafka_connect_source_task_source_record_poll_rate",
    "kafka_connect_source_task_source_record_write_rate",
    "kafka_connect_source_task_poll_batch_avg_time_ms",
    "kafka_connect_sink_task_sink_record_read_rate",
    "kafka_connect_sink_task_sink_record_send_rate",
    "kafka_connect_sink_task_put_batch_avg_time_ms",
    "kafka_connect_connector_task_offset_commit_avg_time_ms",
    "kafka_connect_connector_task_offset_commit_failure_percentage",
    "kafka_connect_connector_task_running_ratio",
    "kafka_connect_task_status",
    "kafka_connect_task_error_total_record_failures",
    "kafka_connect_task_error_deadletterqueue_produce_requests",
})


def promql_string(value: str) -> str:
    """A PromQL double-quoted string literal for `value` (backslash, quote and newline escaped)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def render_query(template: str, connector_name: str, rate_window_seconds: int) -> str:
    name = promql_string(connector_name)
    return (
        template
        .replace("{sel_running}", "{connector=" + name + ',status="running"}')
        .replace("{sel}", "{connector=" + name + "}")
        .replace("{win}", f"[{rate_window_seconds}s]")
    )


def metrics_for(direction: str) -> list[MetricDef]:
    return [m for m in METRICS if direction in m.directions]


def _num(raw: Any) -> float | None:
    """Prometheus sends sample values as strings; NaN/Inf (e.g. a ratio with no denominator yet) become None."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class PrometheusError(Exception):
    pass


class PrometheusClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict]:
        client = await get_client(self.base_url)
        try:
            resp = await client.get(path, params=params, timeout=settings.prometheus_query_timeout_seconds)
        except httpx.TimeoutException as exc:
            raise PrometheusError(
                f"Prometheus did not answer within {settings.prometheus_query_timeout_seconds:g}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise PrometheusError("Prometheus could not be reached.") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise PrometheusError(f"Prometheus returned a non-JSON response (HTTP {resp.status_code}).") from exc
        if resp.status_code >= 400 or body.get("status") != "success":
            # errorType/error are PromQL diagnostics (bad_data, timeout, ...) — they carry no credentials.
            detail = body.get("error") or f"HTTP {resp.status_code}"
            raise PrometheusError(f"Prometheus query failed: {detail}")
        return (body.get("data") or {}).get("result") or []

    async def instant(self, query: str) -> list[dict]:
        return await self._get("/api/v1/query", {"query": query})

    async def range(self, query: str, start: float, end: float, step: int) -> list[dict]:
        return await self._get("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ConnectorPrometheusMetrics:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._summary_cache: dict[str, tuple[float, dict]] = {}

    def _client(self) -> PrometheusClient:
        return PrometheusClient(settings.prometheus_url)

    async def connector_metrics(self, org_id: str, connector_name: str, range_key: str = DEFAULT_RANGE) -> dict | None:
        """None when the connector isn't one of this org's pipelines (the route 404s)."""
        pipeline = (await pipeline_by_connector_name(org_id)).get(connector_name)
        if pipeline is None:
            return None
        if range_key not in RANGES:
            raise ValueError(f"Unknown range {range_key!r}; expected one of {', '.join(RANGES)}.")

        window, step = RANGES[range_key]
        direction = "sink" if is_sink(pipeline.get("direction")) else "source"
        defs = metrics_for(direction)
        base = {
            "connector_name": connector_name,
            "pipeline_id": pipeline["id"],
            "pipeline_name": pipeline.get("name"),
            "direction": direction,
            "runtime_pool_id": pool_of(pipeline),
            "range": range_key,
            "step_seconds": step,
            "observed_at": _now_iso(),
            "prometheus_enabled": bool(settings.prometheus_url),
            "reachable": False,
            "error": None,
            "metrics": [
                {"id": m.id, "label": m.label, "unit": m.unit, "description": m.description, "current": None, "series": [],
                 "history": not m.live_from_kafka}
                for m in defs
            ],
            # The live Kafka backlog has no per-task PromQL but is still a task column (filled for a single-task sink, see _fill_backlog).
            "task_columns": [{"id": m.id, "label": m.label, "unit": m.unit} for m in defs if m.task_query or m.live_from_kafka],
            "tasks": [],
        }
        if not settings.prometheus_url:
            base["error"] = "Prometheus is not configured for this deployment (DATACONNECT_PROMETHEUS_URL)."
            return base

        cache_key = (connector_name, range_key)
        cached = self._cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < settings.prometheus_metrics_cache_ttl_seconds:
            return {**cached[1], "pipeline_name": pipeline.get("name")}

        try:
            result = await self._query_all(connector_name, defs, window, step)
        except PrometheusError as exc:
            logger.warning("monitoring.prometheus_query_failed", connector=connector_name, error=str(exc))
            base["error"] = str(exc)
            return base  # not cached: the next poll retries straight away

        base.update(result, reachable=True)
        if direction == "sink":
            await self._fill_backlog(org_id, connector_name, base)
        self._cache[cache_key] = (time.monotonic(), base)
        return base

    async def _fill_backlog(self, org_id: str, connector_name: str, payload: dict) -> None:
        """The sink's Consumer lag, from Kafka committed offsets (see the metric's description). One task: it is that task's
        backlog too. Several tasks: the per-task split is not known from offsets alone, so the task column stays empty."""
        from app.services.runtime_metrics import sink_backlogs

        backlogs = await sink_backlogs(org_id)
        lag = None if backlogs is None else backlogs.get(connector_name)
        for metric in payload["metrics"]:
            if metric["id"] == "sink_consumer_lag":
                metric["current"] = float(lag) if lag is not None else None
        if len(payload["tasks"]) == 1:
            payload["tasks"][0]["values"]["sink_consumer_lag"] = float(lag) if lag is not None else None

    async def summary(self, org_id: str) -> dict:
        """Current rate / lag / task counts of every connector this org owns (see schemas.ConnectorMetricsSummary).

        Five instant queries grouped `by (connector)` for the whole cluster, then filtered to this org's own
        connector names — nothing about another org's connectors ever leaves this function."""
        owned = await pipeline_by_connector_name(org_id)
        base: dict[str, Any] = {
            "prometheus_enabled": bool(settings.prometheus_url), "reachable": False, "error": None,
            "observed_at": _now_iso(), "connectors": [],
        }

        def row(name: str, pipeline: dict, **values: Any) -> dict:
            return {
                "connector_name": name, "pipeline_id": pipeline["id"], "pipeline_name": pipeline.get("name"),
                "direction": "sink" if is_sink(pipeline.get("direction")) else "source",
                "runtime_pool_id": pool_of(pipeline), "rate": None, "lag": None, "tasks_running": None,
                "tasks_failed": None, "error_rate": None, **values,
            }

        base["connectors"] = [row(n, p) for n, p in owned.items()]
        if not settings.prometheus_url:
            base["error"] = "Prometheus is not configured for this deployment (DATACONNECT_PROMETHEUS_URL)."
            return base
        cached = self._summary_cache.get(org_id)
        if cached and (time.monotonic() - cached[0]) < settings.prometheus_metrics_cache_ttl_seconds:
            return cached[1]

        client = self._client()
        try:
            source_rate, sink_rate, running, failed, total, errors = await asyncio.gather(
                client.instant("sum by (connector) (kafka_connect_source_task_source_record_write_rate)"),
                client.instant("sum by (connector) (kafka_connect_sink_task_sink_record_send_rate)"),
                client.instant('count by (connector) (kafka_connect_task_status{status="running"})'),
                client.instant('count by (connector) (kafka_connect_task_status{status="failed"})'),
                client.instant("count by (connector) (kafka_connect_task_status)"),
                # The same failure counter as the panel's "Record errors" tile, over 5 minutes: a connector skipping every record
                # still reports RUNNING tasks, so the list and the Dashboard need this to tell it from a healthy one.
                client.instant(f"sum by (connector) (rate(kafka_connect_task_error_total_record_failures[{SUMMARY_ERROR_WINDOW}]))"),
            )
        except PrometheusError as exc:
            logger.warning("monitoring.prometheus_summary_failed", org_id=org_id, error=str(exc))
            base["error"] = str(exc)
            return base  # not cached: the next poll retries straight away

        def by_connector(result: list[dict]) -> dict[str, float | None]:
            return {
                item.get("metric", {}).get("connector"): _num((item.get("value") or [None, None])[1]) for item in result
            }

        from app.services.runtime_metrics import sink_backlogs

        src, snk = by_connector(source_rate), by_connector(sink_rate)
        lg = await sink_backlogs(org_id) or {}                 # the real backlog, from Kafka committed offsets (not JMX records-lag-max)
        run, fail, tot, err = by_connector(running), by_connector(failed), by_connector(total), by_connector(errors)
        connectors = []
        for name, pipeline in owned.items():
            is_sink_conn = is_sink(pipeline.get("direction"))
            has_tasks = tot.get(name) is not None
            connectors.append(row(
                name, pipeline,
                rate=(snk if is_sink_conn else src).get(name),
                lag=(float(lg[name]) if name in lg else None) if is_sink_conn else None,
                tasks_running=int(run.get(name) or 0) if has_tasks else None,
                tasks_failed=int(fail.get(name) or 0) if has_tasks else None,
                error_rate=(err.get(name) or 0.0) if has_tasks else None,
            ))
        base.update(reachable=True, connectors=connectors, observed_at=_now_iso())
        self._summary_cache[org_id] = (time.monotonic(), base)
        return base

    async def _query_all(self, connector_name: str, defs: list[MetricDef], window: int, step: int) -> dict:
        client = self._client()
        end = time.time()
        start = end - window
        # Rate over at least 2 scrapes, and never narrower than one step (otherwise points between steps are skipped).
        rate_window = max(60, step * 2)
        scrape = int(settings.prometheus_scrape_interval_seconds)
        # A step wider than a scrape (1h, 6h, 24h) would SAMPLE one instant per point: summarise each bucket instead, and read the
        # tile's "current" value from an instant query so it is still the latest reading, not a bucket average.
        bucketed = step > scrape
        charted = [m for m in defs if not m.live_from_kafka]

        def range_query(m: MetricDef) -> str:
            q = render_query(m.query, connector_name, rate_window)
            return f"{m.over_time}_over_time(({q})[{step}s:{scrape}s])" if bucketed else q

        range_calls = [client.range(range_query(m), start, end, step) for m in charted]
        current_calls = [client.instant(render_query(m.query, connector_name, rate_window)) for m in charted] if bucketed else []
        task_defs = [m for m in defs if m.task_query]
        task_calls = [client.instant(render_query(m.task_query or "", connector_name, rate_window)) for m in task_defs]
        status_call = client.instant(render_query("kafka_connect_task_status{sel}", connector_name, rate_window))

        results = await asyncio.gather(*range_calls, *current_calls, *task_calls, status_call)
        range_results = results[: len(charted)]
        current_results = results[len(charted): len(charted) + len(current_calls)]
        task_results = results[len(charted) + len(current_calls): len(charted) + len(current_calls) + len(task_defs)]
        status_result = results[-1]

        by_id: dict[str, tuple[list[dict], float | None]] = {}
        for i, (m, res) in enumerate(zip(charted, range_results, strict=True)):
            # Aggregated queries return at most one series.
            points = [{"t": float(t), "v": _num(v)} for t, v in res[0].get("values", [])] if res else []
            if bucketed:
                inst = current_results[i]
                current = _num((inst[0].get("value") or [None, None])[1]) if inst else None
            else:
                current = next((p["v"] for p in reversed(points) if p["v"] is not None), None)
            by_id[m.id] = (points, current)
        metrics = []
        for m in defs:
            points, current = by_id.get(m.id, ([], None))
            metrics.append({
                "id": m.id, "label": m.label, "unit": m.unit, "description": m.description,
                "current": current, "series": points, "history": not m.live_from_kafka,
            })

        tasks: dict[str, dict] = {}
        for item in status_result:
            labels = item.get("metric", {})
            task = str(labels.get("task", "?"))
            tasks.setdefault(task, {"task": task, "status": None, "worker": None, "values": {}})
            tasks[task]["status"] = labels.get("status")
            tasks[task]["worker"] = labels.get("pool") or labels.get("instance")
        for m, res in zip(task_defs, task_results, strict=True):
            if m.live_from_kafka:
                continue
            for item in res:
                task = str(item.get("metric", {}).get("task", "?"))
                row = tasks.setdefault(task, {"task": task, "status": None, "worker": None, "values": {}})
                row["values"][m.id] = _num((item.get("value") or [None, None])[1])

        ordered = sorted(tasks.values(), key=lambda r: (not r["task"].isdigit(), int(r["task"]) if r["task"].isdigit() else 0, r["task"]))
        return {"metrics": metrics, "tasks": ordered, "observed_at": _now_iso()}


connector_prometheus_metrics = ConnectorPrometheusMetrics()

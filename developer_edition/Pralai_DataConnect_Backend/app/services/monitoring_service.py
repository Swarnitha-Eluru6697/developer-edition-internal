"""
Live connector/task/worker-pool monitoring.

Single runtime truth model: every status this service returns comes from
asking Kafka Connect directly, through the same KafkaConnectClient the
runtime reconciler uses (app/services/runtime_orchestrator.py) and
interpreted with the same derive_observed_state() the reconciler uses
(app/services/runtime_reconciler.py) — so a connector can never show
"healthy"/RUNNING on one screen and "failed" on another depending on which
code path answered.

Responses are cached briefly per connector purely to avoid hammering Connect
on every dashboard poll (see settings.monitoring_cache_ttl_seconds); every
response carries observed_at + runtime_endpoint so staleness is visible. When
Kafka Connect can't be reached, or hasn't been asked yet, or the connector
isn't found there, the observed state is "unknown"/UNKNOWN — never RUNNING or
healthy. A real error/trace is always attached instead of being swallowed.

Org scoping: settings.kafka_connect_url is one shared Connect cluster for
every org (and possibly for applications outside DataConnect entirely) — it
was previously queried and returned as-is, with no cross-reference against
which connectors this org's own pipelines actually created. Every method
below now takes an `org_id` and filters through
app/services/runtime_ownership.py's connector_name/topic_name -> pipeline
maps before anything reaches the caller, and every per-connector result
includes the `pipeline_id`/`topic_name` it maps back to. `_list_connectors`
and `_fetch_status` are the two exceptions — they still talk to the whole
cluster and cache cluster-wide (org filtering happens in their callers,
never inside them), because the raw connector list and a single connector's
raw status are cheap to share across every org's requests; nothing derived
from them reaches a response before being filtered/attributed to an org.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import settings
from app.core.connect_pools import POOLS, canonical_pool_id
from app.services.connect_runtime import client_for_pool, endpoints, pool_endpoint, pool_of
from app.services.connector_dlq_reader import read_recent_connector_errors
from app.services.connector_error_classifier import (
    CATEGORY_LABELS,
    EXCEPTION_CLASS_HEADER,
    EXCEPTION_MESSAGE_HEADER,
    STAGE_HEADER,
    classify_connector_error,
)
from app.services.dlq_attribution import (
    CONNECTOR_NAME_HEADER,
    DlqAttribution,
    pipelines_by_dlq_topic,
)
from app.services.error_threshold import count_recent_connector_errors
from app.services.failure_counters import (
    INGESTION,
    TOTAL_KEY,
    VALIDATION,
    windowed_counts,
    windowed_counts_by_pipeline,
)
from app.services.runtime_orchestrator import KafkaConnectClient
from app.services.runtime_ownership import org_pipelines, pipeline_by_connector_name
from app.services.runtime_reconciler import derive_observed_state
from app.services.safe_error import to_safe_error
from app.services.secrets import sanitize_probe_message

_RECENT_ERRORS_PER_TOPIC = 10

logger = structlog.get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MonitoringService:
    def __init__(self) -> None:
        # Per Connect CLUSTER (runtime design: each worker pool may be its own cluster, app/services/connect_runtime.py),
        # keyed by the cluster's URL — several pools sharing one cluster share one entry.
        self._connectors_cache: dict[str, tuple[float, list[str]]] = {}
        self._status_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        # (org, window) -> (when, {dlq topic: records in the window}); see _dlq_counts.
        self._dlq_counts_cache: dict[tuple[str, int], tuple[float, dict[str, int]]] = {}
        self._dlq_counts_locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _client(self, pool_id: str | None = None) -> KafkaConnectClient:
        return client_for_pool(pool_id)

    def _fresh(self, cached_at: float) -> bool:
        return (time.monotonic() - cached_at) < settings.monitoring_cache_ttl_seconds

    async def _list_connectors(self, pool_id: str | None = None) -> tuple[list[str] | None, str | None]:
        """Cached list of connector names the pool's Kafka Connect cluster currently knows about.
        (None, error) when Connect can't be reached — never an empty-looking
        success that would be mistaken for "no connectors"."""
        endpoint = pool_endpoint(pool_id)
        cached = self._connectors_cache.get(endpoint)
        if cached and self._fresh(cached[0]):
            return cached[1], None
        try:
            names = await self._client(pool_id).list_connectors()
        except Exception as exc:
            logger.warning("monitoring.connect_unreachable", pool=pool_id, endpoint=endpoint, error=str(exc))
            return None, to_safe_error(exc, event="monitoring.connect_unreachable_detail", target="kafka_connect").describe()
        self._connectors_cache[endpoint] = (time.monotonic(), names)
        return names, None

    async def _fetch_status(self, connector_name: str, pool_id: str | None = None) -> dict[str, Any]:
        """The one place that turns a Kafka Connect response into our runtime-truth
        shape. Cached briefly per connector. UNKNOWN (never RUNNING) whenever real
        state can't be obtained, with the reason carried in `error`/task `trace`."""
        endpoint = pool_endpoint(pool_id)
        cached = self._status_cache.get((endpoint, connector_name))
        if cached and self._fresh(cached[0]):
            return cached[1]

        observed_at = _now()
        names, error = await self._list_connectors(pool_id)
        raw_status: dict[str, Any] | None = None
        connector_exists = bool(names) and connector_name in (names or [])

        if error is None and connector_exists:
            try:
                raw_status = await self._client(pool_id).get_status(connector_name)
            except Exception as exc:
                error = str(exc)
        elif error is None and not connector_exists:
            error = f"Connector '{connector_name}' not found in Kafka Connect."

        observed_state = "unknown" if error else derive_observed_state(connector_exists, raw_status)
        tasks = [
            {
                "id": t.get("id"),
                "state": t.get("state", "UNKNOWN"),
                "worker_id": t.get("worker_id"),
                "trace": t.get("trace"),
            }
            for t in (raw_status or {}).get("tasks", [])
        ]
        result = {
            "name": connector_name,
            "connector": (raw_status or {}).get("connector", {"state": "UNKNOWN"}),
            "tasks": tasks,
            "observed_state": observed_state,
            "observed_at": observed_at,
            "runtime_endpoint": endpoint,
            "error": error,
        }
        self._status_cache[(endpoint, connector_name)] = (time.monotonic(), result)
        return result

    async def list_connect_clusters(self, org_id: str) -> list[dict]:
        """The platform's worker pools (app/core/connect_pools.py) — fixed, global infrastructure, the same six for every
        org — each with the Connect cluster it runs on. Each pool's `status` and `connector_count` reflect only this
        org's own connectors in it, never other orgs'. A pool this org has no connectors in is `idle`."""
        pipelines = [p for p in await org_pipelines(org_id) if p.get("connector_name")]
        sharing = endpoints()
        out = []
        for spec in POOLS:
            endpoint = pool_endpoint(spec.id)
            pool = {
                "id": spec.id, "name": spec.name, "pool_type": spec.short, "workload": spec.workload,
                "runtime_endpoint": endpoint, "shared_cluster": len(sharing[endpoint]) > 1,
            }
            connectors = [p for p in pipelines if pool_of(p) == spec.id]
            pool["connector_count"] = len(connectors)
            if not connectors:
                pool["status"] = "idle"
                pool["observed_at"] = _now()
                out.append(pool)
                continue
            statuses = [await self._fetch_status(p["connector_name"], spec.id) for p in connectors]
            if any(s["error"] for s in statuses):
                pool["status"] = "unknown"
            elif any(s["observed_state"] == "failed" for s in statuses):
                pool["status"] = "degraded"
            elif all(s["observed_state"] == "running" for s in statuses):
                pool["status"] = "healthy"
            else:
                pool["status"] = "unknown"
            pool["observed_at"] = _now()
            out.append(pool)
        return out

    async def list_cluster_connectors(self, org_id: str, cluster_id: str) -> list[dict]:
        all_pipelines = await org_pipelines(org_id)
        pool_id = canonical_pool_id(cluster_id)
        pipelines = [p for p in all_pipelines if pool_id is not None and pool_of(p) == pool_id]
        out = []
        for pipeline in pipelines:
            status = await self._fetch_status(pipeline["connector_name"], pool_id)
            out.append(
                {
                    "pipeline_id": pipeline["id"],
                    "connector_name": pipeline["connector_name"],
                    "topic_name": pipeline.get("topic_name"),
                    "runtime_pool_id": pool_id,
                    "observed_state": status["observed_state"],
                    "observed_at": status["observed_at"],
                    "runtime_error": status["error"],
                }
            )
        return out

    async def connector_status(self, org_id: str, connector_name: str) -> dict | None:
        """None means this connector doesn't belong to any of this org's
        pipelines — the route 404s rather than confirming/denying whether
        it exists under another org or another application entirely."""
        pipeline = (await pipeline_by_connector_name(org_id)).get(connector_name)
        if pipeline is None:
            return None
        status = await self._fetch_status(connector_name, pool_of(pipeline))
        return {**status, "pipeline_id": pipeline["id"], "topic_name": pipeline.get("topic_name")}

    async def connector_tasks(self, org_id: str, connector_name: str) -> list[dict] | None:
        pipeline = (await pipeline_by_connector_name(org_id)).get(connector_name)
        if pipeline is None:
            return None
        status = await self._fetch_status(connector_name, pool_of(pipeline))
        return [
            {
                "connector": connector_name,
                "pipeline_id": pipeline["id"],
                "observed_at": status["observed_at"],
                **task,
            }
            for task in status["tasks"]
        ]

    async def _ingestion_failure_count(self, org_id: str, window_seconds: float) -> int:
        """FE-BUG-17: sum, across this org's own pipelines, of records that
        landed on each pipeline's connector-DLQ topic (Kafka Connect's own
        dead-letter queue — bad serialization, a converter error, the
        connector itself erroring on a malformed source message) within the
        window. This is a fundamentally different failure class from
        validation_failure_count (a DataConnect quality-rule failure) — see
        topic_naming.py's build_connector_quarantine_topic_name/
        build_validation_quarantine_topic_name docstrings for the GAP-03
        split this reads from. A pipeline that's never been deployed has no
        connector_name and is skipped, not counted as zero errors on a
        topic that was never even created."""
        return sum((await self._dlq_counts(org_id, window_seconds)).values())

    async def _dlq_counts(self, org_id: str, window_seconds: float) -> dict[str, int]:
        """{connector-DLQ topic: records that landed on it in the window}, for every DLQ topic this org has.

        Each topic costs a Kafka round trip, and an org can have dozens (one per sink pipeline), so this is read
        concurrently (settings.monitoring_dlq_read_concurrency at a time, never one after another) and shared: the
        summary and failure-breakdown endpoints, and every open tab polling them, reuse one scan for
        settings.monitoring_dlq_cache_ttl_seconds. Concurrent callers wait for the scan already running instead of
        starting their own. A failed scan raises and is not cached — "couldn't find out" is never shown as zero."""
        window = int(window_seconds)
        key = (org_id, window)
        cached = self._dlq_counts_cache.get(key)
        if cached and (time.monotonic() - cached[0]) < settings.monitoring_dlq_cache_ttl_seconds:
            return cached[1]
        async with self._dlq_counts_locks.setdefault(key, asyncio.Lock()):
            cached = self._dlq_counts_cache.get(key)
            if cached and (time.monotonic() - cached[0]) < settings.monitoring_dlq_cache_ttl_seconds:
                return cached[1]
            topics = list(await self._dlq_topics(org_id))
            gate = asyncio.Semaphore(max(1, settings.monitoring_dlq_read_concurrency))

            async def _one(topic: str) -> int:
                async with gate:
                    return await count_recent_connector_errors(topic, window)

            counts = dict(zip(topics, await asyncio.gather(*(_one(t) for t in topics)), strict=True))
            self._dlq_counts_cache[key] = (time.monotonic(), counts)
            return counts

    async def _validation_failure_count(self, org_id: str, window_seconds: float) -> int:
        """FE-BUG-17: the OTHER half of the split — how many records failed
        a DataConnect quality-rule check (a missing customer_id, an invalid
        email, a status outside its allowed values) in the window.

        Read from the exact per-minute failure counters QualityStreamWorker
        increments for every failing record (see models/failure_count.py), not
        from the bounded sample_failures ring buffer — so a pipeline failing
        faster than the buffer holds is no longer undercounted. Accurate to
        within a minute at the window's edge, and comparable to
        _ingestion_failure_count's own windowed count."""
        counts = await windowed_counts(org_id, VALIDATION, window_seconds)
        return counts.get(TOTAL_KEY, 0)

    async def _validation_breakdown(self, org_id: str, window_seconds: float, pipeline_id: str | None = None) -> dict:
        """Windowed validation failures grouped by the rule that rejected the
        record (a record that broke two rules counts once under each, so the
        rule counts can add up to more than `total`)."""
        counts = await windowed_counts(org_id, VALIDATION, window_seconds, pipeline_id)
        total = counts.pop(TOTAL_KEY, 0)
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            "total": total,
            "by_rule": [{"rule": rule, "count": count} for rule, count in ranked],
            "by_check": await self._validation_by_check(org_id, window_seconds, pipeline_id),
        }

    async def _validation_by_check(self, org_id: str, window_seconds: float, pipeline_id: str | None = None) -> list[dict]:
        """Failing records grouped by the QUALITY CHECK of the pipeline that had them (a pipeline carries one check, which may hold any number of
        rules — grouping by rule would make a long list). A record counts once however many rules it broke. Pipelines sharing a check add up under
        its name; a pipeline whose check was since removed is listed as "(removed quality check)"."""
        from app.services.quality_service import quality_service

        per_pipeline = await windowed_counts_by_pipeline(org_id, VALIDATION, TOTAL_KEY, window_seconds)
        if pipeline_id is not None:
            per_pipeline = {k: v for k, v in per_pipeline.items() if k == pipeline_id}
        if not per_pipeline:
            return []
        pipelines = {p["id"]: p for p in await org_pipelines(org_id)}
        checks = {c["id"]: c for c in await quality_service.list_policies(org_id)}
        grouped: dict[str | None, dict] = {}
        for pid, count in per_pipeline.items():
            pipeline = pipelines.get(pid)
            check_id = (pipeline or {}).get("quality_policy_id")
            check = checks.get(check_id) if check_id else None
            key = check["id"] if check else None
            entry = grouped.setdefault(key, {"quality_policy_id": key, "name": check["name"] if check else "(removed quality check)", "count": 0, "pipelines": []})
            entry["count"] += count
            if pipeline and pipeline["name"] not in entry["pipelines"]:
                entry["pipelines"].append(pipeline["name"])
        return sorted(grouped.values(), key=lambda e: (-e["count"], e["name"]))

    async def _dlq_topics(self, org_id: str) -> dict[str, list[dict]]:
        """{connector-DLQ topic: EVERY pipeline that writes it}. Only a SINK pipeline whose policy keeps bad records on a dead-letter queue writes
        one — Kafka Connect gives a source connector none (see app/core/error_policies.py). Create-time validation keeps NEW sinks from sharing a
        topic, but one can still be shared (older pipelines), so a topic maps to a LIST and whose failure a record is comes from the record itself
        (app/services/dlq_attribution.py), never from "the first pipeline"."""
        return pipelines_by_dlq_topic({**pipeline, "org_id": org_id} for pipeline in await org_pipelines(org_id))

    async def _ingestion_breakdown(self, org_id: str, window_seconds: float, pipeline_id: str | None = None) -> dict:
        """Windowed connector-DLQ records. `total` is the exact offset-delta
        count; the per-cause numbers come from the exact per-minute counters
        DlqCounterManager keeps for every DLQ record (see
        dlq_failure_counter.py) — not a sample. `unclassified` is only how far
        those counters still lag the topic. `recent` is a small look at the
        newest records' error details, for display only.

        With `pipeline_id` the numbers are that one pipeline's: its own exact counters (each DLQ record is counted under the pipeline whose
        connector wrote it, so a topic shared by several pipelines is split correctly), and only its own recent records. `total` is then those
        counters' sum, because a topic's offset-delta total cannot be split per pipeline without reading every record."""
        recent: list[dict] = []
        counts = await self._dlq_counts(org_id, window_seconds)
        total = sum(counts.values())
        topics = await self._dlq_topics(org_id)
        names = {p["id"]: p.get("name") or p["id"] for pipelines in topics.values() for p in pipelines}
        gate = asyncio.Semaphore(max(1, settings.monitoring_dlq_read_concurrency))

        async def _read(topic: str) -> list[dict]:
            async with gate:
                return await read_recent_connector_errors(topic, int(window_seconds), limit=_RECENT_ERRORS_PER_TOPIC)

        # Only a topic that actually holds records has anything to look at; an empty one costs no read at all.
        busy = [
            topic for topic, count in counts.items()
            if count > 0 and topic in topics and (pipeline_id is None or any(p["id"] == pipeline_id for p in topics[topic]))
        ]
        for topic, records in zip(busy, await asyncio.gather(*(_read(t) for t in busy)), strict=True):
            attribution = DlqAttribution(topics[topic])
            for record in records:
                headers = record["headers"]
                owner_id = attribution.pipeline_id_for(headers)
                if pipeline_id is not None and owner_id != pipeline_id:
                    continue                                    # another pipeline's failure on a shared topic
                timestamp_ms = record.get("timestamp_ms")
                recent.append({
                    # The pipeline the RECORD belongs to. One that cannot be tied to any pipeline is shown under the connector that wrote it
                    # (a deleted pipeline's leftovers) rather than under whichever pipeline happens to share the topic.
                    "pipeline_name": names.get(owner_id) or headers.get(CONNECTOR_NAME_HEADER) or "Unknown pipeline",
                    "category": classify_connector_error(headers),
                    "stage": headers.get(STAGE_HEADER) or None,
                    "exception_class": headers.get(EXCEPTION_CLASS_HEADER) or None,
                    # The message can embed a connection URI with credentials —
                    # scrubbed and length-capped like every other message this
                    # app shows back from a source system.
                    "message": sanitize_probe_message(headers.get(EXCEPTION_MESSAGE_HEADER) or "") or None,
                    "at": datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC) if timestamp_ms else None,
                })
        recent.sort(key=lambda r: r["at"] or datetime.min.replace(tzinfo=UTC), reverse=True)

        by_category = await windowed_counts(org_id, INGESTION, window_seconds, pipeline_id)
        classified = sum(by_category.values())
        if pipeline_id is not None:
            total = classified
        return {
            "total": total,
            "classified": classified,
            "unclassified": max(0, total - classified),
            "by_category": [
                {"category": category, "label": CATEGORY_LABELS.get(category, category), "count": count}
                for category, count in sorted(by_category.items(), key=lambda kv: -kv[1])
            ],
            "recent": recent[:20],
        }

    async def failure_breakdown(self, org_id: str, pipeline_id: str | None = None, window_seconds: int | None = None) -> dict:
        """FE-BUG-17: the Quality screen's two groups, each broken down by
        cause — never summed together. `pipeline_id` narrows both groups to one pipeline (NEW-04); `window_seconds`
        chooses how far back to count (default: the configured monitoring window)."""
        window = float(window_seconds) if window_seconds else settings.monitoring_failure_window_seconds
        return {
            "window_seconds": int(window),
            "validation": await self._validation_breakdown(org_id, window, pipeline_id),
            "ingestion": await self._ingestion_breakdown(org_id, window, pipeline_id),
        }

    async def metrics_summary(self, org_id: str) -> dict:
        observed_at = _now()
        window = settings.monitoring_failure_window_seconds
        owned = await pipeline_by_connector_name(org_id)
        # One list call per Connect cluster this org's connectors actually live on (runtime design: a pool may be its own
        # cluster). Any cluster being unreachable makes the whole summary "unreachable" rather than silently partial.
        clusters: dict[str, str | None] = {}
        for pipeline in owned.values():
            clusters.setdefault(pool_endpoint(pool_of(pipeline)), pool_of(pipeline))
        clusters = clusters or {pool_endpoint(None): None}
        names: list[str] | None = []
        errors: list[str] = []
        for pool_id in clusters.values():
            cluster_names, cluster_error = await self._list_connectors(pool_id)
            if cluster_error is not None:
                errors.append(cluster_error)
            else:
                names.extend(cluster_names or [])
        error = errors[0] if errors else None
        # FE-BUG-17: validation failures and ingestion failures are counted
        # (and shown) independently of whether Kafka Connect itself could be
        # reached — a quality-rule failure is a DataConnect-side fact that
        # doesn't depend on Connect being up, and an already-unreachable
        # Connect is exactly when knowing "how many records recently landed
        # in a DLQ" (from before it went unreachable) still matters most.
        validation_failure_count = await self._validation_failure_count(org_id, window)
        ingestion_failure_count = await self._ingestion_failure_count(org_id, window)
        if error is not None:
            return {
                "connector_running_count": 0,
                "failed_connector_count": 0,
                "failed_task_count": 0,
                "records_read_per_second": 0,
                "records_written_per_second": 0,
                "error_rate": 0,
                "validation_failure_count": validation_failure_count,
                "ingestion_failure_count": ingestion_failure_count,
                "consumer_lag": 0,
                "extra": {
                    "observed_at": observed_at,
                    "runtime_endpoint": settings.kafka_connect_url,
                    "error": error,
                    "unreachable_clusters": len(errors),
                },
            }
        # Never aggregate over the whole cluster's connectors — only the
        # ones this org's own pipelines created.
        org_names = [name for name in names if name in owned]
        statuses = [await self._fetch_status(name, pool_of(owned[name])) for name in org_names]
        summary = self.aggregate_connector_statuses(statuses)
        return {
            **summary,
            "records_read_per_second": 0,
            "records_written_per_second": 0,
            "error_rate": 0,
            "validation_failure_count": validation_failure_count,
            "ingestion_failure_count": ingestion_failure_count,
            "consumer_lag": 0,
            "extra": {
                "observed_at": observed_at,
                "runtime_endpoint": settings.kafka_connect_url,
                "errors": [s["error"] for s in statuses if s["error"]],
            },
        }

    def aggregate_connector_statuses(self, statuses: list[dict]) -> dict:
        failed_connectors = 0
        failed_tasks = 0
        running_connectors = 0
        for status in statuses:
            connector_state = status.get("connector", {}).get("state")
            if connector_state == "RUNNING":
                running_connectors += 1
            elif connector_state in {"FAILED", "UNASSIGNED"}:
                failed_connectors += 1
            failed_tasks += len(
                [task for task in status.get("tasks", []) if task.get("state") == "FAILED"]
            )
        return {
            "connector_running_count": running_connectors,
            "failed_connector_count": failed_connectors,
            "failed_task_count": failed_tasks,
        }

    def summarize_pipeline_runs(self, runs: list[dict]) -> dict:
        records_read = sum(run.get("records_read", 0) for run in runs)
        records_written = sum(run.get("records_written", 0) for run in runs)
        records_failed = sum(run.get("records_failed", 0) for run in runs)
        total = records_written + records_failed
        return {
            "records_read": records_read,
            "records_written": records_written,
            "records_failed": records_failed,
            "error_rate": records_failed / total if total else 0,
        }


monitoring_service = MonitoringService()

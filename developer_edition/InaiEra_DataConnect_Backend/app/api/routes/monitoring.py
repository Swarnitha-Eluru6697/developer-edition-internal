from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.monitoring import (
    ConnectCluster,
    ConnectorMetrics,
    ConnectorMetricsSummary,
    ConnectorStatus,
    ConnectorTask,
    FailureBreakdown,
    MetricsSummary,
    PoolTopologyReport,
    RuntimeConnector,
    RuntimeFlow,
)
from app.services.connect_topology import verify_topology
from app.services.connector_prometheus_metrics import DEFAULT_RANGE, connector_prometheus_metrics
from app.services.monitoring_service import monitoring_service
from app.services.runtime_metrics import flow_summary

router = APIRouter()


@router.get("/flow", response_model=RuntimeFlow, summary="Live data-flow metrics for this org's own pipelines")
async def runtime_flow(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    return await flow_summary(org_id)


@router.get("/connect-clusters", response_model=list[ConnectCluster])
async def list_connect_clusters(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> list[dict]:
    return await monitoring_service.list_connect_clusters(org_id)


@router.get(
    "/connect-pools/topology", response_model=list[PoolTopologyReport],
    summary="Do each worker pool's Kafka Connect internal topics match the design?",
)
async def connect_pool_topology(
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> list[dict]:
    """Per pool with its own Connect cluster: do `_inai_connect_<pool>_{configs,offsets,status}` exist, compacted,
    replicated and min-ISR'd as designed, with offsets having more partitions than configs/status. Pools on the shared
    cluster are reported as `shared` and not checked. Infrastructure-wide, not org-scoped."""
    return [report.as_dict() for report in await verify_topology()]


@router.get("/connect-clusters/{cluster_id}/connectors", response_model=list[RuntimeConnector])
async def list_cluster_connectors(
    cluster_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> list[dict]:
    return await monitoring_service.list_cluster_connectors(org_id, cluster_id)


@router.get("/connectors/{connector_name}/status", response_model=ConnectorStatus)
async def get_connector_status(
    connector_name: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    status = await monitoring_service.connector_status(org_id, connector_name)
    if status is None:
        # Never distinguish "doesn't exist" from "belongs to someone else" —
        # both cases 404 the same way.
        raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")
    return status


@router.get("/connectors/{connector_name}/tasks", response_model=list[ConnectorTask])
async def get_connector_tasks(
    connector_name: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> list[dict]:
    tasks = await monitoring_service.connector_tasks(org_id, connector_name)
    if tasks is None:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")
    return tasks


@router.get(
    "/connectors/metrics/summary", response_model=ConnectorMetricsSummary,
    summary="Current rate, lag and task counts of every connector this org owns, from Prometheus",
)
async def get_connector_metrics_summary(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    """One call for the Dashboard and the Monitoring connector list. Answers 200 with `prometheus_enabled: false` /
    `reachable: false` (and `error`) rather than failing — see ConnectorMetricsSummary."""
    return await connector_prometheus_metrics.summary(org_id)


@router.get(
    "/connectors/{connector_name}/metrics", response_model=ConnectorMetrics,
    summary="Prometheus-sourced Kafka Connect metrics for one of this org's connectors",
)
async def get_connector_metrics(
    connector_name: str,
    range: Literal["15m", "1h", "6h", "24h"] = Query(DEFAULT_RANGE, description="Time window for the series"),
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    """Throughput, lag, latency and error metrics the Connect workers export over JMX and Prometheus scrapes, as a
    time series over `range` plus a current value, and a per-task breakdown. Answers 200 with
    `prometheus_enabled: false` / `reachable: false` (and `error`) rather than failing when Prometheus isn't
    configured or can't be reached — the connector's REST status (`/status`) is unaffected either way."""
    result = await connector_prometheus_metrics.connector_metrics(org_id, connector_name, range)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_name}' not found")
    return result


@router.get("/metrics/summary", response_model=MetricsSummary)
async def metrics_summary(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    return await monitoring_service.metrics_summary(org_id)


@router.get("/metrics/failures", response_model=FailureBreakdown)
async def failure_breakdown(
    pipeline_id: str | None = Query(default=None, description="Only this pipeline's failures (default: the whole organization)."),
    window_seconds: int | None = Query(
        default=None, ge=60, le=86_400,
        description="How far back to count, in seconds (1 minute to 24 hours). Default: the configured monitoring window (1 hour).",
    ),
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:monitoring:view")),
) -> dict:
    """FE-BUG-17: validation failures (by rule) and ingestion failures (by
    cause), as two separate groups. NEW-04: `pipeline_id` narrows both to one pipeline — each connector dead-letter
    record is counted under the pipeline whose connector wrote it, even on a topic several pipelines share.
    `window_seconds` is the Quality page's "Last 15m / 1h / 6h / 24h" choice."""
    return await monitoring_service.failure_breakdown(org_id, pipeline_id, window_seconds)

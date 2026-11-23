"""
The Kafka Connect runtime design: distributed mode, one worker pool per workload type.

Kafka Connect runs in distributed mode. Rather than one cluster for everything, connectors are placed in separate worker
pools by workload — each pool its own Connect cluster:

    connect-cdc-pool      log-based change data capture (Debezium)
    connect-jdbc-pool     polling / batch database sources
    connect-file-pool     file and object-store sources
    connect-api-pool      REST / webhook API sources
    connect-stream-pool   queue and message-stream sources (JMS, MQ, ...)
    connect-sink-pool     every sink connector

Why separate: plugin isolation (a pool carries only the plugins its workload needs), failure isolation (a stuck CDC task
cannot starve a sink), workload-specific scaling and resource profiles, security segmentation (a pool holds only the
credentials its connectors need), and easier connector upgrades (roll one pool at a time).

A connector's pool is decided by its connector template (`runtime_pool_type`) when the pipeline is created, and fixed for
the pipeline's life — see app/services/connect_runtime.py for routing.

Each pool's cluster stores its state in three internal topics, named `_inai_connect_<pool>_{configs,offsets,status}`
(<pool> is the short name: cdc, jdbc, file, api, stream, sink) and configured as the design recommends: compacted,
replication.factor 3, min.insync.replicas 2, and an offsets topic with more partitions than the config/status topics.
This module is the single definition of those names and settings: the worker configuration (infra/docker-compose.yml), the
topology check (app/services/connect_topology.py) and the provisioning tool all read it, so they cannot drift apart.
"""

from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class PoolSpec:
    id: str
    short: str
    name: str
    workload: str


POOLS: tuple[PoolSpec, ...] = (
    PoolSpec("connect-cdc-pool", "cdc", "CDC Worker Pool", "Log-based change data capture (Debezium)"),
    PoolSpec("connect-jdbc-pool", "jdbc", "JDBC Worker Pool", "Polling and batch database sources"),
    PoolSpec("connect-file-pool", "file", "File Worker Pool", "File and object-store sources"),
    PoolSpec("connect-api-pool", "api", "API Worker Pool", "REST and webhook API sources"),
    PoolSpec("connect-stream-pool", "stream", "Stream Worker Pool", "Queue and message-stream sources"),
    PoolSpec("connect-sink-pool", "sink", "Sink Worker Pool", "Sink connectors"),
)

POOL_BY_ID: dict[str, PoolSpec] = {pool.id: pool for pool in POOLS}

# Every sink connector runs here, and nothing else does.
SINK_POOL_ID = "connect-sink-pool"

# Pool ids an earlier catalog used, mapped to today's. (The queue pool became the stream pool.)
POOL_ALIASES: dict[str, str] = {"connect-queue-pool": "connect-stream-pool"}


def canonical_pool_id(pool_id: str | None) -> str | None:
    """The current id for a pool id, resolving a retired alias; None for None or an unknown id."""
    if pool_id is None:
        return None
    pool_id = POOL_ALIASES.get(pool_id, pool_id)
    return pool_id if pool_id in POOL_BY_ID else None


# ── internal topics ─────────────────────────────────────────────────────────────────────────────────────────────────

# Partition counts. Connect REQUIRES the config topic to have exactly one partition (it is a single ordered log of
# connector configs); status has a few; offsets has the most, since every source task commits offsets to it — hence the
# design's "offsets should have more partitions than config/status".
INTERNAL_TOPIC_PARTITIONS = {"configs": 1, "status": 5, "offsets": 25}

INTERNAL_TOPIC_KINDS = ("configs", "offsets", "status")


def internal_topic_names(pool_id: str) -> dict[str, str]:
    """{"configs": "_inai_connect_<pool>_configs", "offsets": ..., "status": ...} for a pool."""
    pool = POOL_BY_ID[pool_id]
    return {kind: f"_inai_connect_{pool.short}_{kind}" for kind in INTERNAL_TOPIC_KINDS}


def worker_group_id(pool_id: str) -> str:
    """The Connect cluster's group.id — workers sharing it form one distributed cluster, so it is unique per pool."""
    return f"inai-connect-{POOL_BY_ID[pool_id].short}"


def expected_internal_topic_settings(kind: str) -> dict[str, int | str]:
    """What an internal topic of this kind must be configured as, per the design and this deployment's settings."""
    return {
        "partitions": INTERNAL_TOPIC_PARTITIONS[kind],
        "replication_factor": settings.connect_internal_replication_factor,
        "cleanup.policy": "compact",
        "min.insync.replicas": settings.connect_internal_min_insync_replicas,
    }


def worker_environment(pool_id: str, *, bootstrap: str, advertised_host: str) -> dict[str, str]:
    """The CONNECT_* environment a pool's worker container needs for its internal topics and group — what the
    infra definition must set. (The Confluent image maps CONNECT_<X> to the worker property <x>.)"""
    topics = internal_topic_names(pool_id)
    replication = str(settings.connect_internal_replication_factor)
    return {
        "CONNECT_BOOTSTRAP_SERVERS": bootstrap,
        "CONNECT_REST_ADVERTISED_HOST_NAME": advertised_host,
        "CONNECT_GROUP_ID": worker_group_id(pool_id),
        "CONNECT_CONFIG_STORAGE_TOPIC": topics["configs"],
        "CONNECT_OFFSET_STORAGE_TOPIC": topics["offsets"],
        "CONNECT_STATUS_STORAGE_TOPIC": topics["status"],
        "CONNECT_OFFSET_STORAGE_PARTITIONS": str(INTERNAL_TOPIC_PARTITIONS["offsets"]),
        "CONNECT_STATUS_STORAGE_PARTITIONS": str(INTERNAL_TOPIC_PARTITIONS["status"]),
        "CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR": replication,
        "CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR": replication,
        "CONNECT_STATUS_STORAGE_REPLICATION_FACTOR": replication,
    }

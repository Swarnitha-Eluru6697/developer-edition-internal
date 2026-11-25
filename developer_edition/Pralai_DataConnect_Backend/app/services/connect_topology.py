"""
Checking — and provisioning — each worker pool's Kafka Connect internal topics (app/core/connect_pools.py).

A distributed Connect cluster keeps its state in three internal topics: connector configurations, source offsets, and
connector/task status. If those are under-replicated or not compacted, losing a broker loses every connector's configuration
or offsets; the design therefore fixes their names and settings:

    _inai_connect_<pool>_configs | _offsets | _status
    cleanup.policy=compact, replication.factor 3, min.insync.replicas 2, offsets with more partitions than configs/status.

Connect would create them itself with whatever defaults it has (and does NOT enforce compaction or replication on a topic that
already exists), so they are best created ahead of the workers with exactly these settings: `provision_internal_topics`
does that (and the `python -m app.tools.provision_connect_topics` command wraps it), `verify_topology` reports whether what is
actually on the cluster still matches.

Only pools with a Connect cluster of their own (settings.connect_pools) have topics to check. A pool running on the shared
fallback cluster (settings.kafka_connect_url) uses whatever internal topics THAT cluster's workers were started with, which
DataConnect cannot see — it is reported as `shared`, not checked.
"""

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog
from kafka.admin import ConfigResource, ConfigResourceType, KafkaAdminClient, NewTopic
from kafka.errors import KafkaError, TopicAlreadyExistsError

from app.core.config import settings
from app.core.connect_pools import (
    INTERNAL_TOPIC_KINDS,
    POOL_BY_ID,
    POOLS,
    expected_internal_topic_settings,
    internal_topic_names,
)

logger = structlog.get_logger(__name__)

_UNKNOWN_TOPIC_OR_PARTITION = 3


@dataclass
class TopicReport:
    kind: str
    topic: str
    exists: bool = False
    partitions: int | None = None
    replication_factor: int | None = None
    cleanup_policy: str | None = None
    min_insync_replicas: int | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class PoolTopology:
    pool_id: str
    # "dedicated": the pool has its own Connect cluster and its topics were checked. "shared": it runs on the shared
    # cluster, whose internal topics DataConnect can't see.
    cluster_mode: str
    ok: bool
    topics: list[TopicReport] = field(default_factory=list)
    note: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def dedicated_pool_ids() -> list[str]:
    """Pools with a Connect cluster of their own."""
    return [pool.id for pool in POOLS if pool.id in settings.connect_pools]


def _observe(admin: KafkaAdminClient, topics: list[str]) -> dict[str, dict[str, Any]]:
    """{topic: {"exists", "partitions", "replication_factor", "configs"}} for the given topics."""
    observed: dict[str, dict[str, Any]] = {t: {"exists": False} for t in topics}
    existing = []
    for entry in admin.describe_topics(topics):
        name = entry["name"]
        if entry.get("error_code") == _UNKNOWN_TOPIC_OR_PARTITION or not entry.get("partitions"):
            continue
        partitions = entry["partitions"]
        observed[name] = {
            "exists": True,
            "partitions": len(partitions),
            "replication_factor": min(len(p.get("replica_nodes") or p.get("replicas") or []) for p in partitions),
        }
        existing.append(name)
    if existing:
        described = admin.describe_configs([ConfigResource(ConfigResourceType.TOPIC, name) for name in existing])
        for name in existing:
            entries = described.get("topic", {}).get(name, {})
            observed[name]["configs"] = {key: entry.get("value") for key, entry in entries.items()}
    return observed


def _check(kind: str, topic: str, observed: dict[str, Any], all_partitions: dict[str, int | None]) -> TopicReport:
    expected = expected_internal_topic_settings(kind)
    report = TopicReport(kind=kind, topic=topic, exists=bool(observed.get("exists")))
    if not report.exists:
        report.problems.append("topic does not exist")
        return report
    configs = observed.get("configs", {})
    report.partitions = observed["partitions"]
    report.replication_factor = observed["replication_factor"]
    report.cleanup_policy = configs.get("cleanup.policy")
    report.min_insync_replicas = int(configs["min.insync.replicas"]) if configs.get("min.insync.replicas") else None

    if kind == "configs":
        if report.partitions != 1:
            report.problems.append(f"has {report.partitions} partitions; the config topic must have exactly 1")
    elif report.partitions < expected["partitions"]:
        report.problems.append(f"has {report.partitions} partitions; expected at least {expected['partitions']}")
    if report.replication_factor < expected["replication_factor"]:
        report.problems.append(
            f"replication factor is {report.replication_factor}; expected at least {expected['replication_factor']}"
        )
    if report.cleanup_policy != "compact":
        report.problems.append(f"cleanup.policy is {report.cleanup_policy or 'delete (default)'}; it must be compact")
    if report.min_insync_replicas is None:
        report.problems.append(
            f"min.insync.replicas is not set on the topic (the broker default applies); expected {expected['min.insync.replicas']}"
        )
    elif report.min_insync_replicas < expected["min.insync.replicas"]:
        report.problems.append(
            f"min.insync.replicas is {report.min_insync_replicas}; expected at least {expected['min.insync.replicas']}"
        )
    return report


def _verify_pool(admin: KafkaAdminClient, pool_id: str) -> PoolTopology:
    names = internal_topic_names(pool_id)
    observed = _observe(admin, list(names.values()))
    partitions = {kind: observed[names[kind]].get("partitions") for kind in INTERNAL_TOPIC_KINDS}
    reports = [_check(kind, names[kind], observed[names[kind]], partitions) for kind in INTERNAL_TOPIC_KINDS]
    # The design: the offsets topic has MORE partitions than the config and status topics (every source task commits to it).
    offsets, others = partitions["offsets"], [partitions["configs"], partitions["status"]]
    if offsets is not None and all(p is not None for p in others) and offsets <= max(others):
        next(r for r in reports if r.kind == "offsets").problems.append(
            f"has {offsets} partitions; it must have more than the config and status topics ({max(others)})"
        )
    return PoolTopology(pool_id=pool_id, cluster_mode="dedicated", ok=not any(r.problems for r in reports), topics=reports)


def _verify_sync(pool_ids: list[str]) -> list[PoolTopology]:
    results: list[PoolTopology] = []
    shared_note = (
        "Runs on the shared Connect cluster (DATACONNECT_KAFKA_CONNECT_URL); its internal topics are whatever that "
        "cluster's workers were started with and are not checked here."
    )
    dedicated = [p for p in pool_ids if p in settings.connect_pools]
    for pool_id in pool_ids:
        if pool_id not in dedicated:
            results.append(PoolTopology(pool_id=pool_id, cluster_mode="shared", ok=True, note=shared_note))
    if not dedicated:
        return results
    try:
        admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-connect-topology")
    except KafkaError as exc:
        return results + [
            PoolTopology(pool_id=p, cluster_mode="dedicated", ok=False, error=f"Kafka unreachable: {type(exc).__name__}")
            for p in dedicated
        ]
    try:
        for pool_id in dedicated:
            try:
                results.append(_verify_pool(admin, pool_id))
            except KafkaError as exc:
                results.append(PoolTopology(
                    pool_id=pool_id, cluster_mode="dedicated", ok=False, error=f"Could not read topics: {type(exc).__name__}",
                ))
    finally:
        admin.close()
    return sorted(results, key=lambda r: [p.id for p in POOLS].index(r.pool_id))


async def verify_topology(pool_ids: list[str] | None = None) -> list[PoolTopology]:
    """Whether each pool's internal topics exist with the designed settings."""
    ids = pool_ids or [pool.id for pool in POOLS]
    unknown = [p for p in ids if p not in POOL_BY_ID]
    if unknown:
        raise ValueError(f"Unknown worker pool(s): {', '.join(unknown)}")
    return await asyncio.to_thread(_verify_sync, ids)


# ── provisioning ────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class ProvisionResult:
    pool_id: str
    created: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)


def _provision_sync(pool_ids: list[str]) -> list[ProvisionResult]:
    admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-connect-provision")
    results = []
    try:
        for pool_id in pool_ids:
            result = ProvisionResult(pool_id=pool_id)
            for kind, topic in internal_topic_names(pool_id).items():
                spec = expected_internal_topic_settings(kind)
                new_topic = NewTopic(
                    name=topic, num_partitions=int(spec["partitions"]), replication_factor=int(spec["replication_factor"]),
                    topic_configs={"cleanup.policy": "compact", "min.insync.replicas": str(spec["min.insync.replicas"])},
                )
                try:
                    admin.create_topics([new_topic])
                    result.created.append(topic)
                    logger.info(
                        "connect_topology.topic_created", pool=pool_id, topic=topic, partitions=spec["partitions"],
                        replication_factor=spec["replication_factor"],
                    )
                except TopicAlreadyExistsError:
                    # Never altered: partition and replication changes need a reassignment, and an existing topic may be in
                    # use by a running cluster. verify_topology reports whether it matches.
                    result.already_present.append(topic)
            results.append(result)
    finally:
        admin.close()
    return results


async def provision_internal_topics(pool_ids: list[str] | None = None) -> list[ProvisionResult]:
    """Create any missing internal topic of the given pools (default: every pool with its own cluster) with the designed
    settings. Idempotent; leaves existing topics untouched."""
    ids = pool_ids or dedicated_pool_ids()
    unknown = [p for p in ids if p not in POOL_BY_ID]
    if unknown:
        raise ValueError(f"Unknown worker pool(s): {', '.join(unknown)}")
    return await asyncio.to_thread(_provision_sync, ids)

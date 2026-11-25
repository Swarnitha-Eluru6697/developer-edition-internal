"""
What the Kafka cluster can actually do, for checking a topic policy before anything asks it to create a topic (GAP-06).

The platform's topic defaults (replication.factor 3, min.insync.replicas 2) are right for a production cluster. On a cluster with
fewer brokers they cannot be satisfied: Kafka's own CreateTopics fails with InvalidReplicationFactor — but only after the request is
issued, with an error nothing upstream understood. These helpers let DataConnect say so first, in plain words:

    Kafka has 2 brokers; this topic policy asks for replication factor 3.

`broker_count` is cached briefly. Every check here treats "couldn't reach Kafka" as UNKNOWN, never as a failure of the policy: an
unreachable cluster is reported by the steps that actually need it (Connect, provisioning), and blocking a deploy on a policy
question we could not answer would be wrong.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog
from kafka.admin import KafkaAdminClient
from kafka.errors import KafkaError

from app.core.config import settings

logger = structlog.get_logger(__name__)

_UNKNOWN_TOPIC_OR_PARTITION = 3

_cache: tuple[float, int | None] | None = None

# Short timeouts: this runs inside a readiness check or a deploy, and "Kafka is unreachable" must come back quickly as UNKNOWN.
# (Only options KafkaAdminClient actually accepts — an unknown one raises KafkaConfigurationError, a KafkaError that would be
# swallowed below as "unreachable"; tests/test_topic_feasibility.py checks these against the client's own list.)
ADMIN_CLIENT_OPTIONS: dict[str, Any] = {
    "client_id": "dataconnect-topic-preflight", "request_timeout_ms": 5000, "bootstrap_timeout_ms": 3000,
}


def invalidate_broker_count_cache() -> None:
    global _cache
    _cache = None


def brokers_of(admin: Any) -> int:
    """How many brokers `admin`'s cluster has; 0 when it can't be told.

    `describe_cluster()` is the proper call, but against a real Kafka 3.6 broker with kafka-python 3.0 it raises KeyError
    ('endpoint_type' — the client pops a field its own v0 request never gets back), found only when this ran against a live
    cluster. Any NON-Kafka failure there is that kind of client quirk, so the broker list is read from the client's own cluster
    metadata instead. A KafkaError (unreachable, timeout) is genuine and still propagates."""
    try:
        return len(admin.describe_cluster().get("brokers") or [])
    except KafkaError:
        raise
    except Exception as exc:  # noqa: BLE001 - a client library quirk, not a cluster fact
        logger.warning("kafka_cluster.describe_cluster_quirk", error=type(exc).__name__)
    for holder in ("_manager", "_client"):
        cluster = getattr(getattr(admin, holder, None), "cluster", None)
        if cluster is not None:
            try:
                return len(list(cluster.brokers()))
            except Exception:  # noqa: BLE001
                continue
    return 0


def _broker_count_sync() -> int | None:
    try:
        admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, **ADMIN_CLIENT_OPTIONS)
    except KafkaError as exc:
        logger.warning("kafka_cluster.unreachable", error=type(exc).__name__)
        return None
    try:
        return brokers_of(admin) or None  # an empty answer is "couldn't tell", not "no brokers"
    except KafkaError as exc:
        logger.warning("kafka_cluster.broker_count_failed", error=type(exc).__name__)
        return None
    finally:
        admin.close()


async def broker_count() -> int | None:
    """How many brokers the cluster has, or None if that can't be determined. Cached for
    settings.kafka_broker_count_cache_seconds; only a SUCCESSFUL answer is cached, so a Kafka that was down a moment ago is
    asked again next time."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < settings.kafka_broker_count_cache_seconds:
        return _cache[1]
    count = await asyncio.to_thread(_broker_count_sync)
    if count is not None:
        _cache = (now, count)
    return count


def topic_exists_sync(admin: Any, topic: str) -> bool:
    """Whether the topic is already on the cluster."""
    for entry in admin.describe_topics([topic]):
        if entry.get("name") == topic and entry.get("error_code") != _UNKNOWN_TOPIC_OR_PARTITION and entry.get("partitions"):
            return True
    return False


async def topic_exists(topic: str) -> bool | None:
    """True/False, or None if the cluster could not be asked."""

    def _ask() -> bool | None:
        try:
            admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, **ADMIN_CLIENT_OPTIONS)
        except KafkaError:
            return None
        try:
            return topic_exists_sync(admin, topic)
        except KafkaError:
            return None
        finally:
            admin.close()

    return await asyncio.to_thread(_ask)


@dataclass(frozen=True)
class PolicyProblem:
    code: str
    message: str


def topic_policy_problems(resolved: dict[str, Any], brokers: int | None) -> list[PolicyProblem]:
    """What is wrong with a RESOLVED topic policy (every field filled in) given `brokers` — None when unknown, in which case only
    the checks that need no cluster run. Pure.

        replication factor > brokers          can never be placed
        min.insync.replicas > replication     acks=all producers could never be acknowledged, even with every replica up
        min.insync.replicas > brokers         the same, and unsatisfiable on this cluster even at full replication
    """
    problems: list[PolicyProblem] = []
    replication = resolved.get("replication_factor")
    min_isr = resolved.get("min_insync_replicas")
    if brokers is not None and isinstance(replication, int) and replication > brokers:
        problems.append(PolicyProblem(
            "replication_exceeds_brokers", f"Replication factor {replication} exceeds available brokers {brokers}.",
        ))
    if isinstance(min_isr, int) and isinstance(replication, int) and min_isr > replication:
        problems.append(PolicyProblem(
            "min_isr_exceeds_replication",
            f"min.insync.replicas {min_isr} exceeds the replication factor {replication}: producers that require "
            "acknowledgement from all in-sync replicas could never write.",
        ))
    elif brokers is not None and isinstance(min_isr, int) and min_isr > brokers:
        problems.append(PolicyProblem(
            "min_isr_exceeds_brokers",
            f"min.insync.replicas {min_isr} exceeds available brokers {brokers}: producers that require acknowledgement "
            "from all in-sync replicas could never write.",
        ))
    return problems

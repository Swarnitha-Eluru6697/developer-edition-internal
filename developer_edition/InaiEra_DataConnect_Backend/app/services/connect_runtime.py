"""
Routing to the right Kafka Connect cluster (the runtime design's worker pools — app/core/connect_pools.py).

Every connector lives in exactly one pool, chosen from its connector template when the pipeline is created
(`pool_for_template`) and stored on the pipeline as `runtime_pool_id`. Everything that talks to Kafka Connect — deploy,
pause/resume/restart/delete, the reconciler, status and monitoring — asks here for the cluster that pool runs on
(`client_for_pool`), instead of one global URL.

A pool with no cluster of its own in `settings.connect_pools` runs on `settings.kafka_connect_url`: a shared cluster. That
keeps a single-cluster development setup working unchanged; production maps each pool to its own cluster.
"""

from typing import Any

from app.core.config import settings
from app.core.connect_pools import POOL_BY_ID, POOLS, canonical_pool_id
from app.services.runtime_orchestrator import KafkaConnectClient


def pool_endpoint(pool_id: str | None) -> str:
    """The REST URL of the Connect cluster a pool runs on."""
    canonical = canonical_pool_id(pool_id)
    url = settings.connect_pools.get(canonical) if canonical else None
    return (url or settings.kafka_connect_url).rstrip("/")


def client_for_pool(pool_id: str | None) -> KafkaConnectClient:
    return KafkaConnectClient(pool_endpoint(pool_id))


def pool_of(pipeline: dict[str, Any]) -> str | None:
    """The pool a pipeline's connector runs in. None only for a row from before pools existed that was never backfilled —
    it then runs on the shared cluster (pool_endpoint(None))."""
    return canonical_pool_id(pipeline.get("runtime_pool_id"))


def client_for_pipeline(pipeline: dict[str, Any]) -> KafkaConnectClient:
    return client_for_pool(pool_of(pipeline))


def endpoints() -> dict[str, list[str]]:
    """Distinct Connect clusters in use -> the pools running on each (several pools share one URL in a shared setup)."""
    by_url: dict[str, list[str]] = {}
    for pool in POOLS:
        by_url.setdefault(pool_endpoint(pool.id), []).append(pool.id)
    return by_url


def pool_for_template(template: dict[str, Any]) -> str:
    """The pool a connector template places its connectors in. ValueError for a template naming a pool that does not exist."""
    declared = template.get("runtime_pool_type")
    pool_id = canonical_pool_id(declared)
    if pool_id is None:
        raise ValueError(
            f"Connector template names an unknown worker pool {declared!r}; known pools: "
            f"{', '.join(sorted(POOL_BY_ID))}."
        )
    return pool_id


def configuration_problems() -> list[str]:
    """Misconfigured DATACONNECT_CONNECT_POOLS entries, for startup validation."""
    problems = []
    for pool_id, url in settings.connect_pools.items():
        if pool_id not in POOL_BY_ID:
            problems.append(f"DATACONNECT_CONNECT_POOLS names unknown pool {pool_id!r} (known: {', '.join(sorted(POOL_BY_ID))})")
        elif not str(url).startswith(("http://", "https://")):
            problems.append(f"DATACONNECT_CONNECT_POOLS[{pool_id!r}] is not an http(s) URL: {url!r}")
    return problems

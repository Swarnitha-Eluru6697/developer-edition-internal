"""
Liveness vs. readiness, kept deliberately separate:

  GET /health/live
      Is the API process itself up and able to handle a request at all?
      No dependency calls, ever — a downstream outage (PostgreSQL, Kafka
      Connect, or worse, one customer's own source system) must never fail
      this. An orchestrator restarts a process that fails its liveness
      probe; restarting a perfectly healthy process because something else
      is down doesn't fix that outage, it just adds a restart storm on top
      of it.

  GET /health/ready
      Is this process ready to safely serve DataConnect requests? Checks
      the dependencies actually necessary for that: PostgreSQL always (the
      control-plane database backing pipelines/connections/the runtime
      reconciler — nothing works correctly without it), the secret store
      (can it store and read connection credentials; GAP-05), and Kafka Connect
      only when this deployment is actually configured to talk to it
      (settings.runtime_deploy_enabled) — readiness reflects what THIS
      deployment's endpoint contract needs, not a fixed list.

      Deliberately excludes any individual customer source system (a
      pipeline's own target database/API/etc.) — those are numerous,
      tenant-specific, and already checked on demand via
      POST /connections/{id}/test-connection, not the service-wide health
      check. One tenant's unreachable database must never take down
      readiness for every other tenant.

      Returns 503 (not 200) when not ready, so a load balancer/orchestrator
      can act on the HTTP status directly.

  GET /health
      Kept for backward compatibility with existing health-check wiring
      (the Dockerfile/compose healthcheck predates this split) — same
      dependency-free behavior as /health/live. New wiring should prefer
      the explicit /health/live and /health/ready.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings

router = APIRouter()

logger = structlog.get_logger(__name__)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "dataconnect"}


@router.get("/health/live")
def liveness() -> dict[str, str]:
    """No dependency calls — see module docstring."""
    return {"status": "ok", "service": "dataconnect"}


async def _check_postgres() -> dict[str, Any]:
    from app.db.session import engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_class": type(exc).__name__}


async def _check_secret_store() -> dict[str, Any]:
    """GAP-05: readiness must not say "ready" about a secret store it never looked at. Asks the store actually in use whether it
    can store and read secrets; `DATACONNECT_SECRET_BACKEND=vault` cannot reach here (settings reject it at startup until a Vault
    store exists, and that store's own check_ready would then verify authentication and path access)."""
    from app.services import secret_store as secret_store_module

    return await secret_store_module.secret_store.check_ready()


async def _check_kafka_connect() -> dict[str, Any]:
    """Every Kafka Connect cluster the worker pools run on (runtime design) — one check each, so a down pool is named."""
    from app.services.connect_runtime import endpoints
    from app.services.runtime_orchestrator import KafkaConnectClient

    clusters: dict[str, Any] = {}
    for url, pools in endpoints().items():
        try:
            await KafkaConnectClient(url).list_connectors()
            clusters[url] = {"ok": True, "pools": pools}
        except Exception as exc:
            clusters[url] = {"ok": False, "pools": pools, "error": str(exc), "error_class": type(exc).__name__}
    result: dict[str, Any] = {"ok": all(c["ok"] for c in clusters.values())}
    if len(clusters) > 1 or not result["ok"]:
        result["clusters"] = clusters
    first_error = next((c for c in clusters.values() if not c["ok"]), None)
    if first_error:
        result["error"] = first_error["error"]
        result["error_class"] = first_error["error_class"]
    return result


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {"postgres": await _check_postgres(), "secret_store": await _check_secret_store()}

    if settings.runtime_deploy_enabled:
        checks["kafka_connect"] = await _check_kafka_connect()
    else:
        checks["kafka_connect"] = {"ok": True, "skipped": "runtime_deploy_enabled is disabled"}

    ready = all(check.get("ok") for check in checks.values())
    response.status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    if not ready:
        logger.warning("health.not_ready", checks=checks)  # the full detail stays in the log
    return {"status": "ready" if ready else "not_ready", "checks": _without_internal_detail(checks)}


def _without_internal_detail(checks: dict[str, Any]) -> dict[str, Any]:
    """This endpoint is unauthenticated (a probe has no identity), so it says WHAT is not ready, never why in infrastructure
    terms: no driver error text (hostnames, ports, credentials in a connection string) and no cluster URLs."""
    public: dict[str, Any] = {}
    for name, check in checks.items():
        entry: dict[str, Any] = {"ok": bool(check.get("ok"))}
        if check.get("backend"):
            entry["backend"] = check["backend"]   # which store, e.g. "postgres" — not where it is or how it is reached
        if "skipped" in check:
            entry["skipped"] = check["skipped"]
        down = [pool for cluster in (check.get("clusters") or {}).values() if not cluster.get("ok") for pool in cluster.get("pools", [])]
        if down:
            entry["unreachable_pools"] = down
        public[name] = entry
    return public

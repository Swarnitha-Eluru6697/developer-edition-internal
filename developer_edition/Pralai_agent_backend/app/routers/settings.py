"""
Real, read-only runtime configuration (§0.45, GAP_REPORT) — surfaces the
platform's own actual, currently-effective tunable settings for an admin
to see in the Settings page, without letting them change anything from
here. Deliberately read-only: these values are set by whatever infra
actually deploys this platform (environment variables / a Kubernetes
ConfigMap), not by this application itself — see ARCHITECTURE.md §9.3
for why editing them from inside the app would conflict with that real
ownership boundary, and §9.2 for why a connection pool specifically
can't be resized live without a real, disruptive engine restart even if
it COULD be edited here.

Every value here is either non-secret by nature (a pool size, a task
queue name, a soft-check flag) or, for the few real secrets this
platform depends on (the UCS gateway secret, ANTHROPIC_API_KEY, the database
password embedded in DATABASE_URL), reduced to a real boolean -
"is this configured" - never the actual value. This is a genuine
security boundary, not an incidental omission.
"""
import os

from fastapi import APIRouter, Depends
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission

from ..db import engine, DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_TIMEOUT
from .execution import TASK_QUEUE
from ..ucs_client import UCS_BASE_URL, UCS_TENANT, UCS_AGENT_CATALOG, UCS_AGENT_SCHEMA
from .. import ucs_sync
from ..harness import UCS_SOFT_CHECK

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/runtime-config", dependencies=[Depends(require_any_permission(P.SETTINGS, P.STUDIO))])
def get_runtime_config() -> dict:
    """Real, live values - the database section specifically reads the
    actual running SQLAlchemy engine's own pool state
    (engine.pool.size()/checkedout()/overflow()), not just the
    configured numbers, so an admin can see genuine current utilization,
    not only what was requested at startup."""
    live_overflow = engine.pool.overflow()
    return {
        "database": {
            "pool_size_configured": DB_POOL_SIZE,
            "max_overflow_configured": DB_MAX_OVERFLOW,
            "pool_timeout_seconds": DB_POOL_TIMEOUT,
            "pool_size_live": engine.pool.size(),
            "connections_checked_out_live": engine.pool.checkedout(),
            # SQLAlchemy's own real overflow counter starts at
            # -pool_size and counts up as connections beyond the base
            # pool are checked out - genuinely 0 real overflow
            # connections in use until it crosses positive.
            "overflow_connections_in_use_live": max(0, live_overflow),
        },
        "temporal": {
            "address": os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            "task_queue": TASK_QUEUE,
        },
        "ucs": {
            "base_url": UCS_BASE_URL,
            "tenant": UCS_TENANT,
            # Each agent is its own catalog now, named after the agent
            # (ucs_client.agent_slug), so there is no single catalog to
            # report — naming it here would describe a layout that no
            # longer exists. The fallback is what an agent whose name
            # yields no usable slug lands in.
            "agent_catalog_per_agent": True,
            "agent_catalog_fallback": UCS_AGENT_CATALOG,
            "agent_schema": UCS_AGENT_SCHEMA,
            "soft_check_mode": UCS_SOFT_CHECK,
            # Admin calls go as the service identity (gateway secret + principal +
            # permissions); the key name is kept for existing readers.
            "admin_token_configured": ucs_sync.service_identity_configured(),
            "admin_identity": ucs_sync.UCS_SERVICE_USERNAME,
        },
        "platform": {
            "base_url": os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000"),
        },
        "model_provider": {
            "anthropic_api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        },
    }

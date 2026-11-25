"""
A redacted support diagnostics bundle: what an operator or a support engineer needs to understand a deployment, safe to attach to a ticket.

Design rules (docs/SUPPORT_DIAGNOSTICS.md):

  * NEVER secrets. Configuration shows whether a secret-shaped setting is SET, not its value; URLs lose their credentials; and the finished
    bundle goes through a final deep redaction pass as a net under everything else.
  * NEVER customer content. Aggregates (counts by status), identifiers, error CODES and correlation ids — not connection configs, connector
    configs, pipeline or connector names, record data, or free-text messages. A correlation id ties a line here to the detailed (redacted)
    log entry, which is where the real cause lives.
  * TENANT-SCOPED. A bundle built for an organization contains only that organization's counts and failures. (Process-level facts — versions,
    worker health, configuration — are not tenant data.) `org_id=None` is the whole-deployment form used by the CLI (`python -m app.diagnostics`).
  * RESILIENT. Every section is collected independently with its own deadline; a section that fails reports its error CLASS and the rest are
    still returned. Diagnostics must work when parts of the system are broken — that is when they are needed.
  * BOUNDED. The serialized bundle is capped; if it is over, the longest lists are trimmed and `truncated` says so.
"""

import asyncio
import importlib.metadata
import json
import platform
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import settings
from app.core.redaction import MASK, redact_diagnostic, redact_secrets

logger = structlog.get_logger(__name__)

SECTION_TIMEOUT_SECONDS = 5.0
MAX_BUNDLE_BYTES = 1_000_000
MAX_RECENT_FAILURES = 20
_STARTED_AT = time.time()

# Settings whose VALUE must never appear: reported only as "<set>" / "<unset>".
_SECRET_SETTING_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|private[_-]?key|credential|encryption[_-]?key|previous[_-]?keys|authorization|dsn)", re.IGNORECASE
)
# Packages whose versions support actually asks about.
_PACKAGES = ("fastapi", "starlette", "uvicorn", "pydantic", "sqlalchemy", "asyncpg", "alembic", "httpx", "kafka-python", "structlog",
             "prometheus-client", "cryptography", "pymongo", "pytest")


# ── sections ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def _service(org_id: str | None) -> dict[str, Any]:
    return {
        "name": "dataconnect", "version": settings.app_version, "environment": settings.env,
        "python": platform.python_version(), "platform": platform.platform(),
        "uptime_seconds": round(time.time() - _STARTED_AT),
        "scope": {"organization": org_id} if org_id else {"deployment": "all organizations (aggregated)"},
    }


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_secrets(value)[:300]       # a URL keeps its host but loses user:password@
    if isinstance(value, list | tuple | set):
        return [_scalar(v) for v in list(value)[:20]]
    return f"<{type(value).__name__}>"


async def _configuration(org_id: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(settings.model_dump().items()):
        if _SECRET_SETTING_RE.search(key):
            out[key] = "<set>" if value not in (None, "", [], {}) else "<unset>"
        else:
            out[key] = _scalar(value)
    return out


async def _database(org_id: str | None) -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import func, select, text

    from app.db.session import AsyncSessionLocal
    from app.models.connection import ConnectionProfile
    from app.models.operations import Operation
    from app.models.pipeline import Pipeline
    from app.models.quality import QualityExecutionJob

    cfg = Config(str(_repo_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_repo_root() / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    async with AsyncSessionLocal() as session:
        current = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()

        async def grouped(model: Any, column: Any, *extra: Any) -> dict[str, int]:
            stmt = select(column, func.count()).group_by(column)
            if org_id is not None:
                stmt = stmt.where(model.org_id == org_id)
            return {str(k if k is not None else "none"): int(n) for k, n in (await session.execute(stmt)).all()}

        async def count(model: Any) -> int:
            stmt = select(func.count()).select_from(model)
            if org_id is not None:
                stmt = stmt.where(model.org_id == org_id)
            return int((await session.execute(stmt)).scalar_one())

        return {
            "migration": {"current": current, "head": head, "up_to_date": current == head},
            "counts": {
                "connections": await count(ConnectionProfile),
                "pipelines_by_status": await grouped(Pipeline, Pipeline.status),
                "operations_by_status": await grouped(Operation, Operation.status),
                "operations_by_result_status": await grouped(Operation, Operation.result_status),
                "quality_jobs_by_status": await grouped(QualityExecutionJob, QualityExecutionJob.status),
            },
        }


async def _workers(org_id: str | None) -> dict[str, Any]:
    from app.services import operation_runner, quality_job_runner
    from app.services.dlq_failure_counter import dlq_counter_manager
    from app.services.quality_stream_worker import quality_stream_manager
    from app.services.quality_worker_health import quality_worker_health

    def alive(task: Any) -> bool:
        return task is not None and not task.done()

    return {
        "operation_worker_running": alive(operation_runner._worker_task),
        "quality_job_worker_running": alive(quality_job_runner._worker_task),
        "quality_stream_workers": len(quality_stream_manager._workers),
        "dlq_counter_workers": len(dlq_counter_manager._workers),
        "quality_blocked_partitions": quality_worker_health.blocked_partitions(),
        "quality_oldest_uncommitted_age_seconds": round(quality_worker_health.oldest_uncommitted_age(), 1),
    }


async def _readiness(org_id: str | None) -> dict[str, Any]:
    from app.api.routes import health

    def shaped(result: dict[str, Any]) -> dict[str, Any]:
        shaped_result = {"ok": bool(result.get("ok"))}
        if result.get("backend"):
            shaped_result["backend"] = result["backend"]
        if result.get("error_class"):
            shaped_result["error_class"] = str(result["error_class"])      # the CLASS only: the message names hosts and ports
        return shaped_result

    out = {"postgres": shaped(await health._check_postgres()), "secret_store": shaped(await health._check_secret_store())}
    if settings.runtime_deploy_enabled:
        connect = await health._check_kafka_connect()
        out["kafka_connect"] = {**shaped(connect), "clusters": {
            # cluster URLs are infrastructure detail; the pools they serve are what matters for support
            f"cluster_{i}": {"ok": bool(c.get("ok")), "pools": c.get("pools", [])} for i, c in enumerate((connect.get("clusters") or {}).values())}}
    else:
        out["kafka_connect"] = {"ok": True, "skipped": "runtime_deploy_enabled is disabled"}
    return out


async def _metrics(org_id: str | None) -> dict[str, Any]:
    from prometheus_client import REGISTRY

    values: dict[str, float] = {}
    for family in REGISTRY.collect():
        if not family.name.startswith("dataconnect_"):
            continue
        for sample in family.samples:
            if sample.name.endswith(("_created", "_bucket")):
                continue
            labels = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()) if k != "service")
            values[f"{sample.name}{{{labels}}}" if labels else sample.name] = sample.value
    return dict(sorted(values.items())[:300])


async def _recent_failures(org_id: str | None) -> dict[str, Any]:
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.operations import Operation
    from app.models.quality import QualityExecutionJob

    def when(value: Any) -> str | None:
        return value.isoformat() if value else None

    async with AsyncSessionLocal() as session:
        ops = select(Operation).where(Operation.status.in_(("failed", "stale")) | (Operation.result_status == "invalid"))
        jobs = select(QualityExecutionJob).where(QualityExecutionJob.status.in_(("failed", "stale")))
        if org_id is not None:
            ops, jobs = ops.where(Operation.org_id == org_id), jobs.where(QualityExecutionJob.org_id == org_id)
        ops_rows = (await session.execute(ops.order_by(Operation.completed_at.desc().nullslast()).limit(MAX_RECENT_FAILURES))).scalars().all()
        job_rows = (await session.execute(jobs.order_by(QualityExecutionJob.completed_at.desc().nullslast()).limit(MAX_RECENT_FAILURES))).scalars().all()
    # codes and correlation ids only — the correlation id finds the (redacted) log entry that carries the real cause
    return {
        "operations": [{"id": o.id, "type": o.operation_type, "status": o.status, "result_status": o.result_status, "error_code": o.error_code,
                        "retryable": o.retryable, "correlation_id": o.correlation_id, "completed_at": when(o.completed_at)} for o in ops_rows],
        "quality_jobs": [{"id": j.id, "status": j.status, "error_code": j.error_code, "retryable": j.retryable,
                          "correlation_id": j.correlation_id, "completed_at": when(j.completed_at)} for j in job_rows],
    }


async def _dependencies(org_id: str | None) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


SECTIONS: dict[str, Callable[[str | None], Awaitable[Any]]] = {
    "service": _service,
    "configuration": _configuration,
    "readiness": _readiness,
    "database": _database,
    "workers": _workers,
    "metrics": _metrics,
    "recent_failures": _recent_failures,
    "dependencies": _dependencies,
}


# ── assembly ───────────────────────────────────────────────────────────────────────────────────────────────────────

def deep_redact(value: Any, *, key: str = "") -> Any:
    """The net under every section: no string anywhere in the bundle keeps a credential, and a secret-shaped KEY never keeps a value
    (booleans and the "<set>"/"<unset>" markers are not secrets and stay)."""
    if isinstance(value, dict):
        return {k: deep_redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [deep_redact(v, key=key) for v in value]
    if isinstance(value, str):
        if _SECRET_SETTING_RE.search(key) and value not in ("<set>", "<unset>"):
            return MASK
        return redact_diagnostic(value)
    return value


async def _not_available(name: str) -> dict[str, str]:
    return {"note": f"'{name}' describes the running service's process; this bundle was produced by a separate process, so it has nothing to report. "
                    "Use GET /diagnostics/bundle on the service for it."}


async def _collect(name: str, org_id: str | None) -> Any:
    try:
        return await asyncio.wait_for(SECTIONS[name](org_id), timeout=SECTION_TIMEOUT_SECONDS)
    except TimeoutError:
        return {"error": "TimeoutError", "detail": f"section did not finish within {SECTION_TIMEOUT_SECONDS}s"}
    except Exception as exc:  # noqa: BLE001 - a broken section must not take the whole bundle down; its CLASS is the diagnostic
        logger.warning("diagnostics.section_failed", section=name, error=type(exc).__name__)
        return {"error": type(exc).__name__}


def _trim_to_fit(bundle: dict[str, Any]) -> bool:
    """Shrink the longest list in the bundle until it serializes within MAX_BUNDLE_BYTES. True if anything was trimmed."""
    trimmed = False
    while len(json.dumps(bundle, default=str)) > MAX_BUNDLE_BYTES:
        longest: tuple[int, list[Any]] | None = None
        stack: list[Any] = [bundle]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                if longest is None or len(node) > longest[0]:
                    longest = (len(node), node)
                stack.extend(node)
        if longest is None or longest[0] <= 1:
            break
        del longest[1][len(longest[1]) // 2 :]
        trimmed = True
    return trimmed


# Sections that describe THIS PROCESS's runtime (which workers are alive, what its counters read). From the CLI that process is the CLI itself,
# so reporting "operation worker not running" would be a false negative about the real service.
PROCESS_LOCAL_SECTIONS = ("workers", "metrics")


async def build_diagnostics_bundle(org_id: str | None = None, *, in_service_process: bool = True) -> dict[str, Any]:
    names = list(SECTIONS)
    results = await asyncio.gather(*(
        _collect(name, org_id) if in_service_process or name not in PROCESS_LOCAL_SECTIONS else _not_available(name) for name in names
    ))
    bundle: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "schema": "dataconnect-diagnostics/1",
        **dict(zip(names, results, strict=True)),
    }
    bundle = deep_redact(bundle)
    bundle["truncated"] = _trim_to_fit(bundle)
    bundle["failed_sections"] = sorted(n for n in names if isinstance(bundle.get(n), dict) and "error" in bundle[n])
    return bundle

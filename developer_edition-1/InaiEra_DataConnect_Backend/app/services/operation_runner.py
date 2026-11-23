"""
Runs submitted Operations via a durable Postgres claim/lease worker —
replacing the earlier in-process `asyncio.create_task` pattern, which lost
an operation's work the moment the process running it restarted (see
app/models/operations.py's docstring for the lifecycle this now implements:
queued -> claimed/running -> succeeded/failed, with a heartbeated lease and
a bounded attempt_count).

This works *because* every operation type is fully reconstructible from
just (org_id, operation_type, resource_type, resource_id) — "redeploy this
pipeline", "re-discover this connection" — with no arbitrary payload that
only exists in the submitting request's memory. `register_work` is how a
route module supplies that reconstruction: a factory of
(org_id, resource_id) -> a zero-argument async callable, registered once at
import time (see app/api/routes/pipelines.py and connections.py), looked up
by `operation_type` whenever a worker (this process or, after a restart, a
later one) actually claims the row and needs to run it.

QualityExecutionJob (app/services/quality_job_runner.py) uses the same claim/lease model in its
own table, for the same reason: its work is reconstructible from its row (pipeline/policy by id,
the batch from QualityExecutionJobRecords).

A module-level set holds a strong reference to each in-flight execution
task so it isn't garbage collected mid-flight — asyncio only holds a weak
reference to a task that nothing else is tracking.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, func, or_, select, update

from app.core.config import settings
from app.core.metrics import record_operation_result_invalid
from app.db.session import AsyncSessionLocal
from app.models.operations import Operation
from app.repositories.dataconnect import OperationRepository
from app.schemas.operation_results import RESULT_MODEL_BY_OPERATION_TYPE, OperationResultInvalid
from app.services.safe_error import SafeError, manual_safe_error, to_safe_error

logger = structlog.get_logger(__name__)

_running_operations: set[asyncio.Task] = set()

WorkFn = Callable[[], Awaitable[dict[str, Any]]]
WorkFactory = Callable[[str, str], WorkFn]

_work_factories: dict[str, WorkFactory] = {}

_worker_task: asyncio.Task | None = None
_wake_event = asyncio.Event()


_result_models: dict[str, type[BaseModel]] = {}


def register_work(
    operation_type: str, factory: WorkFactory, result_model: type[BaseModel] | None = None,
) -> None:
    """Registers how to (re)build the work for `operation_type` from just
    (org_id, resource_id) — called once at import time by the route module
    that owns that operation type.

    GAP-11: every operation type must also have a typed result contract — `result_model`, or the one in
    app/schemas/operation_results.py's RESULT_MODEL_BY_OPERATION_TYPE. A type with neither is refused here, at import
    time, rather than being free to persist arbitrary JSON later. What the work returns is validated against it before
    it is stored (see `check_result`)."""
    model = result_model or RESULT_MODEL_BY_OPERATION_TYPE.get(operation_type)
    if model is None:
        raise ValueError(
            f"Operation type '{operation_type}' has no result contract: add a model for it to "
            "app/schemas/operation_results.py (RESULT_MODEL_BY_OPERATION_TYPE) or pass result_model=."
        )
    _work_factories[operation_type] = factory
    _result_models[operation_type] = model


@dataclass(frozen=True)
class ResultCheck:
    """The result-contract outcome (GAP-04), kept apart from whether the action ran."""

    payload: dict[str, Any] | None   # the contract-shaped result to store; None if invalid
    reason: str | None = None        # "does not match its contract" | "too large" | "no result contract registered"

    @property
    def valid(self) -> bool:
        return self.reason is None


def check_result(operation_type: str, operation_id: str, result: Any) -> ResultCheck:
    """GAP-11/GAP-04: the boundary every Operation result passes through before it is persisted. The result must satisfy its
    type's contract and serialize to no more than settings.operation_result_max_bytes. It is stored in the CONTRACT's shape
    (re-serialized from the validated model), so a stray field a callable happened to include is dropped rather than stored.

    A result that fails is not stored, and the failure is NOT hidden behind a plain success: the caller records
    status=succeeded (the action's side effects already happened — marking it failed would misreport that) together with
    result_status=invalid and error_code OPERATION_RESULT_INVALID, and a metric counts it (see `_finish_result_invalid`)."""
    model = _result_models.get(operation_type)
    if model is None:  # defence in depth: an unregistered type never reaches here (register_work refuses it)
        logger.error("operation.result_rejected", operation_id=operation_id, operation_type=operation_type,
                     reason="no result contract registered")
        return ResultCheck(None, "no result contract registered")
    try:
        payload = model.model_validate(result).model_dump(mode="json")
    except ValidationError as exc:
        logger.error("operation.result_rejected", operation_id=operation_id, operation_type=operation_type,
                     reason="does not match its contract", errors=exc.errors(include_url=False, include_input=False))
        return ResultCheck(None, "does not match its contract")
    size = len(json.dumps(payload, separators=(",", ":")).encode())
    if size > settings.operation_result_max_bytes:
        logger.error("operation.result_rejected", operation_id=operation_id, operation_type=operation_type,
                     reason="too large", size_bytes=size, limit_bytes=settings.operation_result_max_bytes)
        return ResultCheck(None, "too large")
    return ResultCheck(payload)


def _to_dict(op: Any) -> dict[str, Any]:
    return {
        "id": op.id,
        "org_id": op.org_id,
        "operation_type": op.operation_type,
        "resource_type": op.resource_type,
        "resource_id": op.resource_id,
        "status": op.status,
        "result_status": getattr(op, "result_status", None),
        "result": op.result,
        "error_message": op.error_message,
        "error_code": op.error_code,
        "retryable": op.retryable,
        "correlation_id": op.correlation_id,
        "created_at": op.created_at,
        "started_at": op.started_at,
        "completed_at": op.completed_at,
        "attempt_count": op.attempt_count,
    }


async def create_and_run(
    *,
    org_id: str,
    operation_type: str,
    resource_type: str,
    resource_id: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Durably records the submission (status 'queued') and returns
    immediately; the worker loop (`start_worker`, started from the app
    lifespan) claims and runs it — see `_poll_loop`/`_claim_next` below.

    `operation_id`, when given, is used as this row's id instead of a
    freshly generated one (OperationRepository.create only fills in a
    random one via setdefault) — FE-BUG-25's pipeline lifecycle routes
    pre-generate it so the SAME id can be used for an atomic pipeline claim
    (PipelineService.claim_lifecycle_transition) before this row even
    exists, rather than creating the Operation first and having no way to
    connect it to that claim afterward.
    """
    async with AsyncSessionLocal() as session:
        repo = OperationRepository(session)
        values: dict[str, Any] = {
            "org_id": org_id,
            "operation_type": operation_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": "queued",
        }
        if operation_id is not None:
            values["id"] = operation_id
        op = await repo.create(values)
        await session.commit()
        op_dict = _to_dict(op)

    _wake_event.set()
    return op_dict


async def get_operation(org_id: str, operation_id: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        op = await OperationRepository(session).get_for_org(org_id, operation_id)
        return _to_dict(op) if op else None


# ── Claim/lease worker ────────────────────────────────────────────────────

async def start_worker() -> None:
    """Idempotent — safe to call more than once (a no-op if already running)."""
    global _worker_task
    if _worker_task is None:
        _worker_task = asyncio.create_task(_poll_loop())


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
        _worker_task = None

    for task in list(_running_operations):
        task.cancel()
    for task in list(_running_operations):
        with suppress(asyncio.CancelledError):
            await task


async def _poll_loop() -> None:
    while True:
        try:
            claimed = await _claim_next()
        except Exception:  # noqa: BLE001 - a claim failure (e.g. DB briefly down) must not kill the worker loop
            logger.warning("operation_worker.claim_failed", exc_info=True)
            claimed = None

        if claimed is not None:
            task = asyncio.create_task(_execute(claimed))
            _running_operations.add(task)
            task.add_done_callback(_running_operations.discard)
            continue  # try to claim another right away rather than waiting out the poll interval

        _wake_event.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake_event.wait(), timeout=settings.operation_poll_interval_seconds)


async def _claim_next() -> dict[str, Any] | None:
    """Atomically claims one queued-or-abandoned Operation: `queued` rows,
    or `running` rows whose lease has lapsed (including a NULL lease, which
    only ever happens on a row claimed by the pre-lease code before this
    fix, or in the — otherwise unreachable — gap between two rows racing to
    claim the same abandoned row). `FOR UPDATE SKIP LOCKED` inside the
    subquery is what makes this safe if more than one process ever polls
    concurrently: a locked-but-not-yet-committed candidate row is simply
    skipped rather than raced for."""
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=settings.operation_lease_seconds)

    async with AsyncSessionLocal() as session:
        candidate = (
            select(Operation.id)
            .where(
                or_(
                    Operation.status == "queued",
                    and_(
                        Operation.status == "running",
                        or_(Operation.lease_expires_at.is_(None), Operation.lease_expires_at < now),
                    ),
                )
            )
            .order_by(Operation.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Operation)
            # `id = (scalar subquery)`, not `id IN (subquery)`: with IN, Postgres may re-run the
            # LIMIT 1 ... FOR UPDATE SKIP LOCKED subquery per row and update EVERY claimable row in
            # one statement while RETURNING yields just one, stranding the rest as `running` until
            # their lease lapses. A scalar subquery is evaluated exactly once.
            .where(Operation.id == candidate.scalar_subquery())
            .values(
                status="running",
                started_at=func.coalesce(Operation.started_at, now),
                lease_expires_at=lease_until,
                attempt_count=Operation.attempt_count + 1,
            )
            .returning(Operation)
            .execution_options(synchronize_session=False)
        )
        result = await session.execute(stmt)
        row = result.scalars().first()
        await session.commit()
        return _to_dict(row) if row is not None else None


async def _execute(op: dict[str, Any]) -> None:
    operation_id = op["id"]

    if op["attempt_count"] > settings.operation_max_attempts:
        # FE-BUG-05: "stale", not "failed" — nothing about this operation's
        # own work was ever observed to error out; every claim of it just
        # never reached a terminal status before its lease lapsed (a worker
        # process dying mid-run, repeatedly). That's a meaningfully
        # different thing to tell a caller than "this failed with a real
        # error" — see app/models/operations.py's status column comment.
        safe_error = manual_safe_error(
            "ATTEMPTS_EXHAUSTED",
            (
                f"Gave up after {op['attempt_count']} attempts — this operation was repeatedly "
                "claimed but never reached a terminal status, including across process restarts."
            ),
            retryable=False, event="operation.attempts_exhausted",
            operation_id=operation_id, attempt_count=op["attempt_count"],
        )
        await _finish(operation_id, status="stale", safe_error=safe_error)
        return

    factory = _work_factories.get(op["operation_type"])
    if factory is None:
        safe_error = manual_safe_error(
            "NO_WORK_REGISTERED", "No work is registered for this operation type.",
            retryable=False, event="operation.no_work_registered",
            operation_id=operation_id, operation_type=op["operation_type"],
        )
        await _finish(operation_id, status="failed", safe_error=safe_error)
        return

    heartbeat_task = asyncio.create_task(_heartbeat(operation_id))
    try:
        work = factory(op["org_id"], op["resource_id"])
        result = await work()
    except OperationResultInvalid as exc:
        # The work ran to completion — its side effects happened — and only the result it then built is malformed. Not a failure.
        logger.error(
            "operation.result_rejected", operation_id=operation_id, operation_type=op["operation_type"],
            reason="does not match its contract", errors=exc.errors,
        )
        await _finish_result_invalid(op, reason="does not match its contract")
        return
    except Exception as exc:  # noqa: BLE001 - a background operation's failure must be recorded, never lost to a bare task exception
        safe_error = to_safe_error(exc, event="operation.failed", operation_id=operation_id)
        await _finish(operation_id, status="failed", safe_error=safe_error)
        return
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

    checked = check_result(op["operation_type"], operation_id, result)
    if not checked.valid:
        await _finish_result_invalid(op, reason=checked.reason or "invalid")
        return
    await _finish(operation_id, status="succeeded", result_status="valid", result=checked.payload)
    logger.info("operation.succeeded", operation_id=operation_id)


async def _finish_result_invalid(op: dict[str, Any], *, reason: str) -> None:
    """GAP-04: the action succeeded but its result cannot be stored. Execution outcome and result-contract outcome are recorded
    separately — status=succeeded, result_status=invalid, error_code=OPERATION_RESULT_INVALID — and counted, so a defect in a work
    callable's result can no longer sit behind a plain success that only a log line contradicts."""
    record_operation_result_invalid(operation_type=op["operation_type"], known_types=frozenset(_result_models))
    safe_error = manual_safe_error(
        "OPERATION_RESULT_INVALID",
        (
            f"The operation completed, but its result could not be recorded ({reason}). The action itself took effect — "
            "check the resource's current state rather than repeating the operation."
        ),
        retryable=False, event="operation.result_invalid",
        operation_id=op["id"], operation_type=op["operation_type"], reason=reason,
    )
    await _finish(op["id"], status="succeeded", result_status="invalid", safe_error=safe_error)


async def _heartbeat(operation_id: str) -> None:
    """Renews the lease partway through so a still-running, still-alive
    worker never loses its own claim to another worker's poll — see
    settings.operation_lease_seconds."""
    interval = settings.operation_lease_seconds / 2
    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as session:
                repo = OperationRepository(session)
                op = await repo.get(operation_id)
                if op is None or op.status != "running":
                    return
                await repo.update(
                    op,
                    {"lease_expires_at": datetime.now(UTC) + timedelta(seconds=settings.operation_lease_seconds)},
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - a transient renewal failure shouldn't kill an otherwise-healthy run; worst case the lease lapses and another worker reclaims it
            logger.warning("operation.heartbeat_failed", operation_id=operation_id, exc_info=True)


async def _finish(
    operation_id: str, *, status: str, result: dict[str, Any] | None = None, safe_error: SafeError | None = None,
    result_status: str | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        repo = OperationRepository(session)
        op = await repo.get(operation_id)
        if op is None:
            return
        values: dict[str, Any] = {"status": status, "completed_at": datetime.now(UTC)}
        if result_status is not None:
            values["result_status"] = result_status
        if result is not None:
            values["result"] = result
        if safe_error is not None:
            values["error_message"] = safe_error.message
            values["error_code"] = safe_error.code
            values["retryable"] = safe_error.retryable
            values["correlation_id"] = safe_error.correlation_id
        await repo.update(op, values)
        await session.commit()

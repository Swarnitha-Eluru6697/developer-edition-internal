"""Execution router — durable submission and the execution worker.

Submission (API pods): `submit()` persists the execution as an immutable
command row in status "pending", commits, wakes the workers over Redis, and
returns. Nothing about running it happens in the request (WS-EXEC-005).

Execution (the worker: `python -m app.worker`, or in-process when
`execution_worker_enabled`): workers poll for pending rows (plus an instant
Redis wake-up), claim each with an atomic conditional UPDATE so a row runs
exactly once across any number of worker processes, and run up to
`execution_worker_concurrency` at a time. The claim also records the owner
and a lease expiry ON THE ROW (W-018), so a running row always has a durable
owner; the worker extends the lease while it runs, and a row whose lease
lapsed belongs to a dead worker and is recovered by the reconciler. Redis is
never the source of truth for ownership.

Dispatch by language / target:
  - 'sql' to a lakehouse warehouse      → SqlExecutor (Data Lake House)
  - 'sql' to `connection:<id>`          → ConnectionSqlExecutor, in-process:
    credentials are resolved here and never leave the worker (WS-DATA-007)
  - 'ipynb_cell'                        → NotebookExecutor (JupyterHub) or the
    dev-only local kernel; the kernel is allocated here, not in the request

Every status change goes through transition() — the execution state machine
(WS-EXEC-006): pending → running → completed | failed | cancelled | timeout,
plus pending → cancelled. Exactly one writer wins each transition, and only
the winner publishes, so each execution gets exactly one terminal event and
it always matches the database.

Cancellation is restart-safe: a cancel request is `cancel_requested_at` on the
execution row (W-017), so it outlives an API, worker or Redis restart. The
owning worker sees it on its next lease heartbeat (a Redis key is only a hint
that makes it look sooner) and stops the run (lakehouse cancel / kernel
interrupt / cancelling the in-process query); the row ends "cancelled". If the
worker is gone, the reconciler ends the orphaned row "cancelled" too.

Events are published to Redis channels ws:execution:{id} and
ws:session:{session_id}; the WebSocket gateway forwards them to the client.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.executors.connection_sql_executor import ConnectionSqlExecutor
from app.executors.local_kernel_executor import LocalKernelExecutor
from app.executors.notebook_executor import NotebookExecutor
from app.executors.sql_executor import SqlChunk, SqlExecutor
from app.models import Execution, ExecutionOutput

log = structlog.get_logger()

_redis: redis.Redis | None = None

WAKE_CHANNEL = "executions:submitted"
CONNECTION_TARGET_PREFIX = "connection:"


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def _cancel_key(execution_id) -> str:
    """A wake-up hint only: the durable record is executions.cancel_requested_at."""
    return f"exec-cancel:{execution_id}"


# The execution state machine (WS-EXEC-006). Every status write goes through
# transition(), which applies it as a single conditional UPDATE — so of any
# writers racing on one row (two workers, a cancel, a reconcile), exactly one
# wins, and only the winner publishes the terminal event.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled", "timeout"}),
}


async def transition(
    db: AsyncSession, execution_id: uuid.UUID, to: str, *,
    from_: tuple[str, ...] | None = None, **values,
) -> Execution | None:
    """Atomically move an execution into `to` from any status allowed to
    reach it (optionally narrowed to `from_`), committing. Returns the
    freshly-read row, or None if the row wasn't in an allowed status
    (someone else already moved it) — the caller must then do nothing
    further, including publishing."""
    allowed_from = [frm for frm, tos in _TRANSITIONS.items() if to in tos]
    if from_ is not None:
        if not set(from_) <= set(allowed_from):
            raise ValueError(f"invalid transition {from_} -> {to!r}")
        allowed_from = list(from_)
    if not allowed_from:
        raise ValueError(f"no transition leads to status {to!r}")
    result = await db.execute(
        update(Execution)
        .where(Execution.id == execution_id, Execution.status.in_(allowed_from))
        .values(status=to, **values)
        .returning(Execution.id)
        .execution_options(synchronize_session=False)
    )
    won = result.first() is not None
    await db.commit()
    if not won:
        return None
    # populate_existing: the session is expire_on_commit=False, so a plain
    # get() would hand back the stale identity-map copy, not what we wrote.
    return await db.get(Execution, execution_id, populate_existing=True)


def _as_utc(dt: datetime) -> datetime:
    """Timestamps are stored timezone-aware; treat a naive one (a driver that
    drops the offset) as UTC rather than failing the comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def channel_for(execution_id: str) -> str:
    return f"ws:execution:{execution_id}"


def session_channel_for(session_id: str) -> str:
    return f"ws:session:{session_id}"


class _Cancelled(Exception):
    """The run was stopped because a cancel was requested."""


class ExecutionRouter:
    def __init__(self):
        self._sql = SqlExecutor()
        self._conn_sql = ConnectionSqlExecutor()
        self._notebook = NotebookExecutor()
        self._local = LocalKernelExecutor()
        self._wake = asyncio.Event()
        self._inflight: set[uuid.UUID] = set()
        self._tasks: list[asyncio.Task] = []
        self._runs: set[asyncio.Task] = set()
        self._last_dispatch_tick: datetime | None = None
        self.worker_running = False
        # Identifies this worker in executions.lease_owner.
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the worker: reconcile once, then the dispatch loop, the
        Redis wake listener and the periodic reconcile. Raises if the first
        reconcile can't reach the database — a worker that can't see its
        queue must not report itself started."""
        await self._reconcile()
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="execution-dispatch"),
            asyncio.create_task(self._wake_listener(), name="execution-wake"),
            asyncio.create_task(self._reconcile_loop(), name="execution-reconcile"),
        ]
        self.worker_running = True

    async def stop(self) -> None:
        self.worker_running = False
        for task in [*self._tasks, *self._runs]:
            task.cancel()
        for task in [*self._tasks, *self._runs]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    def healthy(self) -> bool:
        """For readiness/liveness: the worker loops are alive and the
        dispatcher has ticked recently."""
        if not self.worker_running or any(t.done() for t in self._tasks):
            return False
        if self._last_dispatch_tick is None:
            return False
        stale_after = max(30.0, get_settings().execution_poll_interval_seconds * 5)
        return (datetime.now(timezone.utc) - self._last_dispatch_tick).total_seconds() < stale_after

    # ── submission (API side) ────────────────────────────────────────────

    async def submit(
        self,
        db: AsyncSession,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        file_id: uuid.UUID | None,
        cell_id: str | None,
        language: str,
        code_text: str,
        target: str | None,
        max_rows: int,
        timeout_seconds: int,
        request_id: str | None = None,
        trace_id: str | None = None,
        principal_groups: list[str] | None = None,
        principal_permissions: list[str] | None = None,
    ) -> Execution:
        """Persist the execution as an immutable command; return at once
        with status "pending". The commit is what makes it durable — the
        Redis wake-up is only a latency optimisation (workers also poll)."""
        execution = Execution(
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
            file_id=file_id,
            cell_id=cell_id,
            language=language,
            code_text=code_text,
            code_sha256=hashlib.sha256(code_text.encode()).hexdigest(),
            target=target,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
            status="pending",
            request_id=request_id,
            trace_id=trace_id,
            principal_groups=json.dumps(list(principal_groups or [])),
            principal_permissions=json.dumps(list(principal_permissions or [])),
        )
        db.add(execution)
        await db.flush()
        await db.commit()
        try:
            await get_redis().publish(WAKE_CHANNEL, str(execution.id))
        except Exception as e:  # noqa: BLE001 — workers poll anyway
            log.warning("execution_wake_publish_failed", error=str(e))
        return execution

    async def cancel(self, execution_id: uuid.UUID, language: str | None = None) -> str:
        """Cancel an execution wherever it is. Returns the resulting state:
        "cancelled" (it was still queued), "cancelling" (running — its
        worker will stop it), or "finished" (already terminal)."""
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            execution = await transition(
                db, execution_id, "cancelled", from_=("pending",),
                completed_at=datetime.now(timezone.utc),
            )
            if execution:
                await self._publish_terminal(get_redis(), execution)
                return "cancelled"
            # The durable request (W-017): written to the row, conditional on
            # it still running, and kept at its first time if repeated.
            result = await db.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == "running")
                .values(cancel_requested_at=func.coalesce(Execution.cancel_requested_at, datetime.now(timezone.utc)))
                .returning(Execution.timeout_seconds)
                .execution_options(synchronize_session=False)
            )
            row = result.first()
            await db.commit()
            if row is None:
                return "finished"
        # Redis is only a wake-up so the worker looks at the row sooner; the
        # request is already safe without it.
        try:
            ttl = row.timeout_seconds + get_settings().execution_stale_grace_seconds
            await get_redis().set(_cancel_key(execution_id), "1", ex=ttl)
        except Exception as e:  # noqa: BLE001
            log.warning("execution_cancel_hint_failed", execution_id=str(execution_id), error=str(e))
        return "cancelling"

    # ── worker side ──────────────────────────────────────────────────────

    async def _wake_listener(self) -> None:
        while True:
            try:
                pubsub = get_redis().pubsub()
                await pubsub.subscribe(WAKE_CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") == "message":
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — polling covers the gap
                log.warning("execution_wake_listener_error", error=str(e))
                await asyncio.sleep(5)

    async def _dispatch_loop(self) -> None:
        from app.db.session import AsyncSessionLocal

        settings = get_settings()
        concurrency = max(1, settings.execution_worker_concurrency)
        while True:
            self._last_dispatch_tick = datetime.now(timezone.utc)
            free = concurrency - len(self._inflight)
            if free > 0:
                try:
                    async with AsyncSessionLocal() as db:
                        q = (
                            select(Execution.id)
                            .where(Execution.status == "pending")
                            .order_by(Execution.created_at)
                            .limit(free + len(self._inflight))
                        )
                        ids = [i for i in (await db.execute(q)).scalars() if i not in self._inflight][:free]
                except Exception as e:  # noqa: BLE001
                    log.warning("execution_dispatch_poll_failed", error=str(e))
                    ids = []
                for execution_id in ids:
                    self._inflight.add(execution_id)
                    task = asyncio.create_task(self._run_guarded(execution_id))
                    self._runs.add(task)
                    task.add_done_callback(self._runs.discard)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=settings.execution_poll_interval_seconds)

    async def _run_guarded(self, execution_id: uuid.UUID) -> None:
        try:
            await self._run(execution_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("execution_worker_run_failed", execution_id=str(execution_id))
        finally:
            self._inflight.discard(execution_id)
            self._wake.set()  # a slot freed up

    async def _reconcile_loop(self) -> None:
        interval = min(get_settings().lifecycle_reconcile_interval_seconds,
                       get_settings().execution_lease_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._reconcile()
            except Exception:
                log.exception("execution_reconcile_loop_failed")

    async def _reconcile(self) -> None:
        """Recover 'running' rows whose worker died (WS-LIFE-013).

        A running row is orphaned when its lease (`lease_expires_at`, written
        by the claim and extended by the worker's heartbeat) has lapsed. A row
        with no lease — claimed by an older worker that kept it in Redis — is
        orphaned only once it has overrun its own timeout plus grace. Orphans
        end "cancelled" if a cancel had been requested (`cancel_requested_at`),
        else "failed" (INTERRUPTED). Everything is read from the row: Redis is
        not consulted. Pending rows need nothing: the dispatcher polls them.
        Safe on any number of workers: each recovery is a conditional
        transition, so only one reconciler acts on a given row.
        """
        from app.db.session import AsyncSessionLocal

        settings = get_settings()
        now = datetime.now(timezone.utc)
        grace = timedelta(seconds=settings.execution_stale_grace_seconds)
        r = get_redis()

        async with AsyncSessionLocal() as db:
            running = (await db.execute(
                select(Execution).where(Execution.status == "running")
            )).scalars().all()
            recovered = 0
            for e in running:
                if e.started_at is None:
                    continue
                if e.lease_expires_at is not None:
                    orphaned = now > _as_utc(e.lease_expires_at)
                else:
                    orphaned = now - _as_utc(e.started_at) > timedelta(seconds=e.timeout_seconds) + grace
                if not orphaned:
                    continue
                execution_id = e.id
                if e.cancel_requested_at is not None:
                    execution = await transition(db, execution_id, "cancelled", from_=("running",), completed_at=now)
                else:
                    execution = await transition(
                        db, execution_id, "failed", from_=("running",),
                        error_code="INTERRUPTED",
                        error_message="The worker running this execution stopped before it finished.",
                        completed_at=now,
                    )
                if execution:
                    recovered += 1
                    await self._publish_terminal(r, execution)
            if recovered:
                log.warning("execution_reconcile_recovered", count=recovered)

    def _use_local_python(self) -> bool:
        """Prefer JupyterHub when configured; otherwise the dev-only
        in-process IPython kernel (refused outside dev at startup)."""
        return not self._notebook.configured and get_settings().local_python_kernel_enabled

    def _python_executor(self):
        return self._local if self._use_local_python() else self._notebook

    async def ensure_kernel_for(self, user_id: uuid.UUID, existing_kernel_id: str | None = None) -> str | None:
        """Allocate (or reuse) a Python kernel and return its id."""
        if self._notebook.configured:
            info = await self._notebook.ensure_kernel(self._notebook.username_for(str(user_id)))
            return info.get("id")
        if self._use_local_python():
            return await self._local.ensure_kernel(existing_kernel_id)
        return None

    async def shutdown_kernel(self, user_id: uuid.UUID, kernel_id: str) -> None:
        """Release a session's kernel (WS-LIFE-013)."""
        if self._notebook.configured:
            await self._notebook.shutdown(user_id=str(user_id), kernel_id=kernel_id)
        else:
            await self._local.shutdown(kernel_id)

    async def _session_kernel(self, db: AsyncSession, execution: Execution) -> str | None:
        """The kernel for this cell's session, allocating it on first use —
        here in the worker, not in the submit request. Serialised per session
        with a Redis lock so two first cells don't each start a kernel."""
        from app.models import Session as SessionModel

        r = get_redis()
        lock = r.lock(f"kernel-alloc:{execution.session_id}", timeout=180, blocking_timeout=180)
        async with lock:
            session = await db.get(SessionModel, execution.session_id, populate_existing=True)
            kernel_id = (session.runtime_metadata or {}).get("kernel_id") if session else None
            if not kernel_id:
                kernel_id = await self.ensure_kernel_for(execution.user_id)
                if kernel_id and session is not None:
                    session.runtime_metadata = {**(session.runtime_metadata or {}), "kernel_id": kernel_id}
            if kernel_id:
                await db.execute(
                    update(Execution).where(Execution.id == execution.id).values(target=kernel_id)
                    .execution_options(synchronize_session=False)
                )
            await db.commit()
        return kernel_id

    async def _authorize_connection(self, db: AsyncSession, execution: Execution):
        """Load the connection and re-check, at run time, that the submitter
        may still use it (WS-DATA-007). Returns (conn, None) or (None, error)."""
        from app.core.auth import Principal, load_list
        from app.models import DatabaseConnection, User
        from app.services.ucs_client import UcsAuthorizationError, authorize_connection_use

        conn_id = uuid.UUID(execution.target.removeprefix(CONNECTION_TARGET_PREFIX))
        conn = await db.get(DatabaseConnection, conn_id)
        if conn is None:
            return None, ("CONNECTION_NOT_FOUND", "The selected database connection no longer exists.")
        user = await db.get(User, execution.user_id)
        if user is None:
            return None, ("FORBIDDEN", "The submitting user no longer exists.")
        principal = Principal(
            user_id=user.external_id, org_id=get_settings().org_id, roles=[],
            groups=load_list(execution.principal_groups), permissions=frozenset(load_list(execution.principal_permissions)),
            # the catalog policy engine knows the person by their gateway username, not by their subject
            email=user.username or user.email or user.external_id, issued_at=0, expires_at=0,
            username=user.username,
        )
        try:
            await authorize_connection_use(principal, user.id, conn)
        except UcsAuthorizationError as e:
            return None, ("FORBIDDEN", f"Not authorized to use connection '{conn.name}': {e.reason}")
        return conn, None

    async def _heartbeat(self, execution_id: uuid.UUID) -> tuple[bool, bool]:
        """Extend this worker's lease on the row and read the cancel request,
        in one statement. Returns (still_ours, cancel_requested)."""
        from app.db.session import AsyncSessionLocal

        lease = timedelta(seconds=get_settings().execution_lease_seconds)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status == "running",
                       Execution.lease_owner == self.owner)
                .values(lease_expires_at=datetime.now(timezone.utc) + lease)
                .returning(Execution.cancel_requested_at)
                .execution_options(synchronize_session=False)
            )
            row = result.first()
            await db.commit()
        if row is None:
            return False, False
        return True, row.cancel_requested_at is not None

    async def _heartbeat_uninterrupted(self, execution_id: uuid.UUID) -> tuple[bool, bool]:
        """The heartbeat's write must finish (commit or roll back) even if the
        watcher is cancelled meanwhile — a half-done UPDATE would keep the row
        locked against the run's own final transition."""
        task = asyncio.ensure_future(self._heartbeat(execution_id))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda t: t.cancelled() or t.exception())   # mark any error as seen
            raise

    async def _hold_lease_and_watch_cancel(self, execution_id: uuid.UUID, on_cancel) -> None:
        """While a run is active: extend its lease on the row, and call
        on_cancel() once if the row records a cancel request — or if the row is
        no longer ours (the reconciler gave up on it), because nobody wants
        the result any more. The heartbeat runs every lease/3, and at once when
        the Redis hint says a cancel was just requested."""
        r = get_redis()
        lease_s = get_settings().execution_lease_seconds
        step = max(1.0, min(2.0, lease_s / 3))
        refreshed = float("-inf")
        while True:
            try:
                now = asyncio.get_running_loop().time()
                hinted = False
                with contextlib.suppress(Exception):   # a hint only: the row is the truth
                    hinted = bool(await r.exists(_cancel_key(execution_id)))
                if hinted or now - refreshed >= lease_s / 3:
                    ours, cancel_requested = await self._heartbeat_uninterrupted(execution_id)
                    refreshed = now
                    if not ours:
                        log.warning("execution_lease_lost", execution_id=str(execution_id))
                    if cancel_requested or not ours:
                        await on_cancel()
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("execution_lease_error", execution_id=str(execution_id), error=str(e))
            await asyncio.sleep(step)

    async def _run(self, execution_id: uuid.UUID) -> None:
        from app.db.session import AsyncSessionLocal

        r = get_redis()
        settings = get_settings()
        ttl = timedelta(hours=settings.execution_output_retention_hours)

        async with AsyncSessionLocal() as db:
            # Atomic claim: only the worker whose UPDATE flips the row from
            # 'pending' to 'running' runs it. Cancelled/claimed elsewhere → skip.
            # The owner and lease are written by this same UPDATE (W-018): a
            # row is never running without a durable owner, whatever Redis does.
            started_at = datetime.now(timezone.utc)
            execution = await transition(
                db, execution_id, "running", from_=("pending",), started_at=started_at,
                lease_owner=self.owner,
                lease_expires_at=started_at + timedelta(seconds=settings.execution_lease_seconds),
            )
            if not execution:
                return

            structlog.contextvars.bind_contextvars(
                execution_id=str(execution_id), session_id=str(execution.session_id),
                workspace_id=str(execution.workspace_id), user_id=str(execution.user_id),
                request_id=execution.request_id, trace_id=execution.trace_id,
            )
            try:
                await self._run_claimed(db, r, execution, started_at, ttl)
            finally:
                structlog.contextvars.unbind_contextvars(
                    "execution_id", "session_id", "workspace_id", "user_id", "request_id", "trace_id",
                )
                with contextlib.suppress(Exception):
                    await r.delete(_cancel_key(execution_id))

    async def _run_claimed(self, db, r, execution: Execution, started_at: datetime, ttl: timedelta) -> None:
        execution_id = execution.id
        session_id = str(execution.session_id)
        language = execution.language
        user_id = str(execution.user_id)

        # Best effort: a Redis failure must not strand the row we just claimed.
        with contextlib.suppress(Exception):
            await self._publish(r, str(execution_id), session_id, {
                "type": "execution.started",
                "execution_id": str(execution_id),
                "started_at": started_at.isoformat(),
            })

        state = {"chunk_index": 0, "total_rows": 0, "query_error": None, "cancelled": False}

        async def consume() -> None:
            async for chunk in self._chunks(db, execution):
                if chunk.output_type == "error":
                    state["query_error"] = chunk.payload
                db.add(ExecutionOutput(
                    execution_id=execution_id,
                    chunk_index=state["chunk_index"],
                    output_type=chunk.output_type,
                    payload=chunk.payload,
                    row_count=chunk.row_count,
                    expires_at=datetime.now(timezone.utc) + ttl,
                ))
                await db.commit()
                msg_type = self._chunk_to_ws_type(language, chunk.output_type)
                if msg_type is not None:
                    await self._publish(r, str(execution_id), session_id, {
                        "type": msg_type,
                        "execution_id": str(execution_id),
                        "chunk_index": state["chunk_index"],
                        "output_type": chunk.output_type,
                        "payload": chunk.payload,
                    })
                state["chunk_index"] += 1
                state["total_rows"] += chunk.row_count
                if chunk.is_final:
                    break

        run_task = asyncio.create_task(consume())

        async def on_cancel() -> None:
            state["cancelled"] = True
            log.info("execution_cancel_honoured", execution_id=str(execution_id))
            # Ask the engine to stop (lakehouse cancel / kernel interrupt),
            # then stop reading. For an in-process connection query,
            # cancelling the task makes asyncpg cancel it server-side.
            with contextlib.suppress(Exception):
                await self._engine_cancel(execution, user_id)
            run_task.cancel()

        watcher = asyncio.create_task(self._hold_lease_and_watch_cancel(execution_id, on_cancel))
        try:
            try:
                await run_task
            except asyncio.CancelledError:
                if not state["cancelled"]:
                    raise  # the worker itself is shutting down
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            if state["cancelled"]:
                await db.rollback()  # the stream may have been cut mid-commit

            completed_at = datetime.now(timezone.utc)
            values = {
                "completed_at": completed_at,
                "row_count": state["total_rows"],
                "duration_ms": int((completed_at - started_at).total_seconds() * 1000),
            }
            query_error = state["query_error"]
            if state["cancelled"]:
                to = "cancelled"
            elif query_error is not None:
                error_code = query_error.get("error_code", "QUERY_ERROR")
                to = "timeout" if error_code == "TIMEOUT" else "failed"
                values["error_code"] = error_code
                values["error_message"] = query_error.get("error_message", "Query failed")
            else:
                to = "completed"
            # Conditional on still 'running': a reconcile that already
            # recovered this row keeps its outcome, and only one terminal
            # event is ever published.
            final = await transition(db, execution_id, to, from_=("running",), **values)
            if final:
                await self._publish_terminal(r, final)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("execution_failed", execution_id=str(execution_id))
            # A failure may have come from db.commit() mid-stream, leaving the
            # session needing a rollback before any further use.
            await db.rollback()
            final = await transition(
                db, execution_id, "failed", from_=("running",),
                error_code="INTERNAL_ERROR", error_message=str(e)[:1000],
                completed_at=datetime.now(timezone.utc),
            )
            if final:
                await self._publish_terminal(r, final)

    async def _chunks(self, db, execution: Execution):
        """The chunk stream for one execution, from whichever engine runs it."""
        settings = get_settings()
        if execution.language == "sql" and (execution.target or "").startswith(CONNECTION_TARGET_PREFIX):
            conn, err = await self._authorize_connection(db, execution)
            if err:
                yield SqlChunk(output_type="error", payload={"error_code": err[0], "error_message": err[1]},
                               row_count=0, is_final=True)
                return
            async for chunk in self._conn_sql.execute(
                conn=conn, code_text=execution.code_text, max_rows=execution.max_rows,
                timeout_seconds=execution.timeout_seconds, chunk_size=settings.query_chunk_size,
            ):
                yield chunk
            return

        target = execution.target
        if execution.language == "sql":
            executor = self._sql
        else:
            executor = self._python_executor()
            if not target:
                target = await self._session_kernel(db, execution)
        async for chunk in executor.execute(
            execution_id=str(execution.id), user_id=str(execution.user_id),
            code_text=execution.code_text, target=target,
            max_rows=execution.max_rows, timeout_seconds=execution.timeout_seconds,
        ):
            yield chunk

    async def _engine_cancel(self, execution: Execution, user_id: str) -> None:
        if execution.language == "sql":
            if (execution.target or "").startswith(CONNECTION_TARGET_PREFIX):
                return  # in-process: cancelling the consume task cancels the query
            await self._sql.cancel(execution_id=str(execution.id), user_id=user_id, target=execution.target)
            return
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:   # the kernel id may have been allocated during the run
            fresh = await db.get(Execution, execution.id)
            target = fresh.target if fresh else execution.target
        await self._python_executor().cancel(execution_id=str(execution.id), user_id=user_id, target=target)

    # One terminal WS event per persisted terminal status (WS-EXEC-006). This
    # mapping IS the contract: an event's `type` can never disagree with the
    # database, because both are derived from the same `execution.status`.
    _TERMINAL_EVENT_FOR_STATUS = {
        "completed": "execution.completed",
        "failed": "execution.failed",
        "cancelled": "execution.cancelled",
        "timeout": "execution.timeout",
    }

    async def _publish_terminal(self, r: redis.Redis, execution: Execution) -> None:
        """The only place that ever announces an execution is over. Reads
        the event type from the persisted status; callers only invoke it
        after winning transition(), so each execution gets exactly one."""
        event_type = self._TERMINAL_EVENT_FOR_STATUS.get(execution.status)
        if event_type is None:
            return
        msg = {
            "type": event_type,
            "execution_id": str(execution.id),
            "status": execution.status,
            "total_rows": execution.row_count,
            "duration_ms": execution.duration_ms,
        }
        if execution.status in ("failed", "timeout"):
            msg["error_code"] = execution.error_code
            msg["error_message"] = execution.error_message
        await self._publish(r, str(execution.id), str(execution.session_id), msg)

    @staticmethod
    def _chunk_to_ws_type(language: str, output_type: str) -> str | None:
        """Maps a chunk to a WS event for client-facing *content* only —
        never a terminal outcome (that's _publish_terminal's alone). None
        for 'done' markers and SQL 'error' chunks (the error rides in the
        terminal event)."""
        if language == "sql":
            # "statement" closes one statement of a multi-statement script (row total / status).
            if output_type in ("rows", "statement"):
                return "execution.chunk"
            return None
        if output_type in ("stream", "display_data", "execute_result"):
            return f"execution.output.{output_type}"
        if output_type == "error":
            return "execution.output.error"  # inline traceback content, not a terminal claim
        return None

    @staticmethod
    async def _publish(r: redis.Redis, execution_id: str, session_id: str, msg: dict) -> None:
        payload = json.dumps({"v": 1, "ts": datetime.now(timezone.utc).isoformat(), **msg})
        # Per-execution (cell-scoped clients) and per-session (workspace UI).
        await r.publish(channel_for(execution_id), payload)
        await r.publish(session_channel_for(session_id), payload)


_router: ExecutionRouter | None = None


def get_execution_router() -> ExecutionRouter:
    global _router
    if _router is None:
        _router = ExecutionRouter()
    return _router

"""Lifecycle reconciler (WS-LIFE-013).

Scheduled/reconciled cleanup for the state this service accumulates that
nothing else ever revisits: idle sessions, expired execution outputs,
soft-deleted file blobs past their retention window, and codegen turns
orphaned by a mid-turn crash.

Runs as one periodic background task, started and stopped from
app/main.py's lifespan the same way ExecutionRouter already is (see
ExecutionRouter.start()/stop()) — a crash loses at most one interval's
worth of delay, never correctness: every sweep below is a plain
idempotent DB scan, safe to re-run from scratch on the next pass or after
a restart with no state of its own to reconstruct.

Execution reconciliation itself (interrupted/pending runs) stays inside
ExecutionRouter — it already owns the Redis publish helpers and terminal-
event contract for executions; duplicating that here would risk the two
diverging. This module owns everything execution reconciliation doesn't:
sessions, outputs, file blobs, and codegen turns.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete as sa_delete, select

from app.core.config import Settings, get_settings
from app.models import CodegenMessage, ExecutionOutput
from app.models import Session as SessionModel
from app.models import Workspace, WorkspaceFile
from app.services.execution_router import get_redis, session_channel_for
from app.storage import get_blob_store, storage_key_for_file

log = structlog.get_logger()


class LifecycleReconciler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Run one sweep immediately (so a restart doesn't wait a full
        interval before catching up on what accumulated while it was down),
        then start the periodic loop. Call once at app startup."""
        await self.run_once()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the periodic loop. Call once at app shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        interval = get_settings().lifecycle_reconcile_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_once()
            except Exception:
                log.exception("lifecycle_reconcile_failed")

    async def run_once(self) -> None:
        """One full sweep. Safe to call concurrently with itself in theory
        (each query is a plain idempotent scan+update), but start()/the loop
        never overlap calls in practice."""
        from app.db.session import AsyncSessionLocal

        settings = get_settings()
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as db:
            idle = await self._sweep_idle_sessions(db, now, settings)
            kernels = await self._sweep_session_kernels(db)
            outputs = await self._sweep_expired_outputs(db, now)
            files = await self._sweep_soft_deleted_files(db, now, settings)
            turns = await self._sweep_stale_codegen_turns(db, now, settings)

        if idle or kernels or outputs or files or turns:
            log.info(
                "lifecycle_reconcile_swept",
                idle_sessions=idle, released_kernels=kernels, expired_outputs=outputs,
                purged_files=files, stale_codegen_turns=turns,
            )

    async def _sweep_idle_sessions(self, db, now: datetime, settings: Settings) -> int:
        """Sessions with no WS activity for session_idle_timeout_minutes get
        closed out — see gateway.py's session.activity handler for what
        updates last_activity_at, and ix_sessions_idle_cleanup for the index
        this scan uses."""
        cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
        rows = (await db.execute(
            select(SessionModel).where(
                SessionModel.status == "active",
                SessionModel.last_activity_at < cutoff,
            )
        )).scalars().all()
        for s in rows:
            s.status = "idle"
            s.ended_at = now
        if rows:
            await db.commit()
        return len(rows)

    async def _sweep_session_kernels(self, db) -> int:
        """Shut down the kernel of every idle or terminated session that
        still holds one — kernels were never released before, so each closed
        tab leaked a kernel process/pod. Skips a session with an execution
        still pending/running (its cell must finish first). Clearing
        kernel_id means a resumed idle session gets a fresh kernel on its
        next cell."""
        from app.models import Execution
        from app.services.execution_router import get_execution_router

        rows = (await db.execute(
            select(SessionModel).where(
                SessionModel.status.in_(("idle", "terminated")),
                SessionModel.runtime_metadata.has_key("kernel_id"),
            )
        )).scalars().all()
        released = 0
        router = get_execution_router()
        for s in rows:
            busy = (await db.execute(
                select(Execution.id).where(
                    Execution.session_id == s.id, Execution.status.in_(("pending", "running")),
                ).limit(1)
            )).first()
            if busy:
                continue
            kernel_id = (s.runtime_metadata or {}).get("kernel_id")
            try:
                await router.shutdown_kernel(s.user_id, kernel_id)
            except Exception as e:  # noqa: BLE001 — retried next sweep
                log.warning("session_kernel_shutdown_failed", session_id=str(s.id), error=str(e))
                continue
            s.runtime_metadata = {k: v for k, v in (s.runtime_metadata or {}).items() if k != "kernel_id"}
            released += 1
        if released:
            await db.commit()
        return released

    async def _sweep_expired_outputs(self, db, now: datetime) -> int:
        """Delete ExecutionOutput rows past their expiry — the column has
        existed since the initial migration but nothing ever read it against
        `now` until this. The parent Execution row (status, code, timings)
        is untouched: it's the durable audit record; only its bulky content
        chunks are reaped."""
        result = await db.execute(sa_delete(ExecutionOutput).where(ExecutionOutput.expires_at < now))
        await db.commit()
        return result.rowcount or 0

    async def _sweep_soft_deleted_files(self, db, now: datetime, settings: Settings) -> int:
        """A soft-deleted file's blob is kept in case of an accidental
        delete; past the retention window, purge the blob and then the
        metadata row. Blob first: if the delete fails, the row stays
        (still `is_deleted`, so invisible to normal listing) for the next
        sweep to retry rather than losing the only pointer to that blob."""
        cutoff = now - timedelta(hours=settings.file_soft_delete_retention_hours)
        rows = (await db.execute(
            select(WorkspaceFile, Workspace.owner_id)
            .join(Workspace, Workspace.id == WorkspaceFile.workspace_id)
            .where(WorkspaceFile.is_deleted.is_(True), WorkspaceFile.updated_at < cutoff)
        )).all()
        if not rows:
            return 0
        store = get_blob_store()
        org_id = settings.org_id
        purged = 0
        for file_row, owner_id in rows:
            key = storage_key_for_file(
                org_id=org_id, user_id=str(owner_id), workspace_id=str(file_row.workspace_id),
                branch=file_row.branch, path=file_row.path,
            )
            try:
                await store.delete(key)
            except Exception:
                log.warning("lifecycle_file_blob_delete_failed", file_id=str(file_row.id), key=key)
                continue
            await db.delete(file_row)
            purged += 1
        if purged:
            await db.commit()
        return purged

    async def _sweep_stale_codegen_turns(self, db, now: datetime, settings: Settings) -> int:
        """A turn stuck 'running' this long means the process that was
        driving its agent loop is gone (WS-LIFE-013 — previously nothing
        ever detected this; the conversation just stayed stuck forever with
        no reply and no error). Mark it failed and notify whichever
        workspace-session channel the turn was streaming to, using the exact
        codegen.failed envelope shape _run_turn itself publishes (WS-AI-011)
        so the client's existing reducer handles it identically to a live
        failure."""
        cutoff = now - timedelta(seconds=settings.codegen_turn_stale_seconds)
        rows = (await db.execute(
            select(CodegenMessage).where(
                CodegenMessage.status == "running",
                CodegenMessage.created_at < cutoff,
            )
        )).scalars().all()
        if not rows:
            return 0
        error_message = "Generation was interrupted by a service restart and could not be resumed."
        for m in rows:
            m.status = "failed"
            m.content = [{"type": "text", "text": error_message}]
        await db.commit()

        r = get_redis()
        for m in rows:
            if not m.ws_session_id:
                continue
            envelope = {
                "v": 1,
                "ts": now.isoformat(),
                "type": "codegen.failed",
                "session_id": str(m.session_id),
                "error_message": error_message,
            }
            await r.publish(session_channel_for(str(m.ws_session_id)), json.dumps(envelope))
        return len(rows)


_reconciler: LifecycleReconciler | None = None


def get_lifecycle_reconciler() -> LifecycleReconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = LifecycleReconciler()
    return _reconciler

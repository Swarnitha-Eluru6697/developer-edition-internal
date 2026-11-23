"""
Connection discovery (GAP-10) — durably summarized in DiscoveryRun, with
the actual discovered objects (potentially tens of thousands, for a large
source) held as individual DiscoveryObject rows rather than one JSON blob.

Still executed via the same Operation claim/lease worker as every other
long-running action (app/services/operation_runner.py) — see
app/api/routes/connections.py's `_discover_factory`, which now reconstructs
its work from a DiscoveryRun id (resource_type="discovery_run") instead of
a bare connection id, and calls `run_discovery` below rather than returning
a result for Operation.result to hold directly.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import DiscoveryObjectRepository, DiscoveryRunRepository
from app.repositories.discovery_cursor import InvalidCursorError
from app.services.connection_discovery import discover
from app.services.safe_error import to_safe_error
from app.services.secret_store import hydrate_connection_secrets
from app.services.secrets import sanitize_probe_message

logger = structlog.get_logger(__name__)

# Keeps a single INSERT's parameter count well under Postgres's ~65535
# limit (4 columns/row here) even for a source with 100k+ discovered
# objects, without needing one round trip per row either.
_BULK_INSERT_BATCH_SIZE = 2000


def _batches(org_id: str, run_id: str, objects: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Row dicts built one insert-batch at a time, so a large run never holds a second full copy of itself."""
    for start in range(0, len(objects), _BULK_INSERT_BATCH_SIZE):
        yield [
            {"org_id": org_id, "discovery_run_id": run_id, **obj}
            for obj in objects[start:start + _BULK_INSERT_BATCH_SIZE]
        ]


def _run_to_dict(row) -> dict[str, Any]:
    return {
        "id": row.id, "org_id": row.org_id, "connection_id": row.connection_id,
        "operation_id": row.operation_id, "status": row.status, "object_count": row.object_count,
        "continuation": row.continuation, "error_message": row.error_message,
        "truncated": bool(getattr(row, "truncated", False)), "created_at": row.created_at, "completed_at": row.completed_at,
    }


def _object_to_dict(row) -> dict[str, Any]:
    return {"id": row.id, "object_type": row.object_type, "schema_name": row.schema_name, "name": row.name}


class DiscoveryService:
    async def create_run(self, org_id: str, connection_id: str) -> dict[str, Any]:
        async with AsyncSessionLocal() as session:
            row = await DiscoveryRunRepository(session).create({"org_id": org_id, "connection_id": connection_id})
            await session.commit()
            return _run_to_dict(row)

    async def set_operation_id(self, org_id: str, run_id: str, operation_id: str) -> None:
        async with AsyncSessionLocal() as session:
            repo = DiscoveryRunRepository(session)
            row = await repo.get_for_org(org_id, run_id)
            if row is not None:
                await repo.update(row, {"operation_id": operation_id})
                await session.commit()

    async def get_run(self, org_id: str, run_id: str) -> dict[str, Any] | None:
        async with AsyncSessionLocal() as session:
            row = await DiscoveryRunRepository(session).get_for_org(org_id, run_id)
            return _run_to_dict(row) if row else None

    async def get_run_or_404(self, org_id: str, run_id: str) -> dict[str, Any]:
        run = await self.get_run(org_id, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Discovery run '{run_id}' not found.")
        return run

    async def list_objects(
        self,
        org_id: str,
        run_id: str,
        *,
        search: str | None,
        limit: int,
        cursor: str | None,
        object_type: str | None = None,
        schema_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        await self.get_run_or_404(org_id, run_id)  # 404 rather than a silently-empty page for a bad/foreign id
        async with AsyncSessionLocal() as session:
            try:
                rows, next_cursor = await DiscoveryObjectRepository(session).list_page(
                    discovery_run_id=run_id, org_id=org_id, search=search, limit=limit, cursor=cursor,
                    object_type=object_type, schema_name=schema_name,
                )
            except InvalidCursorError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            return [_object_to_dict(r) for r in rows], next_cursor

    async def get_facets(self, org_id: str, run_id: str) -> dict[str, Any]:
        """FE-BUG-12: distinct schema names/object types for a run, so the UI
        can render Schema/Type filter dropdowns without paging through every
        discovered object first."""
        await self.get_run_or_404(org_id, run_id)
        async with AsyncSessionLocal() as session:
            schema_names, object_types = await DiscoveryObjectRepository(session).list_facets(
                discovery_run_id=run_id, org_id=org_id,
            )
            return {"schema_names": schema_names, "object_types": object_types}

    async def _set_run_fields(self, org_id: str, run_id: str, fields: dict[str, Any]) -> None:
        async with AsyncSessionLocal() as session:
            repo = DiscoveryRunRepository(session)
            row = await repo.get_for_org(org_id, run_id)
            if row is not None:
                await repo.update(row, fields)
                await session.commit()

    async def run_discovery(self, org_id: str, run_id: str, connection: dict[str, Any]) -> None:
        """The actual discovery work — introspects the live source, then
        persists what it found as DiscoveryObject rows and summarizes the
        outcome on the DiscoveryRun. Re-raises on a genuine, unexpected
        failure (e.g. a DB error while persisting) so the Operation driving
        this also ends up "failed", not just this run — connection_discovery.
        discover() itself never raises (see its own module docstring): an
        unreachable source or unsupported source_type comes back as a
        normal, successful result with connected=False/None and a `message`
        explaining why, recorded here as this run's error_message without
        treating the run itself as failed."""
        await self._set_run_fields(org_id, run_id, {"status": "running"})
        try:
            hydrated = await hydrate_connection_secrets(connection)
            result = await discover(hydrated, org_id=org_id)

            objects: list[dict[str, Any]] = [
                {"object_type": "schema", "schema_name": None, "name": name}
                for name in (result.get("schemas") or [])
            ]
            objects += [
                {"object_type": "table", "schema_name": table.get("schema"), "name": table.get("name")}
                for table in (result.get("tables") or [])
                if table.get("name")
            ]
            if not objects:
                # File/API/fallback sources: no schemas/tables, just a flat
                # `objects` list of {"name", "type"}.
                objects = [
                    {"object_type": obj.get("type") or "object", "schema_name": None, "name": obj.get("name")}
                    for obj in (result.get("objects") or [])
                    if obj.get("name")
                ]

            # GAP-10: bounded. Past the cap the run keeps the first objects (in discovery order) and says so.
            cap = settings.discovery_max_objects
            truncated = len(objects) > cap
            if truncated:
                logger.warning(
                    "discovery.truncated", org_id=org_id, discovery_run_id=run_id, discovered=len(objects), kept=cap,
                )
                objects = objects[:cap]
            object_count = len(objects)

            async with AsyncSessionLocal() as session:
                repo = DiscoveryObjectRepository(session)
                # Replace, never append: the Operation driving this can be re-claimed and re-executed (a worker died,
                # its lease lapsed), and a second attempt must not leave the first one's rows behind as duplicates.
                await repo.delete_for_runs([run_id])
                for batch in _batches(org_id, run_id, objects):
                    await repo.bulk_create(batch)
                await session.commit()

            error_message = None if result.get("connected") else sanitize_probe_message(result.get("message"))
            await self._set_run_fields(org_id, run_id, {
                "status": "succeeded", "object_count": object_count, "truncated": truncated,
                "error_message": error_message, "completed_at": datetime.now(UTC),
            })
            logger.info(
                "discovery.completed", org_id=org_id, discovery_run_id=run_id,
                object_count=object_count, truncated=truncated, connected=result.get("connected"),
            )
        except Exception as exc:
            # BUG-11: the run's error is the SafeError's fixed message, code and log reference — the exception's own
            # text (a DB error on the persisting step, a driver message) is only ever in the redacted log.
            safe = to_safe_error(exc, event="discovery.failed", org_id=org_id, discovery_run_id=run_id)
            await self._set_run_fields(org_id, run_id, {
                "status": "failed", "error_message": safe.describe(), "completed_at": datetime.now(UTC),
            })
            raise


discovery_service = DiscoveryService()


# ── Retention (GAP-10) ────────────────────────────────────────────────────────────────────────────────────────────
# Every Discover click creates a run and up to `discovery_max_objects` rows; without pruning that only ever grows.

_PRUNE_BATCH = 50


async def prune_old_runs() -> int:
    """Delete finished runs (and their objects) older than settings.discovery_retention_days, always keeping each
    connection's most recent successful run and anything still queued or running. Returns the runs removed."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.discovery_retention_days)
    async with AsyncSessionLocal() as session:
        run_ids = await DiscoveryRunRepository(session).list_prunable_ids(cutoff)
    removed = 0
    for start in range(0, len(run_ids), _PRUNE_BATCH):
        batch = run_ids[start:start + _PRUNE_BATCH]
        async with AsyncSessionLocal() as session:  # objects first, one short transaction per batch
            await DiscoveryObjectRepository(session).delete_for_runs(batch)
            await DiscoveryRunRepository(session).delete_by_ids(batch)
            await session.commit()
        removed += len(batch)
    return removed


_cleanup_task: asyncio.Task | None = None


async def _cleanup_loop() -> None:
    while True:
        try:
            removed = await prune_old_runs()
            if removed:
                logger.info("discovery.old_runs_pruned", count=removed)
        except Exception:  # noqa: BLE001 - one bad sweep must not end the periodic loop
            logger.exception("discovery.prune_failed")
        await asyncio.sleep(settings.discovery_cleanup_interval_seconds)


async def start_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is None:
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def stop_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None

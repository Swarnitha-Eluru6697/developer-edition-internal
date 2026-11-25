"""
Shared pytest fixtures.

`fake_postgres` is a minimal in-memory stand-in for AsyncSessionLocal + the
Postgres repositories, for tests that exercise real service-layer logic
(reference validation, template pinning, update semantics, lifecycle
transitions) across the now fully Postgres-authoritative connection/
transform/quality-policy/schema-version/pipeline/connector-template/
topic-policy services, without a live Postgres connection — which isn't
available in this environment (see this session's various migration notes).
It mirrors the old app.storage.memory.store's own `create(collection, values)
-> dict` / `get(collection, id) -> dict | None` shape on purpose, so a test
that used to seed via `store.create("pipelines", ...)` can switch to
`fake_postgres.create("pipelines", ...)` with no other changes.

connector_templates has no org_id (it's a global catalog, not org-scoped —
see app/models/catalog.py), so `_FakeRepository.list_for_org`/`get_for_org`
aren't used for it; catalog_service.py calls the plain `list()`/`get()`
methods instead.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

# GAP-06: the test configuration is set HERE, before anything imports `app`. `app.core.config.settings` reads the environment once, when
# it is first imported, so defaults set after an app import never reach it — and a test run would then depend on whatever .env the
# developer happens to have (absent on a fresh clone and in CI). Keep every `os.environ.setdefault` above the first `app` import.
#
# Gate 1 (app/core/gateway.py) fails closed by default — these must be set
# before any request reaches a route, so this runs at conftest import time,
# before pytest collects any test module.
os.environ.setdefault("TRUST_ENVOY_HEADERS", "true")
os.environ.setdefault("ENVOY_INTERNAL_SECRET", "test-gateway-secret")
# GAP-01: a fixed throwaway key so credential-encryption code paths run under test (32 bytes, base64).
os.environ.setdefault("DATACONNECT_SECRET_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
# Alerts: no background sweep in tests (a loop against the test database corrupts unrelated tests); tests sweep explicitly.
os.environ.setdefault("DATACONNECT_ALERTS_SWEEP_ENABLED", "false")

from app.repositories.discovery_cursor import decode_cursor, encode_cursor, sort_key  # noqa: E402


def _all_dataconnect_permissions() -> list[str]:
    with open(os.path.join(os.path.dirname(__file__), "..", "permissions.json"), encoding="utf-8") as f:
        return [p["id"] for p in json.load(f)["permissions"]]


ALL_PERMISSIONS: list[str] = _all_dataconnect_permissions()


def gateway_headers(
    permissions: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    username: str = "test-harness",
    tenant_id: str = "test-org",
) -> dict[str, str]:
    """Build the Envoy-injected headers Gate 1 (app/core/gateway.py) expects,
    for tests that call the app's routes directly through FastAPI's
    TestClient instead of through a real Envoy. `permissions` is the
    x-user-permissions list (base64 JSON, as Envoy renders a list claim);
    None means every DataConnect permission in permissions.json. See
    tests/personas.py for the seeded role personas used by RBAC tests.
    """
    perms = ALL_PERMISSIONS if permissions is None else sorted(permissions)
    return {
        "X-User-Name": username,
        "X-User-Id": f"id-{username}",
        "X-User-Permissions": base64.b64encode(json.dumps(list(perms)).encode()).decode(),
        "X-Tenant-Id": tenant_id,
        "X-Inaiera-Gateway-Secret": os.environ["ENVOY_INTERNAL_SECRET"],
    }


# The default identity for tests that don't care about permission granularity —
# they're testing business logic, not Gate 1 itself, so they just need any
# request that gets past the gate. Pass as TestClient(app, headers=...): a
# per-call `headers={...}` merges on top of these rather than replacing them
# (a per-call {"X-Tenant-Id": ...} picks the tenant).
DEFAULT_GATEWAY_HEADERS = gateway_headers()


class _FakeRow:
    """A generic object whose attributes are whatever dict you construct it
    from — stands in for an ORM row without needing a real mapped class.
    Any attribute not explicitly set reads as None (mirroring a nullable
    column that was never populated), so a test fixture that only sets the
    fields it cares about doesn't blow up when a service's row-to-dict
    helper reads every column."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Any:
        return None


class _FakeRepository:
    def __init__(self, table: dict[str, _FakeRow]) -> None:
        self._table = table

    async def list(self, statement: Any = None) -> list[_FakeRow]:
        # `statement` is accepted (matching SqlAlchemyRepository.list's real
        # arity, e.g. ConnectorTemplateRepository.list_active/
        # resume_orphaned_jobs's own WHERE-filtered call) but not honored —
        # a test relying on that filtering must pre-arrange the fake table
        # to already only contain matching rows.
        return list(self._table.values())

    async def list_for_org(self, org_id: str) -> list[_FakeRow]:
        return [row for row in self._table.values() if getattr(row, "org_id", None) == org_id]

    async def get_for_org(self, org_id: str, item_id: str) -> _FakeRow | None:
        row = self._table.get(item_id)
        if row is None or getattr(row, "org_id", None) != org_id:
            return None
        return row

    async def get(self, item_id: str) -> _FakeRow | None:
        return self._table.get(item_id)

    async def create(self, values: dict[str, Any]) -> _FakeRow:
        values = dict(values)
        values.setdefault("id", str(uuid4()))
        row = _FakeRow(values)
        self._table[row.id] = row
        return row

    async def update(self, row: _FakeRow, values: dict[str, Any]) -> _FakeRow:
        for key, value in values.items():
            setattr(row, key, value)
        return row

    async def delete(self, row: _FakeRow) -> None:
        self._table.pop(row.id, None)


class _FakeSecretStore:
    """Fakes app.services.secret_store.secret_store (GAP-01's fix) the same
    way _FakeRepository fakes a Postgres-backed repository — there's no live
    Postgres here either, and this table backs connection_service.py's
    create/update/delete just as much as ConnectionProfileRepository does."""


    def __init__(self) -> None:
        self._by_connection: dict[str, dict[str, Any]] = {}

    async def put(self, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        self._by_connection[connection_id] = dict(fields)
        return f"secret://dataconnect/connections/{connection_id}"

    async def put_in_session(self, session: Any, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        return await self.put(org_id=org_id, connection_id=connection_id, fields=fields)

    async def get(self, secret_ref: str | None) -> dict[str, Any] | None:
        if not secret_ref or not secret_ref.startswith("secret://dataconnect/connections/"):
            return None
        return self._by_connection.get(secret_ref.rsplit("/", 1)[-1])

    async def delete(self, secret_ref: str | None) -> None:
        if not secret_ref:
            return
        self._by_connection.pop(secret_ref.rsplit("/", 1)[-1], None)

    async def delete_in_session(self, session: Any, secret_ref: str | None) -> None:
        await self.delete(secret_ref)


class _FakeTopicPolicyRepository(_FakeRepository):
    async def find_domain_default(self, org_id: str, domain: str) -> _FakeRow | None:
        for row in self._table.values():
            if (
                getattr(row, "org_id", None) == org_id
                and getattr(row, "domain", None) == domain
                and getattr(row, "is_domain_default", False)
            ):
                return row
        return None


class _FakeSchemaVersionRepository(_FakeRepository):
    async def find_contract(
        self, org_id: str, domain: str, source_system: str, entity: str, version: str
    ) -> _FakeRow | None:
        for row in self._table.values():
            if (
                getattr(row, "org_id", None) == org_id
                and getattr(row, "domain", None) == domain
                and getattr(row, "source_system", None) == source_system
                and getattr(row, "entity", None) == entity
                and getattr(row, "version", None) == version
            ):
                return row
        return None


class _FakePipelineRepository(_FakeRepository):
    """FE-BUG-25: mirrors PipelineRepository.claim_transition's conditional
    UPDATE — a real Postgres-serialized compare-and-swap isn't something
    this in-memory single-threaded fake can (or needs to) model; it only
    needs to enforce the same "current status must be in allowed_sources"
    business rule the real one does, which unit tests actually exercise."""

    async def claim_transition(
        self, org_id: str, pipeline_id: str, allowed_sources: set[str], new_status: str, operation_id: str,
    ) -> _FakeRow | None:
        row = self._table.get(pipeline_id)
        if row is None or getattr(row, "org_id", None) != org_id:
            return None
        if getattr(row, "status", None) not in allowed_sources:
            return None
        row.status = new_status
        row.active_operation_id = operation_id
        return row

    async def ids_for_transform_policy(self, org_id: str, transform_policy_id: str) -> list[str]:
        """Mirrors PipelineRepository.ids_for_transform_policy (GAP-09)."""
        return [
            row.id for row in self._table.values()
            if getattr(row, "org_id", None) == org_id and getattr(row, "transform_policy_id", None) == transform_policy_id
        ]

    async def find_by_topic_name(self, org_id: str, topic_name: str) -> _FakeRow | None:
        """Mirrors PipelineRepository.find_by_topic_name: the pipeline in this
        org that PRODUCES topic_name (connect_config.py uses it to learn what
        format a sink's upstream topic was written in)."""
        for row in self._table.values():
            if getattr(row, "org_id", None) == org_id and getattr(row, "topic_name", None) == topic_name:
                return row
        return None


class _FakeFailureCountRepository(_FakeRepository):
    """Mirrors FailureCountRepository (app/repositories/dataconnect.py): add_counts
    is an upsert increment on (pipeline_id, kind, key, bucket_start), sum_since
    totals by key for one org and kind."""

    async def add_counts(self, rows: list[dict[str, Any]]) -> None:
        for values in rows:
            identity = (values["pipeline_id"], values["kind"], values["key"], values["bucket_start"])
            existing = next(
                (r for r in self._table.values()
                 if (r.pipeline_id, r.kind, r.key, r.bucket_start) == identity),
                None,
            )
            if existing is not None:
                existing.count += values["count"]
            else:
                row = dict(values)
                row.setdefault("id", str(uuid4()))
                self._table[row["id"]] = _FakeRow(row)

    async def sum_since(self, org_id: str, kind: str, since: Any, pipeline_id: str | None = None) -> dict[str, int]:
        totals: dict[str, int] = {}
        for r in self._table.values():
            if r.org_id == org_id and r.kind == kind and r.bucket_start >= since and (pipeline_id is None or r.pipeline_id == pipeline_id):
                totals[r.key] = totals.get(r.key, 0) + r.count
        return totals

    async def sum_since_by_pipeline(self, org_id: str, kind: str, key: str, since: Any) -> dict[str, int]:
        totals: dict[str, int] = {}
        for r in self._table.values():
            if r.org_id == org_id and r.kind == kind and r.key == key and r.bucket_start >= since:
                totals[r.pipeline_id] = totals.get(r.pipeline_id, 0) + r.count
        return totals

    async def delete_before(self, cutoff: Any) -> None:
        for row_id in [i for i, r in self._table.items() if r.bucket_start < cutoff]:
            del self._table[row_id]


class _FakeDiscoveryObjectRepository(_FakeRepository):
    """Mirrors DiscoveryObjectRepository's real SQL (app/repositories/
    dataconnect.py) — bulk_create just inserts rows, list_page does the
    same keyset-on-id + optional ILIKE-substring filtering the real query
    does, without needing Postgres for GAP-10's tests."""

    async def bulk_create(self, rows: list[dict[str, Any]]) -> None:
        for values in rows:
            values = dict(values)
            values.setdefault("id", str(uuid4()))
            self._table[values["id"]] = _FakeRow(values)

    async def list_page(
        self,
        *,
        discovery_run_id: str,
        org_id: str,
        search: str | None,
        limit: int,
        cursor: str | None,
        object_type: str | None = None,
        schema_name: str | None = None,
    ) -> tuple[list[_FakeRow], str | None]:
        matches = [
            row for row in self._table.values()
            if getattr(row, "discovery_run_id", None) == discovery_run_id
            and getattr(row, "org_id", None) == org_id
        ]
        if search:
            needle = search.lower()
            matches = [
                row for row in matches
                if needle in (getattr(row, "name", "") or "").lower()
                or needle in (getattr(row, "schema_name", "") or "").lower()
            ]
        if object_type:
            matches = [row for row in matches if getattr(row, "object_type", None) == object_type]
        if schema_name:
            matches = [row for row in matches if getattr(row, "schema_name", None) == schema_name]
        matches.sort(key=sort_key)
        if cursor:
            after = decode_cursor(cursor)
            matches = [row for row in matches if sort_key(row) > after]
        page = matches[:limit]
        next_cursor = encode_cursor(page[-1]) if len(matches) > limit else None
        return page, next_cursor

    async def delete_for_runs(self, run_ids: list[str]) -> None:
        for row_id in [r.id for r in self._table.values() if getattr(r, "discovery_run_id", None) in run_ids]:
            self._table.pop(row_id, None)

    async def list_facets(self, *, discovery_run_id: str, org_id: str) -> tuple[list[str], list[str]]:
        matches = [
            row for row in self._table.values()
            if getattr(row, "discovery_run_id", None) == discovery_run_id
            and getattr(row, "org_id", None) == org_id
        ]
        schema_names = sorted({row.schema_name for row in matches if getattr(row, "schema_name", None)})
        object_types = sorted({row.object_type for row in matches if getattr(row, "object_type", None)})
        return schema_names, object_types


class _FakeDiscoveryRunRepository(_FakeRepository):
    """Mirrors DiscoveryRunRepository's retention queries (GAP-10)."""

    async def list_prunable_ids(self, cutoff: Any) -> list[str]:
        newest_success: dict[str, _FakeRow] = {}
        for row in self._table.values():
            if row.status == "succeeded":
                held = newest_success.get(row.connection_id)
                if held is None or row.created_at > held.created_at:
                    newest_success[row.connection_id] = row
        keep = {r.id for r in newest_success.values()}
        return [
            row.id for row in self._table.values()
            if row.created_at < cutoff and row.status in ("succeeded", "failed") and row.id not in keep
        ]

    async def delete_by_ids(self, run_ids: list[str]) -> None:
        for run_id in run_ids:
            self._table.pop(run_id, None)


class _FakeConnectionVerificationRepository(_FakeRepository):
    """Mirrors ConnectionVerificationRepository (app/repositories/dataconnect.py): one row per connection,
    keyed by connection_id; `record` upserts it."""

    async def get_many(self, connection_ids: list[str]) -> dict[str, _FakeRow]:
        return {cid: self._table[cid] for cid in connection_ids if cid in self._table}

    async def record(
        self, *, connection_id: str, org_id: str, status: str, tested_at: Any, latency_ms: float | None,
        error_code: str | None, message: str | None, verified_revision: int,
    ) -> None:
        self._table[connection_id] = _FakeRow({
            "connection_id": connection_id, "org_id": org_id, "status": status, "tested_at": tested_at,
            "latency_ms": latency_ms, "error_code": error_code, "message": message,
            "verified_revision": verified_revision,
        })


class _FakeQualityExecutionJobRepository(_FakeRepository):
    """Mirrors QualityExecutionJobRepository.claim_next / renew_lease (app/repositories/
    dataconnect.py): claimable = queued, or running with a lapsed/NULL lease; claiming sets
    running + lease + attempt_count. Insertion order stands in for created_at ordering. A real
    FOR UPDATE SKIP LOCKED isn't modelled — the in-memory fake is single-threaded, so claim_next
    being synchronous already makes it atomic."""

    async def claim_next(self, now: Any, lease_until: Any) -> _FakeRow | None:
        for row in self._table.values():
            lapsed = row.lease_expires_at is None or row.lease_expires_at < now
            if row.status == "queued" or (row.status == "running" and lapsed):
                row.status = "running"
                row.started_at = row.started_at or now
                row.lease_expires_at = lease_until
                row.attempt_count = (row.attempt_count or 0) + 1
                return row
        return None

    async def renew_lease(self, job_id: str, lease_until: Any) -> bool:
        row = self._table.get(job_id)
        if row is None or row.status != "running":
            return False
        row.lease_expires_at = lease_until
        return True

    async def save_progress(self, job_id: str, progress: dict[str, Any]) -> bool:
        """Mirrors QualityExecutionJobRepository.save_progress (GAP-13)."""
        row = self._table.get(job_id)
        if row is None or row.status != "running":
            return False
        row.progress = progress
        return True

    async def requeue(self, org_id: str, job_id: str) -> _FakeRow | None:
        """Mirrors QualityExecutionJobRepository.requeue (GAP-13)."""
        row = self._table.get(job_id)
        if row is None or row.org_id != org_id or row.status not in ("failed", "stale"):
            return None
        row.status = "queued"
        row.attempt_count = 0
        row.lease_expires_at = None
        row.completed_at = None
        row.error_message = row.error_code = row.retryable = row.correlation_id = None
        return row


class _FakeQualityExecutionJobRecordsRepository(_FakeRepository):
    """Mirrors QualityExecutionJobRecordsRepository's real SQL (app/
    repositories/dataconnect.py) — GAP-13 (chunked)."""

    async def create(self, values: dict[str, Any]) -> _FakeRow:
        values = dict(values)
        values.setdefault("chunk_index", 0)
        values.setdefault("byte_size", 0)
        return await super().create(values)

    async def create_many(self, rows: list[dict[str, Any]]) -> None:
        for values in rows:
            await self.create(values)

    async def get_chunk(self, job_id: str, chunk_index: int) -> _FakeRow | None:
        for row in self._table.values():
            if getattr(row, "job_id", None) == job_id and (getattr(row, "chunk_index", None) or 0) == chunk_index:
                return row
        return None

    async def list_chunks(self, job_id: str) -> list[_FakeRow]:
        rows = [row for row in self._table.values() if getattr(row, "job_id", None) == job_id]
        return sorted(rows, key=lambda r: getattr(r, "chunk_index", None) or 0)

    async def count_chunks(self, job_id: str) -> int:
        return len(await self.list_chunks(job_id))

    async def extend_expiry(self, job_id: str, expires_at: Any) -> None:
        for row in await self.list_chunks(job_id):
            row.expires_at = expires_at

    async def delete_by_job_id(self, job_id: str) -> None:
        for row in await self.list_chunks(job_id):
            self._table.pop(row.id, None)

    async def count_chunks_from(self, job_id: str, start: int) -> int:
        return len([row for row in await self.list_chunks(job_id) if (getattr(row, "chunk_index", None) or 0) >= start])

    # The jobs table, so delete_expired can honour the real SQL's rule: never touch a queued/running job's input (BUG-06).
    jobs_table: dict[str, _FakeRow] = {}

    async def delete_expired(self, now: Any) -> int:
        active = {
            job_id for job_id, job in self.jobs_table.items() if getattr(job, "status", None) in ("queued", "running")
        }
        expired = [
            row for row in self._table.values()
            if getattr(row, "expires_at", None) and row.expires_at < now and getattr(row, "job_id", None) not in active
        ]
        for row in expired:
            self._table.pop(row.id, None)
        return len(expired)


class _FakeAssetRepository(_FakeRepository):
    """Mirrors AssetRepository's real SQL (app/repositories/dataconnect.py) —
    FE-BUG-20."""

    async def get_by_qualified_name(self, org_id: str, qualified_name: str) -> _FakeRow | None:
        for row in self._table.values():
            if getattr(row, "org_id", None) == org_id and getattr(row, "qualified_name", None) == qualified_name:
                return row
        return None

    async def get_by_ids(self, org_id: str, ids: list[str]) -> list[_FakeRow]:
        idset = set(ids)
        return [row for row in self._table.values() if getattr(row, "org_id", None) == org_id and row.id in idset]


class _FakeLineageEdgeRepository(_FakeRepository):
    """Mirrors LineageEdgeRepository's real SQL (app/repositories/dataconnect.py)
    — FE-BUG-20."""

    async def list_for_pipeline(self, org_id: str, pipeline_id: str) -> list[_FakeRow]:
        return [
            row for row in self._table.values()
            if getattr(row, "org_id", None) == org_id and getattr(row, "pipeline_id", None) == pipeline_id
        ]

    async def get_by_identity(
        self, org_id: str, from_asset_id: str, to_asset_id: str, edge_type: str, pipeline_id: str | None,
    ) -> _FakeRow | None:
        for row in self._table.values():
            if (
                getattr(row, "org_id", None) == org_id
                and getattr(row, "from_asset_id", None) == from_asset_id
                and getattr(row, "to_asset_id", None) == to_asset_id
                and getattr(row, "edge_type", None) == edge_type
                and getattr(row, "pipeline_id", None) == pipeline_id
            ):
                return row
        return None

    def _touching(self, org_id: str, asset_ids: set[str], direction: str) -> list[_FakeRow]:
        def matches(row: _FakeRow) -> bool:
            leaves = getattr(row, "from_asset_id", None) in asset_ids
            arrives = getattr(row, "to_asset_id", None) in asset_ids
            return leaves if direction == "downstream" else arrives if direction == "upstream" else (leaves or arrives)

        rows = [r for r in self._table.values() if getattr(r, "org_id", None) == org_id and matches(r)]
        return sorted(rows, key=lambda r: r.id)

    async def list_touching(
        self, org_id: str, asset_ids: set[str], *, direction: str = "both", limit: int | None = None, offset: int = 0,
    ) -> list[_FakeRow]:
        rows = self._touching(org_id, asset_ids, direction)[offset:]
        return rows if limit is None else rows[:limit]

    async def count_touching(self, org_id: str, asset_ids: set[str], *, direction: str = "both") -> int:
        return len(self._touching(org_id, asset_ids, direction))


class _FakePipelineRuntimeStateRepository(_FakeRepository):
    async def renew_lease(self, row_id: str, worker_id: str, lease_until: Any) -> bool:
        """Mirrors PipelineRuntimeStateRepository.renew_lease (GAP-08): ownership, not expiry, is the condition."""
        row = self._table.get(row_id)
        if row is None or getattr(row, "leased_by", None) != worker_id:
            return False
        row.lease_expires_at = lease_until
        return True

    async def fence(self, row_id: str, worker_id: str, *, lock: bool = False) -> tuple[int, bool] | None:
        """Mirrors PipelineRuntimeStateRepository.fence (GAP-08)."""
        row = self._table.get(row_id)
        if row is None:
            return None
        return row.desired_generation, getattr(row, "leased_by", None) == worker_id

    async def bump_generation(self, pipeline_id: str) -> bool:
        """Mirrors PipelineRuntimeStateRepository.bump_generation (GAP-08)."""
        for row in self._table.values():
            if getattr(row, "pipeline_id", None) == pipeline_id:
                row.desired_generation += 1
                return True
        return False

    async def bump_desired_if_current(
        self, row_id: str, worker_id: str, expected_generation: int, desired_state: str
    ) -> int | None:
        """Mirrors PipelineRuntimeStateRepository.bump_desired_if_current (GAP-08)."""
        row = self._table.get(row_id)
        if row is None or getattr(row, "leased_by", None) != worker_id or row.desired_generation != expected_generation:
            return None
        row.desired_state = desired_state
        row.desired_generation += 1
        return row.desired_generation

    async def get_by_pipeline_id(self, pipeline_id: str, *, for_update: bool = False) -> _FakeRow | None:
        """Mirrors PipelineRuntimeStateRepository.get_by_pipeline_id (FE-BUG-07:
        PipelineService.get_pipeline enriches its response with this)."""
        for row in self._table.values():
            if getattr(row, "pipeline_id", None) == pipeline_id:
                return row
        return None

    async def list_by_pipeline_ids(self, pipeline_ids: list[str]) -> dict[str, _FakeRow]:
        """Mirrors PipelineRuntimeStateRepository.list_by_pipeline_ids (FE-BUG-07:
        PipelineService.list_pipelines' batch lookup)."""
        return {
            row.pipeline_id: row for row in self._table.values()
            if getattr(row, "pipeline_id", None) in pipeline_ids
        }

    async def claim_batch(self, worker_id: str, now: Any, lease_until: Any) -> list[str]:
        """Mirrors PipelineRuntimeStateRepository.claim_batch's real SQL
        (app/repositories/dataconnect.py) — same 3-clause eligibility
        (unleased, already ours, or lease lapsed) — without needing a real
        Postgres to exercise `FOR UPDATE SKIP LOCKED`/atomicity; see GAP-08
        tests in tests/test_runtime_reconciler_lease.py for what this can
        and can't stand in for."""
        claimed = []
        for row in self._table.values():
            leased_by = getattr(row, "leased_by", None)
            lease_expires_at = getattr(row, "lease_expires_at", None)
            eligible = (
                leased_by is None
                or leased_by == worker_id
                or (lease_expires_at is not None and lease_expires_at < now)
            )
            if eligible:
                row.leased_by = worker_id
                row.lease_expires_at = lease_until
                claimed.append(row.id)
        return claimed


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def commit(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def scalar(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakePostgres:
    def __init__(self) -> None:
        self.connection_profiles: dict[str, _FakeRow] = {}
        self.connection_verifications: dict[str, _FakeRow] = {}
        self.transform_policies: dict[str, _FakeRow] = {}
        self.quality_policies: dict[str, _FakeRow] = {}
        self.schema_versions: dict[str, _FakeRow] = {}
        self.pipelines: dict[str, _FakeRow] = {}
        self.connector_templates: dict[str, _FakeRow] = {}
        self.topic_policies: dict[str, _FakeRow] = {}
        self.pipeline_runtime_states: dict[str, _FakeRow] = {}
        self.discovery_runs: dict[str, _FakeRow] = {}
        self.discovery_objects: dict[str, _FakeRow] = {}
        self.operations: dict[str, _FakeRow] = {}
        self.quality_execution_jobs: dict[str, _FakeRow] = {}
        self.quality_execution_job_records: dict[str, _FakeRow] = {}
        self.quality_stream_stats: dict[str, _FakeRow] = {}
        self.failure_counts: dict[str, _FakeRow] = {}
        self.assets: dict[str, _FakeRow] = {}
        self.lineage_edges: dict[str, _FakeRow] = {}
        self.secret_store = _FakeSecretStore()

    def _table_for(self, collection: str) -> dict[str, _FakeRow]:
        return getattr(self, collection)

    def create(self, collection: str, values: dict[str, Any]) -> dict[str, Any]:
        table = self._table_for(collection)
        values = dict(values)
        values.setdefault("id", str(uuid4()))
        if collection == "connector_templates":
            # The real column is NOT NULL (every template names its worker pool); a fixture that doesn't care which gets
            # the natural one for its type.
            values.setdefault("runtime_pool_type", "connect-sink-pool" if values.get("connector_type") == "sink" else "connect-jdbc-pool")
        if collection == "connection_profiles":
            # GAP-02: verification moved to its own table. Tests still describe a connection in one dict
            # ("verification_status": "ok", ...); split those keys out into the verification record the
            # service now reads, so a fixture says what it means without caring where it is stored.
            verification_status = values.pop("verification_status", None)
            values.pop("status", None)
            tested_at = values.pop("last_tested_at", None)
            message = values.pop("last_test_message", None)
            values.setdefault("config_revision", 1)
            if verification_status in ("ok", "error"):
                self.connection_verifications[values["id"]] = _FakeRow({
                    "connection_id": values["id"], "org_id": values.get("org_id"), "status": verification_status,
                    "tested_at": tested_at or datetime.now(UTC), "message": message, "latency_ms": None,
                    "error_code": None if verification_status == "ok" else "PROBE_FAILED",
                    "verified_revision": values["config_revision"],
                })
        table[values["id"]] = _FakeRow(values)
        return dict(values)

    def get(self, collection: str, item_id: str) -> dict[str, Any] | None:
        row = self._table_for(collection).get(item_id)
        return dict(row.__dict__) if row else None

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.services.catalog_service as catalog_service_module
        import app.services.connect_config as connect_config_module
        import app.services.connection_service as connection_service_module
        import app.services.discovery_service as discovery_service_module
        import app.services.failure_counters as failure_counters_module
        import app.services.lineage_service as lineage_service_module
        import app.services.operation_runner as operation_runner_module
        import app.services.pipeline_service as pipeline_service_module
        import app.services.quality_job_runner as quality_job_runner_module
        import app.services.quality_service as quality_service_module
        import app.services.runtime_reconciler as runtime_reconciler_module
        import app.services.schema_service as schema_service_module
        import app.services.secret_store as secret_store_module
        import app.services.topic_policy as topic_policy_module
        import app.services.transform_service as transform_service_module

        # Two separate bindings to patch, not one: connection_service.py
        # calls secret_store.put/get/delete through its own `from ... import
        # secret_store` name, while hydrate_connection_secrets (called from
        # pipeline_service.py and connect_config.py too) resolves `secret_store`
        # from secret_store.py's own module globals at call time — patching
        # only one would leave the other still hitting a real DB.
        monkeypatch.setattr(secret_store_module, "secret_store", self.secret_store)
        monkeypatch.setattr(connection_service_module, "secret_store", self.secret_store)

        self._patch_module(monkeypatch, connection_service_module, "ConnectionProfileRepository", self.connection_profiles)
        self._patch_module(
            monkeypatch, connection_service_module, "ConnectionVerificationRepository", self.connection_verifications,
            repo_class=_FakeConnectionVerificationRepository,
        )
        self._patch_module(monkeypatch, transform_service_module, "TransformPolicyRepository", self.transform_policies)
        self._patch_module(
            monkeypatch, transform_service_module, "PipelineRepository", self.pipelines,
            repo_class=_FakePipelineRepository,
        )
        self._patch_module(monkeypatch, quality_service_module, "QualityPolicyRepository", self.quality_policies)
        self._patch_module(
            monkeypatch, schema_service_module, "SchemaVersionRepository", self.schema_versions,
            repo_class=_FakeSchemaVersionRepository,
        )
        self._patch_module(
            monkeypatch, pipeline_service_module, "PipelineRepository", self.pipelines,
            repo_class=_FakePipelineRepository,
        )
        # FE-BUG-07: PipelineService.list_pipelines/get_pipeline now enrich
        # their response with desired/observed runtime state — same
        # underlying table and fake class runtime_reconciler_module patches
        # below, just a second binding (pipeline_service.py imports the real
        # class into its own module namespace too).
        # build_connector looks up which pipeline produces a sink's input topic
        # (to pick the right converter) — without this it opens a real DB session.
        self._patch_module(
            monkeypatch, connect_config_module, "PipelineRepository", self.pipelines,
            repo_class=_FakePipelineRepository,
        )
        self._patch_module(
            monkeypatch, pipeline_service_module, "PipelineRuntimeStateRepository", self.pipeline_runtime_states,
            repo_class=_FakePipelineRuntimeStateRepository,
        )
        self._patch_module(monkeypatch, catalog_service_module, "ConnectorTemplateRepository", self.connector_templates)
        self._patch_module(
            monkeypatch, topic_policy_module, "TopicPolicyRepository", self.topic_policies,
            repo_class=_FakeTopicPolicyRepository,
        )
        self._patch_module(
            monkeypatch, runtime_reconciler_module, "PipelineRuntimeStateRepository", self.pipeline_runtime_states,
            repo_class=_FakePipelineRuntimeStateRepository,
        )
        self._patch_module(
            monkeypatch, discovery_service_module, "DiscoveryRunRepository", self.discovery_runs,
            repo_class=_FakeDiscoveryRunRepository,
        )
        self._patch_module(
            monkeypatch, discovery_service_module, "DiscoveryObjectRepository", self.discovery_objects,
            repo_class=_FakeDiscoveryObjectRepository,
        )
        self._patch_module(monkeypatch, operation_runner_module, "OperationRepository", self.operations)
        self._patch_module(
            monkeypatch, quality_job_runner_module, "QualityExecutionJobRepository", self.quality_execution_jobs,
            repo_class=_FakeQualityExecutionJobRepository,
        )
        self._patch_module(
            monkeypatch, quality_job_runner_module, "QualityExecutionJobRecordsRepository",
            self.quality_execution_job_records, repo_class=_FakeQualityExecutionJobRecordsRepository,
        )
        self._patch_module(
            monkeypatch, quality_service_module, "QualityExecutionJobRepository", self.quality_execution_jobs,
            repo_class=_FakeQualityExecutionJobRepository,
        )
        self._patch_module(
            monkeypatch, quality_service_module, "QualityExecutionJobRecordsRepository",
            self.quality_execution_job_records, repo_class=_FakeQualityExecutionJobRecordsRepository,
        )
        monkeypatch.setattr(_FakeQualityExecutionJobRecordsRepository, "jobs_table", self.quality_execution_jobs)
        # FE-BUG-17: MonitoringService._validation_failure_count reads
        # through quality_service.list_stream_stats, the first fake_postgres
        # caller of QualityStreamStatsRepository — every prior test exercised
        # it via per-test monkeypatch.setattr on individual methods instead
        # (see tests/test_quality_stream_stats.py), never through this table.
        self._patch_module(monkeypatch, quality_service_module, "QualityStreamStatsRepository", self.quality_stream_stats)
        self._patch_module(
            monkeypatch, failure_counters_module, "FailureCountRepository", self.failure_counts,
            repo_class=_FakeFailureCountRepository,
        )
        # FE-BUG-20: bounded lineage traversal (LineageService.graph_for_asset)
        # is the first lineage read path to need real repository behavior
        # (get_by_ids/list_touching) under fake_postgres — test_lineage_service.py
        # previously only unit-tested pure dict helpers + route wiring.
        self._patch_module(
            monkeypatch, lineage_service_module, "AssetRepository", self.assets, repo_class=_FakeAssetRepository,
        )
        self._patch_module(
            monkeypatch, lineage_service_module, "LineageEdgeRepository", self.lineage_edges,
            repo_class=_FakeLineageEdgeRepository,
        )

    def _patch_module(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: Any,
        repo_attr_name: str,
        table: dict[str, _FakeRow],
        repo_class: type[_FakeRepository] = _FakeRepository,
    ) -> None:
        def repo_factory(session: Any) -> _FakeRepository:
            return repo_class(table)

        monkeypatch.setattr(module, repo_attr_name, repo_factory)
        monkeypatch.setattr(module, "AsyncSessionLocal", lambda: _FakeSession())


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch) -> FakePostgres:
    fp = FakePostgres()
    fp.patch(monkeypatch)
    return fp


@pytest.fixture(autouse=True)
def _no_live_kafka_connect_for_connector_availability(monkeypatch: pytest.MonkeyPatch):
    """NEW-09: listing the connector catalog asks each Kafka Connect cluster which plugins it has. A test must never reach a REAL Connect (there may
    be one running on a developer's machine), so by default every cluster "could not be reached". A test that cares about availability sets its own."""
    from app.services import connector_availability

    async def _unreachable(pool_id, need_worker):
        return connector_availability.RuntimeSnapshot(None, None, error="NoLiveConnectInTests")

    connector_availability.reset_cache()
    monkeypatch.setattr(connector_availability, "_snapshot", _unreachable)


@pytest.fixture(autouse=True)
def _no_live_kafka_for_sink_backlogs(monkeypatch: pytest.MonkeyPatch):
    """A sink's Consumer lag is read from Kafka committed offsets. A test must never reach a REAL Kafka, so by default the backlog
    is unknown ({}: no readable group). A test that cares about lag sets its own."""
    from app.services import runtime_metrics

    async def _no_backlogs(org_id):
        return {}

    runtime_metrics._BACKLOG_CACHE.clear()
    monkeypatch.setattr(runtime_metrics, "sink_backlogs", _no_backlogs)


@pytest.fixture(autouse=True)
def _deploy_readiness_not_enforced_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /pipelines/{id}/deploy enforces the "Ready to deploy" checks in production
    (settings.deploy_requires_readiness, FE-BUG-24). Most tests drive deploy to exercise
    something else (idempotency, auditing, Kafka Connect calls) against minimal fake
    pipelines/connections that were never meant to pass a live readiness check, so they run
    with it off; tests/test_deploy_readiness_enforcement.py turns it back on."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "deploy_requires_readiness", False)


def patch_job_execution(monkeypatch, work=None, result=None) -> None:
    """GAP-13: a quality job now runs chunk by chunk (QualityService.process_job_chunk per chunk, then finalize_job_result),
    not one execute_policy call. Replaces both so a test can say what the job's work does: `work(**kwargs)` is awaited
    once per chunk (it may raise, sleep, or count calls; its return value is ignored) and each chunk contributes nothing
    unless `work` returns a {"failed", "total", "samples"} dict; `result` is what finalize_job_result returns."""
    from app.services.quality_service import quality_service

    async def _process_job_chunk(**kwargs):
        outcome = await work(**kwargs) if work is not None else None
        if isinstance(outcome, dict) and "failed" in outcome:
            return outcome
        return {"failed": 0, "total": len(kwargs.get("records") or []), "samples": []}

    async def _finalize_job_result(**kwargs):
        return result or {"id": "result-1", "failed_count": 0}

    monkeypatch.setattr(quality_service, "process_job_chunk", _process_job_chunk)
    monkeypatch.setattr(quality_service, "finalize_job_result", _finalize_job_result)


@pytest.fixture(autouse=True)
def _no_live_topic_feasibility_check(monkeypatch):
    """GAP-06: the topic-policy feasibility check asks a real Kafka cluster how many brokers it has. No test has one: the
    check is switched off here, and tests/test_topic_feasibility.py turns it back on with a fake cluster."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "topic_feasibility_check_enabled", False)


@pytest.fixture(autouse=True)
def _quality_worker_blocked_state_is_not_written_to_a_database(monkeypatch):
    """GAP-03: a stuck quality worker writes a heartbeat to its runtime-state row. Unit tests have no database, so by default that
    write is a no-op and the in-process registry starts empty; tests of the heartbeat itself replace these with recorders."""
    from app.services.quality_stream_worker import QualityStreamWorker
    from app.services.quality_worker_health import quality_worker_health

    async def _noop(self, *args, **kwargs):
        return None

    monkeypatch.setattr(QualityStreamWorker, "_write_blocked_state", _noop)
    monkeypatch.setattr(QualityStreamWorker, "_clear_blocked_state", _noop)
    quality_worker_health.reset()
    yield
    quality_worker_health.reset()

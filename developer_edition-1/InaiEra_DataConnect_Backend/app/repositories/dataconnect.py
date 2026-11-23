from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

import app.models as models
from app.core.config import settings
from app.repositories.base import SqlAlchemyRepository
from app.repositories.discovery_cursor import decode_cursor, encode_cursor


class OrgScopedRepository(SqlAlchemyRepository):
    # True for what the UI lists with a "Created at" column (connections, pipelines, schemas, quality policies, SMT chains): the newest comes
    # first, so something just created is at the top of its list and not buried at the bottom. Ties (rows created in one statement) keep a
    # stable order by id. Every other repository keeps the database's own order.
    newest_first: bool = False

    async def list_for_org(self, org_id: str) -> list:
        stmt = select(self.model).where(self.model.org_id == org_id)
        if self.newest_first:
            stmt = stmt.order_by(self.model.created_at.desc(), self.model.id)
        return await self.list(stmt)

    async def get_for_org(self, org_id: str, item_id: str):
        row = await self.get(item_id)
        if not row or row.org_id != org_id:
            return None
        return row


class ConnectorTemplateRepository(SqlAlchemyRepository[models.ConnectorTemplate]):
    model = models.ConnectorTemplate

    async def list_active(self) -> list[models.ConnectorTemplate]:
        return await self.list(select(self.model).where(self.model.is_active.is_(True)))


class ConnectorTemplateRevisionRepository(SqlAlchemyRepository[models.ConnectorTemplateRevision]):
    model = models.ConnectorTemplateRevision

    async def list_for_template(self, template_id: str) -> list[models.ConnectorTemplateRevision]:
        return await self.list(
            select(self.model)
            .where(self.model.template_id == template_id)
            .order_by(self.model.revision_number)
        )

    async def latest_revision_number(self, template_id: str) -> int:
        from sqlalchemy import func

        result = await self.session.scalar(
            select(func.max(self.model.revision_number)).where(self.model.template_id == template_id)
        )
        return result or 0


class ConnectionProfileRepository(OrgScopedRepository):
    model = models.ConnectionProfile
    newest_first = True


class ConnectionVerificationRepository(SqlAlchemyRepository):
    """GAP-02: the only writer of a connection's verification outcome (`record`). Nothing that edits a
    ConnectionProfile goes through here, and nothing here touches the profile's configuration."""

    model = models.ConnectionVerification

    async def get_many(self, connection_ids: list[str]) -> dict[str, models.ConnectionVerification]:
        if not connection_ids:
            return {}
        result = await self.session.execute(select(self.model).where(self.model.connection_id.in_(connection_ids)))
        return {row.connection_id: row for row in result.scalars().all()}

    async def record(
        self, *, connection_id: str, org_id: str, status: str, tested_at: Any, latency_ms: float | None,
        error_code: str | None, message: str | None, verified_revision: int,
    ) -> None:
        values = {
            "org_id": org_id, "status": status, "tested_at": tested_at, "latency_ms": latency_ms,
            "error_code": error_code, "message": message, "verified_revision": verified_revision,
        }
        stmt = insert(self.model).values(connection_id=connection_id, **values)
        await self.session.execute(stmt.on_conflict_do_update(index_elements=[self.model.connection_id], set_=values))


class TransformPolicyRepository(OrgScopedRepository):
    model = models.TransformPolicy
    newest_first = True


class TopicPolicyRepository(OrgScopedRepository):
    model = models.TopicPolicy

    async def find_domain_default(self, org_id: str, domain: str) -> models.TopicPolicy | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.domain == domain,
                self.model.is_domain_default.is_(True),
            )
        )
        return result.scalar_one_or_none()


class SchemaVersionRepository(OrgScopedRepository):
    model = models.SchemaVersion
    newest_first = True

    async def find_contract(
        self, org_id: str, domain: str, source_system: str, entity: str, version: str
    ) -> models.SchemaVersion | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.domain == domain,
                self.model.source_system == source_system,
                self.model.entity == entity,
                self.model.version == version,
            )
        )
        return result.scalar_one_or_none()


class SchemaInferenceRunRepository(OrgScopedRepository):
    model = models.SchemaInferenceRun


class SchemaDriftApprovalRepository(OrgScopedRepository):
    model = models.SchemaDriftApproval

    async def list_pending(self, org_id: str) -> list[models.SchemaDriftApproval]:
        return await self.list(
            select(self.model).where(self.model.org_id == org_id, self.model.status == "pending")
        )

    async def list_for_org_with_status(
        self, org_id: str, status: str | None
    ) -> list[models.SchemaDriftApproval]:
        if status is None:
            return await self.list_for_org(org_id)
        return await self.list(
            select(self.model).where(self.model.org_id == org_id, self.model.status == status)
        )


class QualityPolicyRepository(OrgScopedRepository):
    model = models.QualityPolicy
    newest_first = True


class PipelineRepository(OrgScopedRepository):
    model = models.Pipeline
    newest_first = True

    async def find_by_connector_name(self, org_id: str, connector_name: str) -> models.Pipeline | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.connector_name == connector_name,
            )
        )
        return result.scalar_one_or_none()

    async def ids_for_transform_policy(self, org_id: str, transform_policy_id: str) -> list[str]:
        """GAP-09: every pipeline in this org that applies the given transform policy — the ones whose generated
        connector config changes when the policy is edited."""
        result = await self.session.execute(
            select(self.model.id).where(
                self.model.org_id == org_id,
                self.model.transform_policy_id == transform_policy_id,
            )
        )
        return [row_id for (row_id,) in result.all()]

    async def find_by_topic_name(self, org_id: str, topic_name: str) -> models.Pipeline | None:
        """The pipeline that PRODUCES topic_name (its own topic_name column),
        if any — used by connect_config.py to look up what format a sink's
        upstream topic was actually written in, since Kafka Connect itself
        gives a sink no way to introspect that. None for a topic nothing in
        this org produced (e.g. hand-created outside the app, or from
        another org) — callers treat that as "assume the platform default"."""
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.topic_name == topic_name,
            )
        )
        return result.scalar_one_or_none()

    async def claim_transition(
        self, org_id: str, pipeline_id: str, allowed_sources: set[str], new_status: str, operation_id: str,
    ) -> models.Pipeline | None:
        """FE-BUG-25: atomic conditional UPDATE — sets status/active_operation_id
        in one statement, gated by `allowed_sources`, so two near-simultaneous
        callers can't both succeed (Postgres serializes concurrent UPDATEs to
        the same row; the second one's WHERE clause is evaluated against the
        first's already-committed status). None if zero rows matched — the
        pipeline doesn't exist, belongs to a different org, or its current
        status isn't in `allowed_sources`. See PipelineService.
        claim_lifecycle_transition, the only caller, for the full picture."""
        stmt = (
            update(self.model)
            .where(
                self.model.id == pipeline_id, self.model.org_id == org_id,
                self.model.status.in_(allowed_sources),
            )
            .values(status=new_status, active_operation_id=operation_id)
            .returning(self.model)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()


class PipelineRuntimeStateRepository(SqlAlchemyRepository):
    model = models.PipelineRuntimeState

    async def get_by_pipeline_id(
        self, pipeline_id: str, *, for_update: bool = False
    ) -> models.PipelineRuntimeState | None:
        """`for_update` takes a row lock held until the caller's transaction ends — GAP-08: record_desired reads,
        decides the next generation and writes back, so two simultaneous callers must queue rather than both read N
        and both write N+1."""
        stmt = select(self.model).where(self.model.pipeline_id == pipeline_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_quality_blocked(self, pipeline_id: str, *, since: Any, heartbeat_at: Any) -> None:
        """GAP-03: record that this pipeline's quality worker is stuck. `since` is kept if already set, so it is when the record
        FIRST failed; the heartbeat always moves. No row (a pipeline that was never deployed) is a no-op."""
        await self.session.execute(
            update(self.model)
            .where(self.model.pipeline_id == pipeline_id)
            .values(
                quality_blocked_since=func.coalesce(self.model.quality_blocked_since, since),
                quality_blocked_heartbeat_at=heartbeat_at,
            )
            .execution_options(synchronize_session=False)
        )

    async def clear_quality_blocked(self, pipeline_id: str) -> None:
        await self.session.execute(
            update(self.model)
            .where(self.model.pipeline_id == pipeline_id)
            .values(quality_blocked_since=None, quality_blocked_heartbeat_at=None)
            .execution_options(synchronize_session=False)
        )

    async def bump_generation(self, pipeline_id: str) -> bool:
        """GAP-08: a user edit that changes what the reconciler should apply, without changing desired_state — mark
        every reconciliation that planned against the old spec as stale. Done in the database (`gen = gen + 1`), never
        read-modify-write, so it composes with a concurrent record_desired. False if the pipeline has no runtime row
        yet (never deployed), which needs no bump."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.pipeline_id == pipeline_id)
            .values(desired_generation=self.model.desired_generation + 1)
            .returning(self.model.id)
            .execution_options(synchronize_session=False)
        )
        return result.first() is not None

    async def renew_lease(self, row_id: str, worker_id: str, lease_until: Any) -> bool:
        """GAP-08: push this worker's lease on one row forward. False means the row is no longer ours (another
        process claimed it after our lease lapsed) and must not be acted on. The condition is ownership, not expiry: a
        lapsed lease nobody else has taken is still ours to keep."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.id == row_id, self.model.leased_by == worker_id)
            .values(lease_expires_at=lease_until)
            .returning(self.model.id)
            .execution_options(synchronize_session=False)
        )
        return result.first() is not None

    async def fence(self, row_id: str, worker_id: str, *, lock: bool = False) -> tuple[int, bool] | None:
        """GAP-08: (live desired_generation, is this worker still the lease holder) for one row, or None if it no
        longer exists. `lock=True` holds the row until the caller commits, which is what makes "check we still own it
        and nothing newer was recorded" and "write the result" one atomic step — a competing claim skips a locked row,
        and a concurrent record_desired waits."""
        stmt = select(self.model.desired_generation, self.model.leased_by).where(self.model.id == row_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return int(row[0]), row[1] == worker_id

    async def bump_desired_if_current(
        self, row_id: str, worker_id: str, expected_generation: int, desired_state: str
    ) -> int | None:
        """GAP-08: the reconciler's own desired-state change (the error-threshold pause), applied only if nothing newer
        was recorded since it read the row and it still holds the lease. Returns the new generation, or None when
        superseded — an absolute write from a stale snapshot would otherwise erase a user's edit."""
        result = await self.session.execute(
            update(self.model)
            .where(
                self.model.id == row_id,
                self.model.leased_by == worker_id,
                self.model.desired_generation == expected_generation,
            )
            .values(desired_state=desired_state, desired_generation=self.model.desired_generation + 1)
            .returning(self.model.desired_generation)
            .execution_options(synchronize_session=False)
        )
        row = result.first()
        return int(row[0]) if row else None

    async def list_by_pipeline_ids(self, pipeline_ids: list[str]) -> dict[str, models.PipelineRuntimeState]:
        """FE-BUG-07: one query for a whole pipeline list's worth of runtime
        state (PipelineService.list_pipelines), keyed by pipeline_id —
        avoids an N+1 lookup per row."""
        if not pipeline_ids:
            return {}
        result = await self.session.execute(
            select(self.model).where(self.model.pipeline_id.in_(pipeline_ids))
        )
        return {row.pipeline_id: row for row in result.scalars().all()}

    async def claim_batch(self, worker_id: str, now: Any, lease_until: Any) -> list[str]:
        """GAP-08: atomically claim (or renew this worker's own already-held
        lease on) every row not currently leased by ANOTHER live worker, and
        return just their ids — see app/services/runtime_reconciler.py's
        module docstring for the full ownership/handoff story this backs.
        `FOR UPDATE SKIP LOCKED` inside the candidate subquery is what makes
        this safe when more than one process runs this concurrently: a row
        another worker's own claim transaction is mid-committing is simply
        skipped this pass, not raced for."""
        candidate = (
            select(self.model.id)
            .where(
                or_(
                    self.model.leased_by.is_(None),
                    self.model.leased_by == worker_id,
                    self.model.lease_expires_at < now,
                )
            )
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(self.model)
            .where(self.model.id.in_(candidate))
            .values(leased_by=worker_id, lease_expires_at=lease_until)
            .returning(self.model.id)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        return [row_id for (row_id,) in result.all()]


class AssetRepository(OrgScopedRepository):
    model = models.Asset

    async def get_by_qualified_name(self, org_id: str, qualified_name: str) -> models.Asset | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.qualified_name == qualified_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, org_id: str, ids: list[str]) -> list[models.Asset]:
        """Bulk fetch by id, org-scoped — FE-BUG-20: the bounded lineage
        traversal (LineageService.graph_for_asset) resolves exactly the
        node ids BFS actually discovered, never the whole org's asset table
        the way graph_for_pipeline used to (see its own docstring)."""
        if not ids:
            return []
        return await self.list(
            select(self.model).where(self.model.org_id == org_id, self.model.id.in_(ids))
        )


class LineageEdgeRepository(OrgScopedRepository):
    model = models.LineageEdge

    async def list_for_pipeline(self, org_id: str, pipeline_id: str) -> list[models.LineageEdge]:
        return await self.list(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.pipeline_id == pipeline_id,
            )
        )

    def _touching_clause(self, org_id: str, ids: list[str], direction: str):
        """Edges leaving `ids` (downstream), arriving at them (upstream), or either."""
        if direction == "downstream":
            endpoint = self.model.from_asset_id.in_(ids)
        elif direction == "upstream":
            endpoint = self.model.to_asset_id.in_(ids)
        else:
            endpoint = or_(self.model.from_asset_id.in_(ids), self.model.to_asset_id.in_(ids))
        return (self.model.org_id == org_id, endpoint)

    async def list_touching(
        self, org_id: str, asset_ids: set[str], *, direction: str = "both", limit: int | None = None, offset: int = 0,
    ) -> list[models.LineageEdge]:
        """One BFS hop's worth of edges (FE-BUG-20), not the org's entire edge
        table — filtered by `direction` in SQL, in a stable id order, and
        optionally windowed with limit/offset so a hub asset with thousands of
        edges never loads them all (LineageService.graph_for_asset passes
        limit+1 to learn whether there are more)."""
        if not asset_ids:
            return []
        stmt = select(self.model).where(*self._touching_clause(org_id, list(asset_ids), direction)).order_by(self.model.id)
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return await self.list(stmt)

    async def count_touching(self, org_id: str, asset_ids: set[str], *, direction: str = "both") -> int:
        if not asset_ids:
            return 0
        result = await self.session.execute(
            select(func.count()).select_from(self.model).where(*self._touching_clause(org_id, list(asset_ids), direction))
        )
        return int(result.scalar_one())

    async def get_by_identity(
        self,
        org_id: str,
        from_asset_id: str,
        to_asset_id: str,
        edge_type: str,
        pipeline_id: str | None,
    ) -> models.LineageEdge | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.org_id == org_id,
                self.model.from_asset_id == from_asset_id,
                self.model.to_asset_id == to_asset_id,
                self.model.edge_type == edge_type,
                self.model.pipeline_id == pipeline_id,
            )
        )
        return result.scalar_one_or_none()


class QualityResultRepository(OrgScopedRepository):
    model = models.QualityResult

    async def create_once_for_job(self, values: dict[str, Any], *, job_id: str) -> tuple[models.QualityResult, bool]:
        """The ONE result of execution job `job_id` (NEW-05): (row, created). The first caller inserts it; any later caller — a worker that
        reclaimed the job after a crash between committing the result and marking the job finished — gets the existing row back and
        created=False, so a job can never have two results. One atomic statement, safe under concurrent callers."""
        stmt = insert(self.model).values(**values, job_id=job_id).on_conflict_do_nothing(index_elements=[self.model.job_id]).returning(self.model.id)
        inserted = (await self.session.execute(stmt)).scalar_one_or_none()
        row_id = inserted
        if row_id is None:
            row_id = (await self.session.execute(select(self.model.id).where(self.model.job_id == job_id))).scalar_one()
        row = await self.session.get(self.model, row_id, populate_existing=True)
        return row, inserted is not None


class QualityExecutionJobRepository(OrgScopedRepository):
    model = models.QualityExecutionJob

    async def claim_next(self, now: Any, lease_until: Any) -> models.QualityExecutionJob | None:
        """Atomically claims ONE job that is `queued`, or `running` with a lapsed (or NULL) lease:
        sets status=running, started_at (first claim only), lease_expires_at and attempt_count + 1.
        FOR UPDATE SKIP LOCKED inside the subquery means several workers polling at once never
        race for, or double-claim, the same row — the same query shape as the Operation claim
        (app/services/operation_runner.py)."""
        candidate = (
            select(self.model.id)
            .where(
                or_(
                    self.model.status == "queued",
                    (self.model.status == "running")
                    & (self.model.lease_expires_at.is_(None) | (self.model.lease_expires_at < now)),
                )
            )
            .order_by(self.model.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(self.model)
            # `id = (scalar subquery)`, NOT `id IN (subquery)`: with IN, Postgres may re-run the
            # LIMIT 1 ... FOR UPDATE SKIP LOCKED subquery per row and update every claimable row in
            # one statement (verified: 6 queued rows -> 6 updated), while RETURNING hands back only
            # one — the rest would sit `running` and unexecuted until their lease lapsed. A scalar
            # subquery is evaluated exactly once.
            .where(self.model.id == candidate.scalar_subquery())
            .values(
                status="running",
                started_at=func.coalesce(self.model.started_at, now),
                lease_expires_at=lease_until,
                attempt_count=self.model.attempt_count + 1,
            )
            .returning(self.model)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def save_progress(self, job_id: str, progress: dict[str, Any]) -> bool:
        """GAP-13: the worker's checkpoint, committed after each chunk — only while the job is still `running`. False
        means the job is no longer this worker's (finished, or reclaimed by another), so it must stop."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.id == job_id, self.model.status == "running")
            .values(progress=progress)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    async def requeue(self, org_id: str, job_id: str) -> models.QualityExecutionJob | None:
        """GAP-13: put a `failed`/`stale` job back in the queue so a worker resumes it from its checkpoint (its staged
        chunks are kept for that). Conditional on the status so a job that is running or done is never disturbed."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.id == job_id, self.model.org_id == org_id, self.model.status.in_(("failed", "stale")))
            .values(
                status="queued", attempt_count=0, lease_expires_at=None, completed_at=None,
                error_message=None, error_code=None, retryable=None, correlation_id=None,
            )
            .returning(self.model)
            .execution_options(synchronize_session=False)
        )
        return result.scalars().first()

    async def renew_lease(self, job_id: str, lease_until: Any) -> bool:
        """Pushes the lease forward — only while the job is still `running`. False means the job
        is no longer this worker's to keep (finished, or reclaimed by another)."""
        result = await self.session.execute(
            update(self.model)
            .where(self.model.id == job_id, self.model.status == "running")
            .values(lease_expires_at=lease_until)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)


class QualityExecutionJobRecordsRepository(SqlAlchemyRepository):
    model = models.QualityExecutionJobRecords

    async def create_many(self, rows: list[dict[str, Any]]) -> None:
        """One INSERT for a batch of chunk rows (GAP-13)."""
        if rows:
            await self.session.execute(insert(self.model), rows)

    async def get_chunk(self, job_id: str, chunk_index: int) -> models.QualityExecutionJobRecords | None:
        result = await self.session.execute(
            select(self.model).where(self.model.job_id == job_id, self.model.chunk_index == chunk_index)
        )
        return result.scalar_one_or_none()

    async def list_chunks(self, job_id: str) -> list[models.QualityExecutionJobRecords]:
        result = await self.session.execute(
            select(self.model).where(self.model.job_id == job_id).order_by(self.model.chunk_index)
        )
        return list(result.scalars().all())

    async def count_chunks(self, job_id: str) -> int:
        result = await self.session.execute(select(func.count()).select_from(self.model).where(self.model.job_id == job_id))
        return int(result.scalar_one())

    async def extend_expiry(self, job_id: str, expires_at: Any) -> None:
        await self.session.execute(update(self.model).where(self.model.job_id == job_id).values(expires_at=expires_at))

    async def delete_by_job_id(self, job_id: str) -> None:
        await self.session.execute(delete(self.model).where(self.model.job_id == job_id))

    async def count_chunks_from(self, job_id: str, start: int) -> int:
        """How many staged chunks exist at index >= start — what a resumed job still needs is every index in [start, chunk_count)."""
        result = await self.session.execute(
            select(func.count()).select_from(self.model).where(self.model.job_id == job_id, self.model.chunk_index >= start)
        )
        return int(result.scalar_one())

    async def delete_expired(self, now: Any) -> int:
        """Retention sweep. BUG-06: never touches the input of a job that is still queued or running — those rows are needed
        however old they are (a job can wait in the queue, or run, longer than the TTL). Only chunks whose job has finished
        (succeeded / failed / stale) are eligible once past `expires_at`."""
        still_needed = select(models.QualityExecutionJob.id).where(models.QualityExecutionJob.status.in_(("queued", "running")))
        result = await self.session.execute(
            delete(self.model).where(self.model.expires_at < now, self.model.job_id.not_in(still_needed))
        )
        return int(result.rowcount or 0)


class QualityStreamOffsetRepository(SqlAlchemyRepository):
    """NEW-05: see models/quality.py's QualityStreamOffset. Runs on the caller's session and never commits, so claiming a record and recording
    its statistics are one transaction."""

    model = models.QualityStreamOffset

    async def claim(self, *, pipeline_id: str, topic: str, partition: int, offset: int) -> bool:
        """True if the record at (topic, partition, offset) has NOT been counted for this pipeline yet — and from this moment, in this
        transaction, it has. False if it has (a replay).

        One atomic statement: it creates the row, or moves it forward only when `offset` is at or beyond what is stored. Under a concurrent
        writer the row is locked, so the second one waits for the first to commit and then sees its offset: a record is claimed by exactly one
        committed transaction, whoever reads it and however often it is redelivered."""
        stmt = insert(self.model).values(pipeline_id=pipeline_id, topic=topic, kafka_partition=partition, next_offset=offset + 1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[self.model.pipeline_id, self.model.topic, self.model.kafka_partition],
            set_={"next_offset": stmt.excluded.next_offset, "updated_at": func.now()},
            where=self.model.next_offset <= offset,
        ).returning(self.model.next_offset)
        return (await self.session.execute(stmt)).first() is not None

    async def stored(self, *, pipeline_id: str, topics: Iterable[str]) -> dict[tuple[str, int], int]:
        """{(topic, partition): next offset not yet counted} as last committed."""
        result = await self.session.execute(
            select(self.model.topic, self.model.kafka_partition, self.model.next_offset)
            .where(self.model.pipeline_id == pipeline_id, self.model.topic.in_(list(topics)))
        )
        return {(t, int(p)): int(n) for t, p, n in result.all()}

    async def forget(self, *, pipeline_id: str, topic: str, partitions: Iterable[int]) -> None:
        wanted = list(partitions)
        if wanted:
            await self.session.execute(
                delete(self.model).where(
                    self.model.pipeline_id == pipeline_id, self.model.topic == topic, self.model.kafka_partition.in_(wanted),
                )
            )


class QualityStreamStatsRepository(OrgScopedRepository):
    model = models.QualityStreamStats

    async def get_by_pipeline_id(self, org_id: str, pipeline_id: str) -> models.QualityStreamStats | None:
        result = await self.session.execute(
            select(self.model).where(self.model.org_id == org_id, self.model.pipeline_id == pipeline_id)
        )
        return result.scalar_one_or_none()

    async def record_outcome(self, *, org_id: str, pipeline_id: str, quality_policy_id: str, valid: bool) -> None:
        """Atomic upsert increment (+= on the existing row, not a read-then-
        write) — one row per pipeline_id, created on first record. A single
        QualityStreamWorker is the only writer for a given pipeline_id today,
        but this stays correct even if that ever changes, since a lost
        update is impossible under ON CONFLICT DO UPDATE ... = column + n."""
        stmt = insert(self.model).values(
            org_id=org_id, pipeline_id=pipeline_id, quality_policy_id=quality_policy_id,
            total_count=1, failed_count=0 if valid else 1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[self.model.pipeline_id],
            set_={
                "quality_policy_id": quality_policy_id,
                "total_count": self.model.total_count + 1,
                "failed_count": self.model.failed_count + (0 if valid else 1),
            },
        )
        await self.session.execute(stmt)

    async def append_sample_failure(
        self, *, org_id: str, pipeline_id: str, envelope: dict[str, Any]
    ) -> None:
        """Best-effort append of one failing record's quarantine envelope to
        this pipeline's bounded sample, capped at
        settings.quality_stream_max_sample_failures (oldest dropped first) —
        same bounded-JSONB idea as QualityResult.sample_failures, sized for
        an ongoing stream rather than one batch (see that setting's own
        comment for why it's a separate value). A plain read-then-write, not
        folded into record_outcome's atomic upsert: record_outcome's own
        docstring already establishes a single QualityStreamWorker as the
        only writer for a given pipeline_id, so this is safe, and call this
        only after record_outcome has created/updated the row for this
        pipeline this run."""
        row = await self.get_by_pipeline_id(org_id, pipeline_id)
        if row is None:
            return
        limit = settings.quality_stream_max_sample_failures
        row.sample_failures = [*(row.sample_failures or []), envelope][-limit:]


class FailureCountRepository(SqlAlchemyRepository):
    """FE-BUG-17: see models/failure_count.py. add_counts is an atomic upsert
    increment (count = count + n), so concurrent writers — a quality worker per
    pipeline, a DLQ counter per pipeline, several app replicas — never lose an
    update."""

    model = models.FailureCount

    async def add_counts(self, rows: list[dict[str, Any]]) -> None:
        """Each row: org_id, pipeline_id, kind, key, bucket_start, count — one per
        unique (pipeline_id, kind, key, bucket_start); the caller aggregates first
        (one statement can't upsert the same key twice)."""
        if not rows:
            return
        stmt = insert(self.model).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[self.model.pipeline_id, self.model.kind, self.model.key, self.model.bucket_start],
            set_={"count": self.model.count + stmt.excluded.count},
        )
        await self.session.execute(stmt)

    async def sum_since(self, org_id: str, kind: str, since: Any, pipeline_id: str | None = None) -> dict[str, int]:
        """{key: total count} across all of the org's pipelines — or only `pipeline_id`'s, when given — for buckets that
        start at or after `since`."""
        stmt = (
            select(self.model.key, func.sum(self.model.count))
            .where(self.model.org_id == org_id, self.model.kind == kind, self.model.bucket_start >= since)
            .group_by(self.model.key)
        )
        if pipeline_id is not None:
            stmt = stmt.where(self.model.pipeline_id == pipeline_id)
        result = await self.session.execute(stmt)
        return {key: int(total) for key, total in result.all()}

    async def sum_since_by_pipeline(self, org_id: str, kind: str, key: str, since: Any) -> dict[str, int]:
        """{pipeline_id: total count of `key`} for buckets that start at or after `since` — e.g. failing records per pipeline."""
        stmt = (
            select(self.model.pipeline_id, func.sum(self.model.count))
            .where(self.model.org_id == org_id, self.model.kind == kind, self.model.key == key, self.model.bucket_start >= since)
            .group_by(self.model.pipeline_id)
        )
        result = await self.session.execute(stmt)
        return {pipeline_id: int(total) for pipeline_id, total in result.all()}

    async def delete_before(self, cutoff: Any) -> None:
        await self.session.execute(delete(self.model).where(self.model.bucket_start < cutoff))


class FailureCountOffsetRepository(SqlAlchemyRepository):
    """NEW-03 / NEW-04: see models/failure_count.py's FailureCountOffset. Every method runs on the caller's session and never commits, so the
    offsets move in the SAME transaction as the counts they cover. Keyed by topic: one topic, one progress, however many pipelines share it."""

    model = models.FailureCountOffset

    async def lock(self, *, org_id: str, kind: str, topic: str, partitions: Iterable[int]) -> dict[int, int]:
        """{partition: next offset not yet counted}, with those rows LOCKED until the transaction ends. A partition with no row yet starts at 0.
        The lock is what stops two replicas that were handed the same records (a rebalance) from both counting them: the second waits, then
        sees the first one's offsets and skips what they already covered. Rows are locked in partition order so two writers cannot deadlock."""
        wanted = sorted(set(partitions))
        if not wanted:
            return {}
        await self.session.execute(
            insert(self.model).values([
                {"org_id": org_id, "kind": kind, "topic": topic, "kafka_partition": p, "next_offset": 0} for p in wanted
            ]).on_conflict_do_nothing()
        )
        result = await self.session.execute(
            select(self.model.kafka_partition, self.model.next_offset)
            .where(self.model.kind == kind, self.model.topic == topic, self.model.kafka_partition.in_(wanted))
            .order_by(self.model.kafka_partition)
            .with_for_update()
        )
        return {int(p): int(n) for p, n in result.all()}

    async def advance(self, *, org_id: str, kind: str, topic: str, next_offsets: dict[int, int]) -> None:
        """Move each partition's offset forward to `next_offsets[partition]`; never backward."""
        if not next_offsets:
            return
        stmt = insert(self.model).values([
            {"org_id": org_id, "kind": kind, "topic": topic, "kafka_partition": p, "next_offset": n}
            for p, n in sorted(next_offsets.items())
        ])
        stmt = stmt.on_conflict_do_update(
            index_elements=[self.model.kind, self.model.topic, self.model.kafka_partition],
            set_={"next_offset": func.greatest(self.model.next_offset, stmt.excluded.next_offset), "updated_at": func.now()},
        )
        await self.session.execute(stmt)

    async def stored(self, *, kind: str, topic: str) -> dict[int, int]:
        """{partition: next offset not yet counted} as last committed, without locking."""
        result = await self.session.execute(
            select(self.model.kafka_partition, self.model.next_offset).where(self.model.kind == kind, self.model.topic == topic)
        )
        return {int(p): int(n) for p, n in result.all()}

    async def forget(self, *, kind: str, topic: str, partitions: Iterable[int]) -> None:
        wanted = list(partitions)
        if wanted:
            await self.session.execute(
                delete(self.model).where(
                    self.model.kind == kind, self.model.topic == topic, self.model.kafka_partition.in_(wanted),
                )
            )


class OperationRepository(OrgScopedRepository):
    model = models.Operation


class AuditLogRepository(OrgScopedRepository):
    model = models.AuditLog


class DiscoveryRunRepository(OrgScopedRepository):
    model = models.DiscoveryRun

    async def list_prunable_ids(self, cutoff: Any) -> list[str]:
        """GAP-10 retention: finished runs older than `cutoff`, except the most recent successful run of each
        connection (what the UI shows) — and never a queued/running one."""
        newest_success = (
            select(self.model.id)
            .where(self.model.status == "succeeded")
            .order_by(self.model.connection_id, self.model.created_at.desc())
            .distinct(self.model.connection_id)
        )
        stmt = select(self.model.id).where(
            self.model.created_at < cutoff,
            self.model.status.in_(("succeeded", "failed")),
            self.model.id.not_in(newest_success),
        )
        return [str(run_id) for run_id in (await self.session.execute(stmt)).scalars().all()]

    async def delete_by_ids(self, run_ids: list[str]) -> None:
        if run_ids:
            await self.session.execute(delete(self.model).where(self.model.id.in_(run_ids)))


class DiscoveryObjectRepository(SqlAlchemyRepository):
    model = models.DiscoveryObject

    async def bulk_create(self, rows: list[dict[str, Any]]) -> None:
        """One INSERT for the whole batch (GAP-10) — a large source can mean
        tens of thousands of rows; inserting them one at a time (one flush/
        round-trip each, the pattern SqlAlchemyRepository.create uses) would
        be needlessly slow at that scale."""
        if not rows:
            return
        await self.session.execute(insert(self.model), rows)

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
    ) -> tuple[list[models.DiscoveryObject], str | None]:
        """Keyset pagination over (schema, name, id) — see app/repositories/discovery_cursor.py. Fetches one extra row to know whether there's a
        next page without a separate COUNT query.

        `object_type`/`schema_name` (FE-BUG-12) are exact-match filters —
        unlike `search`, these come from a closed set of options the UI gets
        from list_facets, so there's no need for ilike here."""
        schema_key = func.coalesce(self.model.schema_name, "")
        stmt = select(self.model).where(
            self.model.discovery_run_id == discovery_run_id, self.model.org_id == org_id,
        )
        if cursor:
            # Raises InvalidCursorError for a token this service didn't issue.
            after_schema, after_name, after_id = decode_cursor(cursor)
            # The id is a UUID column: bind it as one, or Postgres refuses to compare a UUID with text.
            stmt = stmt.where(
                tuple_(schema_key, self.model.name, self.model.id)
                > tuple_(after_schema, after_name, literal(after_id, type_=self.model.id.type))
            )
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(self.model.name.ilike(like), self.model.schema_name.ilike(like))
            )
        if object_type:
            stmt = stmt.where(self.model.object_type == object_type)
        if schema_name:
            stmt = stmt.where(self.model.schema_name == schema_name)
        stmt = stmt.order_by(schema_key, self.model.name, self.model.id).limit(limit + 1)
        rows = await self.list(stmt)
        next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
        return rows[:limit], next_cursor

    async def delete_for_runs(self, run_ids: list[str]) -> None:
        """Remove every object of the given runs — a re-executed discovery replaces its previous attempt's rows, and
        retention deletes a pruned run's objects."""
        if run_ids:
            await self.session.execute(delete(self.model).where(self.model.discovery_run_id.in_(run_ids)))

    async def list_facets(self, *, discovery_run_id: str, org_id: str) -> tuple[list[str], list[str]]:
        """Distinct schema names and object types for a run, computed in SQL
        (FE-BUG-12) so the UI can populate filter dropdowns without paging
        through — and holding in memory — every discovered object first."""
        schema_stmt = (
            select(self.model.schema_name)
            .where(
                self.model.discovery_run_id == discovery_run_id,
                self.model.org_id == org_id,
                self.model.schema_name.is_not(None),
            )
            .distinct()
            .order_by(self.model.schema_name)
        )
        type_stmt = (
            select(self.model.object_type)
            .where(self.model.discovery_run_id == discovery_run_id, self.model.org_id == org_id)
            .distinct()
            .order_by(self.model.object_type)
        )
        schema_names = (await self.session.execute(schema_stmt)).scalars().all()
        object_types = (await self.session.execute(type_stmt)).scalars().all()
        return list(schema_names), list(object_types)

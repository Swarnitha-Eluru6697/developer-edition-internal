"""All SQLAlchemy models in one module, mirroring workspace-service's
convention.

Schema overview:
  projects       — a registered dbt project (Git ref + schedule + engine)
  runs           — one execution of a project (manual or scheduled)
  run_events     — structured log lines / dbt node results for a run
  lineage_edges  — dataset-level lineage produced by a successful run
  git_credentials — per-org OAuth/PAT for cloning (vault references only)
  scheduled_occurrences — one row per (project, cron fire time) actually
    dispatched; its unique constraint is what stops two scheduler replicas
    from double-firing the same occurrence (see app/services/scheduler.py)
  metadata_outbox_events — durable queue of lineage/dataset-registration
    events awaiting delivery to UCS/Datasets (see app/services/metadata_outbox.py)
  project_dependents — "if this project fails, these projects fail too": a job
    link with no data meaning (see app/services/dependents.py)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


# ---------- Projects ----------
class Project(Base):
    """A registered dbt project. The registry is shared by the whole org: access is decided by org + role capability, never
    by workspace. `workspace_id` is a grouping label (it lets the Workspace UI show the projects registered from a
    workspace), NOT an ownership or isolation boundary (DBT-012). It is set only when the Workspace service verified that
    the caller owns that workspace (signed delegated token claim); an external CI system registers with no workspace_id."""
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_org", "org_id"),
        Index("ix_projects_workspace", "workspace_id"),
        Index("ix_projects_git_trigger", "org_id", postgresql_where=text("git_trigger_enabled")),
        UniqueConstraint("org_id", "git_url", "git_ref", "project_path", name="uq_project_repo_ref"),
        CheckConstraint(
            "engine IN ('trino', 'pg_lake', 'clickhouse')",
            name="ck_project_engine",
        ),
        CheckConstraint("kind IN ('dbt', 'sql', 'python')", name="ck_project_kind"),
        CheckConstraint("connection_id IS NULL OR kind = 'sql'", name="ck_project_connection_sql_only"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # What runs: 'dbt' — a dbt project (the folder with dbt_project.yml in `project_path`); 'sql' — one .sql file (`project_path` is
    # the file), its statements run in order against `engine` (app/executors/sql_job_executor.py).
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="dbt", server_default="dbt")
    # 'python' — one .py script (`project_path`), run in its own virtualenv built from the requirements.txt next to it
    # (app/executors/python_job_executor.py). `connection_id` (SQL jobs only): the Workspace connection the statements run on;
    # NULL runs them on the runner's warehouse engine. The credentials stay in the Workspace.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Grouping label: the Workspace the project was registered from, if any (verified by Workspace). Nullable.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    registered_by_user_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # A clean repository URL: never user info (DBT-009). The credential, if any, is named by `credential_ref`.
    git_url: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(64))
    git_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    # Folder of the dbt project inside the repo ('' = repo root / the only
    # dbt_project.yml). Lets one repo hold several independent projects.
    project_path: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default="",
    )
    # 'trino' (against Polaris-backed Iceberg) or 'pg_lake' (Postgres+pg_lake).
    # See Workspace TDD 4.3 / the architecture doc's dialect-router design.
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="trino")
    schedule_cron: Mapped[str | None] = mapped_column(String(120))
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "Run on new commit" (DG-008): Dagster's git sensor watches the projects with this on, whether or not they have a cron schedule.
    git_trigger_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_synced_sha: Mapped[str | None] = mapped_column(String(40))
    # schema.yml test blocks rendered by the DQ Service, keyed by target_table.
    # Written into the cloned project before dbt build — never committed back
    # to the customer's Git repo.
    dq_schema_tests: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="DQ-rendered schema.yml test blocks: {target_table: yaml_text}",
    )
    # Per stored block: which DQ ArtifactVersion it is and the apply's
    # idempotency key — {target_model: {artifact_version_id, binding_id,
    # idempotency_key, content_hash, renderer_version, applied_at}}.
    dq_schema_tests_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    runs = relationship("Run", back_populates="project", cascade="all, delete-orphan")


class ProjectDependent(Base):
    """One Dependents link: when a run of `project_id` fails, `dependent_project_id` (and, through its own links, everything
    below it) fails too — see app/services/dependents.py. Purely a job link: it says nothing about data, and the projects need
    not share any table. Both projects belong to the same org (checked by the API); deleting either removes the link."""
    __tablename__ = "project_dependents"
    __table_args__ = (
        CheckConstraint("project_id <> dependent_project_id", name="ck_project_dependent_not_self"),
        Index("ix_project_dependents_dependent", "dependent_project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    dependent_project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Runs ----------
class Run(Base):
    """One execution of a project — a `dbt build` (or `dbt run` + `dbt
    test`) against the project's configured engine."""
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_project_created", "project_id", "created_at"),
        Index("uq_runs_idempotency_key", "project_id", "idempotency_key", unique=True,
              postgresql_where="idempotency_key IS NOT NULL"),
        Index("ix_runs_pending", "status",
              postgresql_where="status IN ('queued', 'running', 'cancelling')"),
        CheckConstraint(
            "status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled')",
            name="ck_run_status",
        ),
        CheckConstraint(
            "trigger IN ('manual', 'scheduled', 'api')",
            name="ck_run_trigger",
        ),
        CheckConstraint("cancel_outcome IS NULL OR cancel_outcome = 'failed'", name="ck_run_cancel_outcome"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(255))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    # dbt selector, e.g. '--select state:modified+' — None means full build.
    select: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    models_total: Mapped[int | None] = mapped_column(Integer)
    models_succeeded: Mapped[int | None] = mapped_column(Integer)
    models_failed: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    # --- Ownership / lease (RUN-DBT-003) ---
    # Incremented by every successful queued->running claim. Names the k8s
    # Job/Secret for that attempt (so an attempt only ever touches its own
    # resources) and keys the durable result artifact.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The process currently responsible for driving this run forward. Only
    # it may dispatch/finalize; another replica may take over only once
    # lease_expires_at has passed (the owner stopped heartbeating).
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # k8s_job mode: the Job created for `attempt` (recorded BEFORE the Job is
    # created, so a cancel can always find it), and when it was released from
    # suspension (i.e. allowed to start executing customer code). A missing
    # Job whose release was never recorded provably never ran.
    k8s_job_name: Mapped[str | None] = mapped_column(String(63))
    job_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Durable artifact-store reference of the recorded result, if any.
    result_ref: Mapped[str | None] = mapped_column(Text)
    # DBT-005: when a user asked to cancel a RUNNING run. The run is 'cancelling' from then until the
    # cluster confirms its Job is gone or finished; only then is it 'cancelled' (completed_at is set then).
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What a 'cancelling' run becomes once its Job is confirmed gone: NULL = 'cancelled' (a person cancelled it), 'failed' = it
    # was stopped because a project it is a Dependent of failed (app/services/dependents.py).
    cancel_outcome: Mapped[str | None] = mapped_column(String(20))
    # DBT-006/007: what this run executes, frozen at submission (see app/services/run_manifest.py): the resolved
    # Git commit, project folder, engine, selector and the DQ schema blocks with their artifact versions and hashes.
    # `git_sha` is the manifest's commit, set at submission. A database trigger (migration 0009) refuses to change
    # any of the three once set. NULL only for a run queued by an older runner version (frozen at first dispatch).
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    # X-004: a client-supplied key makes a retried submission return the run it created instead of a second one;
    # `idempotency_request_hash` binds the key to what was asked (a different request with the key is refused).
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    idempotency_request_hash: Mapped[str | None] = mapped_column(String(64))
    # X-004: evidence recorded when the build finished — Git's own hash (tree id) of the project folder that was
    # actually built. With `git_sha` it content-addresses the SQL the run executed.
    source_tree_sha: Mapped[str | None] = mapped_column(String(64))

    project = relationship("Project", back_populates="runs")
    events = relationship("RunEvent", back_populates="run", cascade="all, delete-orphan")
    lineage_edges = relationship("LineageEdge", back_populates="run", cascade="all, delete-orphan")


class RunEvent(Base):
    """Structured log line / dbt node-result event for a run. Streamed to
    the Workspace 'recent runs' panel (TDD 4.3.1) by polling or
    subscribing — see GET /api/v1/projects/{id}/runs and the run-detail
    endpoint."""
    __tablename__ = "run_events"
    __table_args__ = (
        Index("ix_run_events_run", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False,
    )
    # 'log' | 'node_start' | 'node_finish' | 'status'
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(255))  # dbt unique_id, e.g. model.proj.stg_orders
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    run = relationship("Run", back_populates="events")


# ---------- Lineage ----------
class LineageEdge(Base):
    """One edge in the dataset lineage graph produced by a successful run.
    Surfaced via GET /api/v1/projects/{id}/lineage (TDD 4.3.3) and
    contributed to the Datasets product (TDD 4.4.2: 'authored by which
    user, in which workspace, against which Git ref')."""
    __tablename__ = "lineage_edges"
    __table_args__ = (
        Index("ix_lineage_run", "run_id"),
        Index("ix_lineage_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    source_table: Mapped[str] = mapped_column(String(500), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(20), nullable=False, default="ref")  # ref | source
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    run = relationship("Run", back_populates="lineage_edges")


# ---------- Scheduling ----------
class ScheduledOccurrence(Base):
    """One (project, cron fire time) the scheduler has actually dispatched.

    The unique constraint below is the hard correctness guarantee behind
    RUN-DBT-004: two scheduler replicas racing to fire the same logical
    occurrence both attempt to insert this row, but only one insert can
    succeed — the loser's IntegrityError is caught and treated as 'someone
    else already handled this' rather than firing a second Run. A
    per-project Postgres advisory lock (see app/services/scheduler.py)
    keeps this from being a two-INSERTs-racing thing in the common case at
    all; the constraint is what makes it correct even if that lock's
    scoping doesn't fully serialize (e.g. a lock-key hash collision)."""
    __tablename__ = "scheduled_occurrences"
    __table_args__ = (
        UniqueConstraint("project_id", "scheduled_fire_time", name="uq_scheduled_occurrence"),
        Index("ix_scheduled_occurrences_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    scheduled_fire_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Metadata outbox ----------
class MetadataOutboxEvent(Base):
    """One canonical metadata event (OpenLineage publish, dataset
    registration) queued for delivery to an external governance endpoint
    (UCS, Datasets) — RUN-DBT-008.

    Written in the SAME transaction that marks a Run terminal (see
    run_service._finalize_run()), so a run can never succeed while the
    event that was supposed to announce it silently vanishes because UCS
    or Datasets happened to be down at that exact moment — the row is
    durable and outlives that instant. A separate background worker
    (app.services.metadata_outbox) delivers pending rows and retries with
    backoff until the endpoint acknowledges (2xx), up to
    metadata_outbox_max_attempts, after which the row is moved to
    'dead_letter' (logged + counted) instead of retrying forever; a row
    whose sink is no longer configured is marked 'skipped'. The
    idempotency_key is also sent downstream as an Idempotency-Key header.
    idempotency_key's uniqueness is the
    hard guarantee against enqueueing the same logical event twice —
    _finalize_run() itself already only runs once per run (RUN-DBT-003's
    guarded claim), so this is a second, independent layer, not the only
    one."""
    __tablename__ = "metadata_outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_metadata_outbox_idempotency_key"),
        Index("ix_metadata_outbox_pending", "status", "next_attempt_at",
              postgresql_where="status = 'pending'"),
        CheckConstraint("status IN ('pending', 'delivered', 'dead_letter', 'skipped')",
                        name="ck_metadata_outbox_status"),
        CheckConstraint("event_type IN ('openlineage', 'dataset_registration')",
                        name="ck_metadata_outbox_event_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Scoped service-token replay protection ----------
class ServiceTokenJti(Base):
    """One consumed `jti` of a scoped service JWT (RUN-DBT-005). The primary
    key makes a replayed token fail its INSERT; rows are purged once the
    token they record has expired (a replay of an expired token already
    fails the exp check)."""
    __tablename__ = "service_token_jti"
    __table_args__ = (Index("ix_service_token_jti_expires", "expires_at"),)

    jti: Mapped[str] = mapped_column(String(255), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Git ----------
class GitCredential(Base):
    """Per-org Git credential for cloning registered projects. Same
    vault-reference-only pattern as workspace-service's GitCredential —
    no token bytes in this table."""
    __tablename__ = "git_credentials"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", "instance_url", name="uq_dbtrunner_git_cred"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    instance_url: Mapped[str | None] = mapped_column(String(500))
    vault_path: Mapped[str] = mapped_column(String(500), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

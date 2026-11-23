"""All SQLAlchemy models in one module for simplicity.

Schema overview:
  users               — mirror of the user identity from JWT claims
  workspaces          — one or more per user. Per Snowflake-style model:
                        each workspace has its own storage prefix.
  workspace_files     — file metadata (content lives in object storage)
  git_bindings        — links a workspace to an external Git repo
  git_credentials     — per-user OAuth tokens (vault references only)
  sessions            — active workspace sessions (browser tabs effectively)
  executions          — every SQL or notebook cell execution
  execution_outputs   — output rows/chunks/MIME bundles for completed executions
  audit_logs          — append-only record of every state-changing action
  codegen_sessions    — one chat thread per file/target the user is authoring
  codegen_messages    — turn-by-turn chat + tool-use history for a codegen session
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


# ---------- Users ----------
class User(Base):
    """Local mirror of identity. Created lazily on first JWT validation."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # The control plane's immutable subject (the token's `sub`, injected by the gateway as x-user-id) — the one identity every
    # service uses (W-020, X-003). Rows older than that hold the gateway username here until the first request re-keys them.
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Display attribute: the gateway's x-user-name. Never a key.
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    workspaces = relationship("Workspace", back_populates="owner", cascade="all, delete-orphan")


# ---------- Workspaces ----------
class Workspace(Base):
    """A user's personal (or later shared) workspace.

    Files live at:  {storage_bucket}/{org_id}/{user_id}/{workspace_id}/...
    Metadata lives in workspace_files.
    """
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_workspace_owner_name"),
        Index("ix_workspaces_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    workspace_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="personal",
        # 'personal' in Phase 1; 'shared' added in Phase 2 (no migration needed)
    )
    storage_prefix: Mapped[str] = mapped_column(String(500), nullable=False)
    # Default execution targets (overridable per execution)
    default_warehouse: Mapped[str | None] = mapped_column(String(255))
    default_kernel_image: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner = relationship("User", back_populates="workspaces")
    files = relationship(
        "WorkspaceFile", back_populates="workspace", cascade="all, delete-orphan",
    )
    git_binding = relationship(
        "GitBinding", back_populates="workspace", uselist=False,
        cascade="all, delete-orphan",
    )


# ---------- Files ----------
class WorkspaceFile(Base):
    """File metadata. Content is in object storage at:
       {workspace.storage_prefix}/{branch}/{path}
    """
    __tablename__ = "workspace_files"
    __table_args__ = (
        UniqueConstraint("workspace_id", "path", "branch", name="uq_file_path_branch"),
        Index("ix_files_workspace_lang", "workspace_id", "language"),
        CheckConstraint(
            "language IN ('sql', 'flinksql', 'ipynb', 'py', 'md', 'yaml', 'json', 'other')",
            name="ck_file_language",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Phase 1: sql, ipynb. Phase 2+ accepts py, md, yaml, json, other.
    # 'flinksql' (*.flink.sql) is streaming SQL for the FlinkSQL Runner — it is
    # edited like sql but never dispatched to the lakehouse SQL executor.
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    last_committed_sha: Mapped[str | None] = mapped_column(String(40))
    is_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    workspace = relationship("Workspace", back_populates="files")


# ---------- Git ----------
class GitCredential(Base):
    """Per-user OAuth credentials for a Git provider.

    Tokens NEVER live in this table. We store a reference to the secrets vault
    where the encrypted token actually lives.
    """
    __tablename__ = "git_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "instance_url", name="uq_git_cred"),
        Index("ix_git_creds_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # github / gitlab / azure_devops
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_username: Mapped[str | None] = mapped_column(String(255))
    instance_url: Mapped[str | None] = mapped_column(String(500))  # for self-hosted
    vault_path: Mapped[str] = mapped_column(String(500), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class GitBinding(Base):
    """Links a workspace to a remote Git repo. One per workspace."""
    __tablename__ = "git_bindings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_name: Mapped[str | None] = mapped_column(String(500))  # owner/repo
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    current_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    # W-014: the branch/tag the user explicitly asked for (NULL = the repository's default). `last_synced_sha` is the commit the
    # import actually checked out: requested ref and actual immutable commit are both recorded.
    requested_ref: Mapped[str | None] = mapped_column(String(255))
    last_synced_sha: Mapped[str | None] = mapped_column(String(40))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'provisioning' while the initial clone+import is running, 'ready' once
    # it (or a plain bind_repo with no clone) succeeded, 'failed' if the
    # clone/import didn't complete — see create_workspace_from_git
    # (WS-GIT-009: previously a partial failure left no persisted trace).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    workspace = relationship("Workspace", back_populates="git_binding")


# ---------- Sessions ----------
class Session(Base):
    """A workspace session. Roughly one per browser tab the user has open.

    status: 'active' while the tab is open and either connected or within
    session_idle_timeout_minutes of its last WS activity; 'idle' once the
    lifecycle reconciler closes it out for inactivity (WS-LIFE-013);
    'terminated' when the client explicitly ends it (DELETE /sessions/{id}).
    No CheckConstraint — kept a free-form column like GitBinding.status.
    """
    __tablename__ = "sessions"
    __table_args__ = (
        Index(
            "ix_sessions_user_active", "user_id",
            postgresql_where="status = 'active'",
        ),
        Index(
            "ix_sessions_idle_cleanup", "last_activity_at",
            postgresql_where="status = 'active'",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- Executions ----------
class Execution(Base):
    """One run of code: a SQL query or a notebook cell."""
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_user_created", "user_id", "created_at"),
        Index("ix_executions_session", "session_id"),
        Index(
            "ix_executions_pending", "status",
            postgresql_where="status IN ('pending', 'running')",
        ),
        CheckConstraint(
            "language IN ('sql', 'ipynb_cell')",
            name="ck_execution_language",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'timeout')",
            name="ck_execution_status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_files.id", ondelete="SET NULL"),
    )
    # For notebook cells, this is the cell_id within the .ipynb
    cell_id: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(20), nullable=False)  # sql / ipynb_cell
    code_text: Mapped[str] = mapped_column(Text, nullable=False)
    code_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Execution target — warehouse name for SQL; kernel/pod id for notebook
    target: Mapped[str | None] = mapped_column(String(255))
    # Persisted alongside code_text/target so the row is a fully self-contained,
    # immutable command — a controller can replay it (e.g. after a restart)
    # without needing anything beyond this table. See ExecutionRouter.
    max_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=10_000)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    row_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # Correlation of the submitting request, so the worker's logs for this
    # run join up with the API request that created it (WS-016).
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    # The submitter's Keycloak groups and permissions (JSON arrays), so the worker can re-check UCS
    # authorization for a connection at run time with the same identity —
    # a grant revoked between submit and run is enforced (WS-DATA-007).
    principal_groups: Mapped[str | None] = mapped_column(Text)
    principal_permissions: Mapped[str | None] = mapped_column(Text)
    # W-017: a user's cancel request is recorded HERE (the source of truth),
    # not in Redis; Redis only wakes the worker sooner.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # W-018: who owns a running row and until when. Written in the SAME
    # statement that claims the row (pending -> running), so a running row
    # always has an owner; the worker extends the lease while it runs, and a
    # row whose lease lapsed belongs to a dead worker (see _reconcile).
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionOutput(Base):
    """Output chunk for a completed execution.

    For SQL: chunks of rows. chunk 0 includes columns.
    For notebook cells: MIME bundles, with output_type set.
    """
    __tablename__ = "execution_outputs"
    __table_args__ = (
        UniqueConstraint("execution_id", "chunk_index", name="uq_output_chunk"),
        Index("ix_outputs_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("executions.id", ondelete="CASCADE"), nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # For SQL: 'rows'. For notebook: 'stream' | 'display_data' | 'execute_result' | 'error'
    output_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Audit ----------
class AuditLog(Base):
    """Append-only audit log. Never deleted, never updated."""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_action", "action", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# ---------- Codegen chat (SQL / dbt / FlinkSQL authoring assistant) ----------
class CodegenSession(Base):
    """One chat thread. Scoped to a workspace, optionally to a single file
    the user has open (so the assistant knows what it's editing).

    target tracks which backend/dialect the chat is generating for. The
    router picks a backend based on this field; see app/services/codegen.py.
    """
    __tablename__ = "codegen_sessions"
    __table_args__ = (
        Index("ix_codegen_sessions_workspace", "workspace_id"),
        Index("ix_codegen_sessions_user", "user_id"),
        CheckConstraint(
            "target IN ('sql', 'python', 'dbt', 'flinksql')",
            name="ck_codegen_target",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_files.id", ondelete="SET NULL"),
    )
    target: Mapped[str] = mapped_column(String(20), nullable=False)  # sql | dbt | flinksql
    title: Mapped[str | None] = mapped_column(String(255))
    # Engine dialect the target compiles against — informs system prompt and
    # validation, e.g. 'trino', 'pg_lake', 'flink'. Defaults from workspace
    # config; user can override per-session.
    engine: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages = relationship(
        "CodegenMessage", back_populates="session",
        order_by="CodegenMessage.created_at",
        cascade="all, delete-orphan",
    )


class CodegenMessage(Base):
    """One turn in a codegen chat (user / assistant, plus a legacy 'tool'
    role kept by the check constraint for existing rows). The codegen agent's
    validation outcome lives on generated_code / validation_status /
    validation_detail, so a turn is reconstructable for audit.
    """
    __tablename__ = "codegen_messages"
    __table_args__ = (
        Index("ix_codegen_messages_session", "session_id", "created_at"),
        Index(
            "ix_codegen_messages_running", "created_at",
            postgresql_where="status = 'running'",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="ck_codegen_message_role",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_codegen_message_status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("codegen_sessions.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Content blocks, e.g. [{"type": "text", "text": ...}] — JSONB so older
    # rows written in the previous provider's block shape still load.
    content: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    # Populated on assistant messages that produced a candidate artifact —
    # lets the UI diff/apply without re-parsing the chat transcript.
    generated_code: Mapped[str | None] = mapped_column(Text)
    generated_path: Mapped[str | None] = mapped_column(String(1024))
    validation_status: Mapped[str | None] = mapped_column(String(20))  # passed | failed | skipped
    validation_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # 'running' from the moment an assistant placeholder is written (before
    # the agent loop starts) until _run_turn updates it in place — lets a
    # restart-safe reconciler find turns orphaned by a mid-turn crash
    # (WS-LIFE-013), the same way Execution.status already does for runs.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    # The caller's live workspace-session id at submit time — which Redis
    # channel (see session_channel_for) to notify on if this turn has to be
    # reconciled as failed after the fact. Not a FK: purely a notification
    # target, never joined against `sessions`.
    ws_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    session = relationship("CodegenSession", back_populates="messages")


class CodegenOrgSettings(Base):
    """Org-level governance policy for codegen source-context egress
    (WS-AI-011). One row per org — this deployment only ever serves the
    single org in `settings.org_id`, but the row is still keyed by it rather
    than being a bare singleton, so the policy decision is explicit and
    auditable rather than implied by "the only row that exists".

    Defaults are fail-closed: a fresh deployment gets
    allow_external_context=False and an empty provider allowlist, so no
    file content leaves the customer boundary until an org admin makes that
    an explicit, recorded decision via PUT /api/v1/codegen/org-settings.
    """
    __tablename__ = "codegen_org_settings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    allow_external_context: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Model providers allowed to receive source context when the toggle above
    # is on, e.g. ["codegen_gateway"]. Empty means none — the toggle alone is not
    # sufficient, both gates must agree.
    allowed_providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    redact_secrets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


# ---------- Database connections (catalog) ----------
class DatabaseConnection(Base):
    """A named, reusable connection to a query engine or data source.

    Users add connections once (e.g. their Trino cluster, a pg_lake
    instance, a Postgres database) and then select them as the execution
    target when running SQL or registering a dbt project. Credentials are
    never stored here — only a reference to the secrets vault path.

    connection_type drives which execution path / dbt adapter is used:
      trino    → dbt-trino / lakehouse SQL executor
      pg_lake  → dbt-postgres (pg_lake wire protocol) / direct pg_lake SQL
      postgres → vanilla Postgres (non-Iceberg, ad hoc SQL only)
      flink    → FlinkSQL job executor (Phase 3)
      other    → custom JDBC-style, ad hoc SQL only
    """
    __tablename__ = "database_connections"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_dbconn_org_name"),
        Index("ix_dbconn_org", "org_id"),
        Index("ix_dbconn_user", "created_by_user_id"),
        CheckConstraint(
            "connection_type IN ('trino','pg_lake','postgres','flink','other')",
            name="ck_dbconn_type",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    connection_type: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[str | None] = mapped_column(String(500))
    port: Mapped[int | None] = mapped_column(Integer)
    database_name: Mapped[str | None] = mapped_column(String(255))
    # For Trino: catalog name in Polaris/Iceberg REST catalog
    catalog_name: Mapped[str | None] = mapped_column(String(255))
    # W-011: the canonical resource UCS authorizes this connection's use by — the linked UCS catalog (verified when linked) or
    # `<catalog>.connection.<id>`. Persisted, never derived from the mutable name; a rename does not change it.
    ucs_resource_fqn: Mapped[str | None] = mapped_column(String(500))
    # Credential reference only — raw creds never stored here
    vault_secret_path: Mapped[str | None] = mapped_column(String(500))
    # TLS
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Connection tested and confirmed working
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Extra driver properties as JSONB (e.g. Trino http_scheme, auth method)
    extra_properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


# ---------- SQL jobs on a connection ----------
class JobConnectionBinding(Base):
    """Which Workspace connection a dbt-runner SQL job runs on, and as whom.

    Written when the job is registered through the Workspace (after the registering user was checked to be allowed to use that
    connection) and read when the runner later asks the Workspace to run the job's statements. The runner is only trusted to say
    WHICH registered job is running; the connection, the user it runs as and that user's groups / permissions are this row's, never
    the request's.
    Removed with the job (or the connection)."""
    __tablename__ = "job_connection_bindings"
    __table_args__ = (Index("ix_job_conn_bindings_connection", "connection_id"),)

    # The dbt Runner's project id (a different service's key, so no foreign key).
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # The user's Keycloak groups and permissions (JSON arrays) when they registered the job (a run has no signed-in user to ask).
    groups: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    permissions: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------- Alerts ----------
class AlertChannel(Base):
    """Where alerts are posted: a Slack or Microsoft Teams incoming webhook.

    The webhook URL is a credential (anyone holding it can post into the channel), so it is NEVER stored here: it lives in the
    secrets backend at `secret_ref`, and only a short non-secret `url_hint` ("hooks.slack.com/…/x7Q2") is kept for display."""
    __tablename__ = "alert_channels"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_alert_channel_org_name"),
        CheckConstraint("kind IN ('slack','teams')", name="ck_alert_channel_kind"),
        CheckConstraint("min_severity IN ('low','medium','high')", name="ck_alert_channel_severity"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    url_hint: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    min_severity: Mapped[str] = mapped_column(String(10), nullable=False, default="low")
    # One job (a dbt/SQL/Python project or a Flink job) or NULL = every job. The name is a snapshot for display.
    scope_job_key: Mapped[str | None] = mapped_column(String(100))
    scope_job_name: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_detail: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AlertRule(Base):
    """What raises an alert: a kind of failure, how much of it, over which window, for which job. Stored as data and evaluated
    by every sweep (app/services/alerts/engine.py)."""
    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_alert_rule_org_name"),
        CheckConstraint("severity IN ('low','medium','high')", name="ck_alert_rule_severity"),
        CheckConstraint("threshold > 0", name="ck_alert_rule_threshold"),
        CheckConstraint("min_runs >= 1", name="ck_alert_rule_min_runs"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")
    threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)    # 0 = the default (24 hours)
    min_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope_job_key: Mapped[str | None] = mapped_column(String(100))
    scope_job_name: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AlertObservation(Base):
    """One thing the sweep saw happen to a job: a run that finished, a run Dagster refused, a Flink job's state. Rules are evaluated
    over these (not over live calls), so history survives a runner pruning its runs. Unique per (source, ref_id): seen twice = once."""
    __tablename__ = "alert_observations"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "ref_id", name="uq_alert_observation_ref"),
        Index("ix_alert_observation_job", "org_id", "job_key", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)       # dbt-runner | flink | dagster
    ref_id: Mapped[str] = mapped_column(String(200), nullable=False)
    job_key: Mapped[str] = mapped_column(String(100), nullable=False)
    job_name: Mapped[str] = mapped_column(String(300), nullable=False)
    job_kind: Mapped[str] = mapped_column(String(20), nullable=False)     # dbt | sql | python | flink
    category: Mapped[str] = mapped_column(String(20), nullable=False)     # run | blocked | flink_state
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)      # succeeded | failed | blocked | running | ...
    failure_class: Mapped[str | None] = mapped_column(String(20))         # quality | None
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Alert(Base):
    """One raised alert (a rule breached for a job). open -> acknowledged -> resolved; resolved also happens by itself when the
    condition clears. While one is open or acknowledged the same breach does not raise another (no channel flooding)."""
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("severity IN ('low','medium','high')", name="ck_alert_severity"),
        CheckConstraint("state IN ('open','acknowledged','resolved')", name="ck_alert_state"),
        CheckConstraint("delivery IN ('pending','sent','no_channel','failed')", name="ck_alert_delivery"),
        Index("ix_alert_org_state", "org_id", "state", "raised_at"),
        Index("ix_alert_fingerprint", "org_id", "fingerprint", "state"),
        # At most one live (open / acknowledged) alert per rule + job: two sweeps racing cannot raise it twice.
        Index("uq_alert_live_fingerprint", "org_id", "fingerprint", unique=True,
              postgresql_where=text("state IN ('open','acknowledged')"), sqlite_where=text("state IN ('open','acknowledged')")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="SET NULL"))
    rule_name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    job_key: Mapped[str] = mapped_column(String(100), nullable=False)
    job_name: Mapped[str] = mapped_column(String(300), nullable=False)
    job_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)       # "<rule id>:<job key>"
    state: Mapped[str] = mapped_column(String(15), nullable=False, default="open")
    # pending = not tried yet / retrying; sent = at least one channel got it; no_channel = no channel matches (yet); failed = every
    # matching channel refused it (retried by later sweeps).
    delivery: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    events: Mapped[list["AlertEvent"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", order_by="AlertEvent.at")


class AlertEvent(Base):
    """History of an alert: raised, sent to a channel, delivery failed, acknowledged, resolved, cleared automatically."""
    __tablename__ = "alert_events"
    __table_args__ = (Index("ix_alert_event_alert", "alert_id", "at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    alert_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    alert: Mapped[Alert] = relationship(back_populates="events")


class AlertState(Base):
    """One row per org: whether the default rules were created, and the sweep's lease (so several replicas never sweep at once)."""
    __tablename__ = "alert_state"

    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    defaults_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sweep_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sweep_detail: Mapped[str | None] = mapped_column(Text)

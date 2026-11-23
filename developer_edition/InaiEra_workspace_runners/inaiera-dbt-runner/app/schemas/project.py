"""Project, run, and lineage schemas — the public contract documented in
the Workspace TDD section 4.3.3.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from typing import Any

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from app.core.git_credentials import REF_PATTERN, assert_clean_url
from app.core.project_path import normalize_project_path, split_git_url
from app.schemas.common import ORMModel

Engine = Literal["trino", "pg_lake", "clickhouse"]
# 'dbt': a dbt project folder. 'sql': one .sql file of the repo, its statements run in order against the engine (or a Workspace
# connection). 'python': one .py script of the repo, run in its own virtualenv.
JobKind = Literal["dbt", "sql", "python"]
_JOB_FILE_SUFFIX = {"sql": ".sql", "python": ".py"}
# "cancelling" (DBT-005): a user asked to cancel a running run; its Job is being terminated and `cancelled` is
# reported only once the cluster confirms it. Not terminal: clients keep polling until `cancelled`.
RunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]
RunTrigger = Literal["manual", "scheduled", "api"]


class ProjectRegister(BaseModel):
    """POST /api/v1/projects/register — TDD 4.3.3."""
    git_url: str
    git_ref: str = "main"
    # Folder holding dbt_project.yml when the repo has several dbt projects.
    # Also accepted as a git_url suffix: ...repo.git#subdirectory=folder
    project_path: str | None = None
    name: str | None = None  # defaults to repo name (+ folder) if omitted
    kind: JobKind = "dbt"
    # SQL jobs only: run on this Workspace connection (Database Explorer) instead of the runner's warehouse engine. The Workspace
    # proxy checks the caller may use it and records who registered the job; the credentials never reach this service.
    connection_id: UUID | None = None
    # A grouping label, not an ownership boundary (the registry is org-shared). Accepted only when the Workspace service
    # vouched for it in the delegated token (DBT-012); external CI omits it.
    workspace_id: UUID | None = None
    engine: Engine = "trino"
    schedule_cron: str | None = None
    # Run on every new commit of git_ref (watched by Dagster's git sensor), independent of schedule_cron (DG-008).
    git_trigger_enabled: bool = False
    # Names a credential the operator provisioned for the runner (GIT_CREDENTIAL_<REF>); the token is never part of the URL (DBT-009).
    credential_ref: str | None = Field(default=None, pattern=REF_PATTERN.pattern)
    # Dependents: projects that fail when this one fails (everything below them too). A job link, not a data link.
    dependent_project_ids: list[UUID] = Field(default_factory=list, max_length=200)
    # Upstream projects: this one fails (is not started) when any of them fails. The other end of the same links: "A upstream of B" is
    # stored exactly like "B a dependent of A".
    upstream_project_ids: list[UUID] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _split_project_path(self) -> "ProjectRegister":
        assert_clean_url(self.git_url)                       # DBT-009: reject user info at the boundary, before anything is stored
        self.git_url, from_url = split_git_url(self.git_url)
        explicit = normalize_project_path(self.project_path)
        if explicit and from_url and explicit != from_url:
            raise ValueError("project_path conflicts with the #subdirectory in git_url")
        self.project_path = explicit or from_url
        suffix = _JOB_FILE_SUFFIX.get(self.kind)
        path = (self.project_path or "").lower()
        if suffix and not path.endswith(suffix):
            example = "jobs/daily_totals.sql" if self.kind == "sql" else "jobs/pipeline.py"
            raise ValueError(f"a {'SQL' if self.kind == 'sql' else 'Python'} job needs the path of a {suffix} file in the repository (e.g. {example})")
        if self.kind == "dbt" and path.endswith(tuple(_JOB_FILE_SUFFIX.values())):
            raise ValueError(f"project_path points at a {path.rsplit('.', 1)[1]} file: register it as a SQL or Python job (kind 'sql' / 'python')")
        if self.connection_id is not None and self.kind != "sql":
            raise ValueError("connection_id is only for SQL jobs")
        return self


class LastRunOut(BaseModel):
    """The most recent run of a project, embedded in the project list.

    Enough to answer "did it work, and when" without opening the project. The
    alternative — the list page fetching /runs per row — is one request per
    project on every render, and still leaves the row blank until they land.
    """
    id: UUID
    status: str
    trigger: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    models_total: int | None
    models_failed: int | None
    error_message: str | None


class ProjectOut(ORMModel):
    id: UUID
    org_id: str
    workspace_id: UUID | None
    name: str
    kind: JobKind = "dbt"
    connection_id: UUID | None = None
    git_url: str
    credential_ref: str | None = None
    git_ref: str
    project_path: str = ""
    engine: Engine
    schedule_cron: str | None
    schedule_enabled: bool
    git_trigger_enabled: bool = False
    last_synced_sha: str | None
    created_at: datetime
    updated_at: datetime
    # None means the project has never been built, which the UI must show as
    # "never run" rather than as an empty cell that reads like a load failure.
    last_run: LastRunOut | None = None
    # Dependents: the projects that fail when this one fails (direct links; the chain below them follows their own links).
    dependent_project_ids: list[UUID] = Field(default_factory=list)
    # The projects whose failure fails this one (direct links).
    upstream_project_ids: list[UUID] = Field(default_factory=list)
    # Projects whose data this one reads, from the lineage of their latest successful runs (app/services/data_dependencies.py).
    data_upstream_project_ids: list[UUID] = Field(default_factory=list)

    @field_validator("git_url")
    @classmethod
    def _never_return_a_credential(cls, v: str) -> str:
        """Defence in depth (DBT-009): a row written before credentials were refused is never echoed with its user info."""
        from app.services.run_manifest import redact_url
        return redact_url(v)


class ProjectHeadOut(BaseModel):
    """GET /internal/projects/{id}/head — the commit the project's ref points to RIGHT NOW (DG-009)."""
    project_id: UUID
    git_ref: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ProjectUpdate(BaseModel):
    git_ref: str | None = None
    # Names the credential used to clone (DBT-009); "" clears it. Never a token.
    credential_ref: str | None = Field(default=None, pattern=r"^([a-z][a-z0-9_]{0,62})?$")
    schedule_cron: str | None = None
    schedule_enabled: bool | None = None
    git_trigger_enabled: bool | None = None
    # Replaces the project's Dependents; [] removes them all, omitted leaves them as they are.
    dependent_project_ids: list[UUID] | None = Field(default=None, max_length=200)
    # Replaces the projects above this one the same way; [] removes them all, omitted leaves them as they are.
    upstream_project_ids: list[UUID] | None = Field(default=None, max_length=200)


class RunTriggerRequest(BaseModel):
    """POST /api/v1/projects/{id}/run — manual trigger."""
    select: str | None = Field(default=None, description="dbt --select expression; full build if omitted")
    # DG-007: only the scheduler's internal route accepts this (Dagster's git sensor pins the commit it saw); the
    # user-facing route refuses it. A full 40-character commit SHA; the run builds exactly that commit.
    git_sha: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$",
                                description="full commit SHA to build; the project's ref at submission if omitted")
    # DG-006: only the scheduler's internal route accepts this. The cron instant (a timezone-aware time, taken as UTC)
    # Dagster is firing: (project, instant) is claimed in scheduled_occurrences together with the run, and a second
    # request for the same instant returns the first run instead of starting another.
    scheduled_fire_time: AwareDatetime | None = Field(
        default=None, description="the cron occurrence this run is for; the same instant never starts a second run")


class RunCancelByKey(BaseModel):
    """POST /internal/projects/{id}/runs/cancel — the orchestrator cancels the run it submitted under this key."""
    idempotency_key: str = Field(min_length=1, max_length=255)


class RunOut(ORMModel):
    id: UUID
    project_id: UUID
    trigger: RunTrigger
    git_sha: str | None
    select: str | None
    status: RunStatus
    models_total: int | None
    models_succeeded: int | None
    models_failed: int | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class RunManifestOut(ORMModel):
    """What a run was frozen to at submission (DBT-006/007): the Git commit and the DQ artifact versions, hashes and
    rules it executed. Never contains a Git credential."""
    id: UUID
    git_sha: str | None
    manifest_sha256: str | None
    manifest: dict | None
    # Evidence recorded when the build finished (X-004): Git's tree id of the project folder that was built.
    source_tree_sha: str | None = None
    idempotency_key: str | None = None


class RunEventOut(ORMModel):
    id: UUID
    run_id: UUID
    event_type: str
    node_id: str | None
    payload: dict
    created_at: datetime


class LineageEdgeOut(ORMModel):
    source_table: str
    target_table: str
    relationship_type: str


class ProjectLineageOut(BaseModel):
    project_id: UUID
    edges: list[LineageEdgeOut]


# ---------- Validate-only (used by Workspace's codegen chat, not by users) ----------
class ValidateRequest(BaseModel):
    """Parse + compile + dry-run a single candidate dbt model without
    materializing anything or touching a registered project. Called by
    Workspace's codegen service during its generate -> validate -> fix
    loop — see app/services/codegen.py in workspace-service."""
    path: str
    sql: str
    schema_yml: str | None = None
    engine: Engine = "trino"


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    compiled_sql: str | None = None


# ---------- DQ Service -> dbt schema.yml injection (internal token only) ----------
class SchemaTestsPayload(BaseModel):
    """DQ-rendered schema.yml for one model, pushed by inaiera-data-quality.

    Preferred shape — ApplyDqArtifact (DQ renderer 2.x):
      artifact_version_id, binding_id, project_id, org_id, target_model,
      dbt_schema_fragment, idempotency_key, [content_hash, renderer_version,
      engine_requirements]
    Legacy shape (still accepted): target_table + yaml_content.

    `engine_requirements` (e.g. {"engine": "dbt-core", "min": "1.5.0",
    "max_exclusive": "2.0.0"}) is checked against this runner's installed
    dbt-core; an incompatible artifact is refused with 422 ENGINE_INCOMPATIBLE
    instead of breaking the next build.
    """
    artifact_version_id: str | None = Field(default=None, max_length=64)
    binding_id: str | None = Field(default=None, max_length=64)
    project_id: UUID | None = None
    org_id: str | None = Field(default=None, max_length=255)
    target_model: str | None = Field(default=None, max_length=500)
    dbt_schema_fragment: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, max_length=64)
    renderer_version: str | None = Field(default=None, max_length=32)
    engine_requirements: dict[str, Any] | None = None
    # legacy
    target_table: str | None = None
    yaml_content: str | None = Field(default=None,
                                     description="Full dbt schema.yml text (legacy field name)")

    def shape_problem(self) -> str | None:
        """Checked by the route (returned as a 422 detail)."""
        if self.dbt_schema_fragment is None and self.yaml_content is None:
            return "dbt_schema_fragment (or legacy yaml_content) is required"
        if not (self.target_model or self.target_table):
            return "target_model (or legacy target_table) is required"
        if self.dbt_schema_fragment is not None and not (
                self.artifact_version_id and self.idempotency_key and self.project_id
                and self.org_id):
            return ("ApplyDqArtifact requires artifact_version_id, project_id, org_id and "
                    "idempotency_key")
        return None

    @property
    def model_key(self) -> str:
        return self.target_model or self.target_table or ""

    @property
    def yaml_text(self) -> str:
        return self.dbt_schema_fragment if self.dbt_schema_fragment is not None \
            else (self.yaml_content or "")
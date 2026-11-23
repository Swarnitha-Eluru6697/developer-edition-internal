"""Configuration. All settings driven from environment variables — same
pattern as workspace-service's app/core/config.py.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

# Audiences that are another service's (or the generic Workspace one): never this runner's.
FORBIDDEN_AUDIENCES = frozenset({"workspace.inaiera", "flinksql-runner", "dq", "ucs.inaiera", "datasets.inaiera"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity ---
    service_name: str = "dbtrunner"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    org_id: str = Field(
        default="local-dev",
        description="Customer org UUID — set per data plane at provision time",
    )

    # --- Caller authentication (X-001) ---
    # Two ways in to the user-facing routes, each with its OWN audience so a credential minted for
    # one trust domain means nothing in another:
    #   1. External CI: a control-plane service-principal JWT (RS256, verified against the JWKS)
    #      whose audience is THIS service ("dbtrunner") and which carries the route's scope.
    #   2. Workspace: a short-lived delegated JWT (HS256, see delegation_jwt_secret below).
    # A token minted for Workspace itself (aud "workspace.inaiera") is not accepted here.
    control_plane_jwks_url: str = "https://www.app.inaiera.com/.well-known/jwks.json"
    control_plane_issuer: str = "https://www.app.inaiera.com"
    control_plane_audience: str = "dbtrunner"
    # DBT-002: the longest an externally issued (service-principal / CI, RS256 via JWKS) token may live: exp - iat. Interactive
    # sessions are not what calls this service — Workspace uses its own short delegated token. 0 disables the cap (not advised).
    external_jwt_max_lifetime_seconds: int = 3600
    jwks_cache_ttl_seconds: int = 3600
    # Static shared token. Accepted ONLY by the narrow, unscoped POST /api/v1/validate route (Workspace's
    # codegen loop). It is never a signing key, is explicitly rejected on every scoped route (RUN-DBT-005),
    # and no longer opens the /internal scheduler routes (DBT-003: Dagster has its own SCHEDULER_JWT_SECRET).
    internal_service_token: str | None = None

    # --- Scoped service JWTs (RUN-DBT-005) ---
    # HMAC key for short-lived, scoped service tokens presented TO this
    # service (e.g. by inaiera-data-quality on /schema-tests). Must differ
    # from internal_service_token: a holder of the raw legacy token must not
    # be able to mint scoped tokens.
    service_jwt_secret: str | None = None
    service_jwt_allowed_issuers: list[str] = ["dataquality"]
    service_jwt_allowed_subjects: list[str] = ["dataquality"]
    service_jwt_max_lifetime_seconds: int = 300
    service_jwt_clock_skew_seconds: int = 30

    # --- Scheduler identity (DBT-003) ---
    # HMAC key the Dagster orchestrator signs its per-request JWT with for the /internal routes (project
    # enumeration, run trigger, run read). Its own key: it must differ from every other service secret, so
    # none of them (and not the static internal token) can speak as the scheduler. Unset = the /internal
    # routes refuse everything. Tokens: iss/sub in scheduler_jwt_allowed_issuers, aud "dbtrunner", one scope.
    scheduler_jwt_secret: str | None = None
    scheduler_jwt_allowed_issuers: list[str] = ["dagster"]

    # --- Delegated Workspace tokens (X-001) ---
    # HMAC key Workspace signs its per-call delegated JWT with (aud "dbtrunner", exactly one scope,
    # sub = the end user, act.sub = "workspace", lifetime <= delegation_jwt_max_lifetime_seconds).
    # A key of its own: it must differ from service_jwt_secret (DQ's) and internal_service_token, so
    # holding either of those never lets a caller speak as a user.
    delegation_jwt_secret: str | None = None
    delegation_jwt_allowed_issuers: list[str] = ["workspace"]
    # X-001: the Data Quality service also acts on a user's behalf here (it looks a project up when a rule is bound to it),
    # signing with a key of ITS OWN. DQ is an accepted issuer only while this is set, and only for the scopes below:
    # it cannot start runs, register or edit projects.
    dq_delegation_jwt_secret: str | None = None
    dq_delegation_issuer: str = "dataquality"
    dq_delegation_allowed_scopes: list[str] = ["dbt.projects.read"]
    delegation_jwt_max_lifetime_seconds: int = 300
    delegation_jwt_clock_skew_seconds: int = 30

    # --- Metadata database ---
    # Either a full DATABASE_URL, or the discrete fields below (what Helm
    # sets — the password comes from a Secret and is URL-escaped here rather
    # than spliced into a string with $(VAR) expansion).
    database_url: str | None = None
    database_host: str | None = None
    database_port: int = 5432
    database_name: str = "dbtrunner"
    database_user: str = "dbtrunner"
    database_password: str | None = None
    # asyncpg's `ssl` connect argument: disable | prefer | require |
    # verify-ca | verify-full. (libpq's `?sslmode=` is NOT understood by
    # asyncpg and is translated/stripped — see database_connect_args().)
    database_ssl: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] | None = None
    database_pool_size: int = 10

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/1"

    # --- Git working copies ---
    git_workdir: str = "/var/lib/dbtrunner/git"
    # DBT-006: submitting a run resolves the project's Git ref to a commit with `git ls-remote`; give up (and refuse
    # the submission) after this long so a dead Git host cannot hang the request or the scheduler.
    git_resolve_timeout_seconds: int = 30
    git_clone_timeout_seconds: int = 120
    # Check at registration that the repo is readable and the named folder holds
    # a dbt_project.yml (a wrong project name is rejected up front, not on the
    # first run). Turn off only where the repo is not reachable at register time.
    validate_project_on_register: bool = True

    # --- Engines ---
    trino_host: str | None = None
    trino_port: int = 443
    trino_catalog: str = "iceberg"
    trino_http_scheme: Literal["http", "https"] = "https"
    # Secret holding the warehouse JWT dbt-trino authenticates with. The Job
    # pod gets it via secretKeyRef (never a plaintext value in the Job spec).
    trino_jwt_secret_name: str | None = None
    trino_jwt_secret_key: str = "token"
    # How dbt / SQL / Python jobs authenticate to Trino. "jwt" (the default) sends the warehouse JWT; "none" sends only a user name, for
    # a local Trino with no authentication, and is refused outside ENVIRONMENT=dev.
    trino_auth_method: Literal["jwt", "none"] = "jwt"
    trino_user: str = "inaiera"
    pg_lake_dsn: str | None = None
    # ClickHouse (engine "clickhouse"): the HTTP(S) interface that dbt-clickhouse, SQL jobs and Python jobs use (8123 plain, 8443 TLS).
    # `clickhouse_database` is where a SQL job's statements run when they name no database; dbt's schema is a ClickHouse database.
    # The password is read from the process env (CLICKHOUSE_PASSWORD); a run Job gets it through a Secret reference
    # (clickhouse_password_secret_name), never as a plain value outside dev.
    clickhouse_host: str | None = None
    clickhouse_port: int = 8123
    clickhouse_secure: bool = False
    clickhouse_user: str = "default"
    clickhouse_password: str | None = None
    clickhouse_database: str = "default"
    clickhouse_password_secret_name: str | None = None
    clickhouse_password_secret_key: str = "password"
    # SQL jobs (app/executors/sql_job_executor.py): longest a single statement may run before it is cancelled.
    sql_job_statement_timeout_seconds: int = Field(default=3600, ge=1)
    # Python jobs (app/executors/python_job_executor.py): how long installing requirements.txt and running the script may each take,
    # the longest output kept per step, and the package index pip uses (a mirror or proxy when the cluster has no direct egress).
    python_job_install_timeout_seconds: int = Field(default=900, ge=1)
    python_job_timeout_seconds: int = Field(default=3600, ge=1)
    python_job_output_max_chars: int = Field(default=20000, ge=1000)
    pip_index_url: str | None = None
    # SQL jobs on a Workspace connection (app/executors/connection_sql_job.py): the runner never sees the connection's credentials.
    # It sends the job's statements to the Workspace, which runs them as the user who registered the job. The token authorising
    # ONE run is signed with this secret (Workspace trusts it as the dbt runner's, nothing else).
    workspace_url: str | None = None
    workspace_job_jwt_secret: str | None = None
    # Name/key of the k8s Secret this DSN was itself sourced from (see
    # values.yaml engines.pgLake.dsnSecretRef). The k8s Job runner references
    # the secret by name instead of copying the plaintext value into a Job
    # spec — and refuses to create the Job without it outside dev.
    pg_lake_dsn_secret_name: str | None = None
    pg_lake_dsn_secret_key: str | None = None

    # --- Apache Polaris (Iceberg REST catalog) ---
    polaris_rest_uri: str = "http://polaris.dataplane.svc:8181/api/catalog"
    polaris_client_id: str | None = None
    polaris_client_secret: str | None = None

    # --- Runner isolation ---
    # The Job runs the SAME image as the API (docker/Dockerfile) with
    # `python -m app.executors.job_entrypoint` as its command; a dedicated,
    # slimmer image can be built from the Dockerfile's `job` target.
    runner_image: str = "registry.inaiera.com/products/dbt-runner:0.1.0"
    runner_namespace: str = "inaiera"
    runner_job_timeout_seconds: int = 3600
    # How long a finished Job (and its Pods) may linger before Kubernetes
    # garbage-collects it. The run's result is durable in the artifact store
    # independent of this, so a GC'd Job never causes a re-run.
    runner_job_ttl_seconds: int = 3600
    runner_job_cpu_request: str = "250m"
    runner_job_cpu_limit: str = "2"
    runner_job_memory_request: str = "512Mi"
    runner_job_memory_limit: str = "2Gi"
    runner_job_service_account: str | None = None
    runner_job_run_as_user: int = 10001
    # Egress proxy Job pods send git/dbt-deps traffic through (DBT-010); empty = direct (only with an explicit NetworkPolicy destination).
    job_git_proxy_url: str = ""
    runner_job_run_as_group: int = 10001
    # Extra labels stamped on Job pods (Helm sets the release instance label
    # here so the Job NetworkPolicy selects exactly this release's pods).
    runner_job_pod_labels: dict[str, str] = {}
    runner_mode: Literal["k8s_job", "subprocess"] = "k8s_job"
    # Bounded thread pool for the (synchronous) kubernetes client. Each k8s
    # API call borrows a thread only for that call; polling sleeps on the
    # event loop, so in-flight runs do not pin threads.
    runner_k8s_client_threads: int = 8
    # Size cap for the inline (log-line) result fallback, used only when the
    # artifact store is unavailable.
    runner_inline_result_max_bytes: int = 256 * 1024

    # --- Durable run artifacts (manifest / run_results / result) ---
    artifact_store_backend: Literal["none", "local", "s3"] = "local"
    artifact_store_local_dir: str = "/var/lib/dbtrunner/artifacts"
    artifact_s3_bucket: str | None = None
    artifact_s3_prefix: str = "dbtrunner"
    artifact_s3_endpoint_url: str | None = None
    artifact_s3_region: str | None = None
    # Optional static credentials Secret for the Job pod (IRSA / workload
    # identity on the Job ServiceAccount is the alternative).
    artifact_s3_credentials_secret_name: str | None = None
    artifact_s3_access_key_id_key: str = "access-key-id"
    artifact_s3_secret_access_key_key: str = "secret-access-key"

    # --- Datasets product ---
    datasets_url: str | None = "http://datasets.dataplane.svc:8083"
    # Signs the short-lived datasets.register.write token sent with every
    # dataset-registration call (RUN-DBT-007). Unset = sink disabled.
    datasets_internal_token: str | None = None

    # --- Scheduling ---
    scheduler_poll_interval_seconds: int = 30

    # --- Run reconciliation / leases (RUN-DBT-003) ---
    run_reconciler_poll_interval_seconds: int = 15
    # How often the leader looks for newly queued runs when no wake-up arrives (DBT-011): the NOTIFY from submit is the fast path, this is the net.
    run_queue_poll_seconds: int = 2
    # A claimed run is owned by exactly one process, recorded as a lease on
    # the row and renewed every run_lease_seconds/3 while that process is
    # alive. Only an EXPIRED lease may be taken over by another replica.
    run_lease_seconds: int = 60
    # Max dispatches started concurrently by one process's queued sweep.
    run_dispatch_concurrency: int = 10
    # DBT-005: a run stays 'cancelling' until its Job is confirmed gone. If that takes longer than this, it is
    # stuck: an error is logged, `run_cancel_stuck` is counted and /readyz reports run_cancellation_stuck.
    run_cancel_stuck_seconds: int = 300
    # Leader election: one process (across replicas AND uvicorn workers)
    # runs the scheduler / reconciler / outbox loops.
    leader_election_retry_seconds: int = 10
    # SCHEDULER_ENABLED=false hands cron scheduling to Dagster: the built-in
    # scheduler_loop is left out, while the reconciler and outbox keep running.
    scheduler_enabled: bool = True

    # --- Metadata outbox (RUN-DBT-008) ---
    metadata_outbox_poll_interval_seconds: int = 15
    metadata_outbox_initial_backoff_seconds: int = 30
    metadata_outbox_max_backoff_seconds: int = 3600
    metadata_outbox_max_attempts: int = 20
    # readiness reports "degraded" (still 200) above these
    metadata_outbox_backlog_warn: int = 500

    # --- Limits ---
    max_projects_per_org: int = 500
    max_run_history_per_project: int = 200

    # --- Telemetry ---
    telemetry_endpoint: str = "https://telemetry.inaiera.com/v1/events"
    telemetry_enabled: bool = True

    # --- OpenLineage -> UCS ---
    openlineage_url: str | None = None
    openlineage_namespace: str = "inaiera.dbt"
    # Shared secret this service uses to MINT a short-lived
    # ucs.lineage.write-scoped JWT for each lineage publish call.
    ucs_internal_token: str | None = None

    # --- DQ Service integration ---
    dq_service_url: str | None = None
    dq_schema_tests_filename: str = "models/_dq_generated_tests.yml"

    # --- CORS ---
    cors_origins: list[str] = ["https://www.app.inaiera.com"]

    @model_validator(mode="after")
    def _own_audience_only(self) -> "Settings":
        """DBT-002: the audience is THIS service's — never Workspace's generic one (a token minted for Workspace, or a browser
        session token, must mean nothing here) and never a sibling runner's."""
        aud = (self.control_plane_audience or "").strip()
        if not aud or aud in FORBIDDEN_AUDIENCES:
            raise ValueError(
                f"CONTROL_PLANE_AUDIENCE must be this service's own audience (\"dbtrunner\"), never empty or one of "
                f"{sorted(FORBIDDEN_AUDIENCES)} (got {self.control_plane_audience!r}): tokens meant for another service must not be accepted here")
        return self

    @model_validator(mode="after")
    def _trino_without_auth_is_dev_only(self) -> "Settings":
        if self.trino_auth_method == "none" and self.environment != "dev":
            raise ValueError(f"TRINO_AUTH_METHOD=none is for a local development Trino only; ENVIRONMENT={self.environment} needs jwt")
        return self

    @model_validator(mode="after")
    def _distinct_service_secrets(self) -> "Settings":
        if (self.service_jwt_secret and self.internal_service_token
                and self.service_jwt_secret == self.internal_service_token):
            raise ValueError(
                "SERVICE_JWT_SECRET must differ from INTERNAL_SERVICE_TOKEN — the raw "
                "legacy token must not be usable as a key to sign scoped service tokens"
            )
        if self.delegation_jwt_secret and self.delegation_jwt_secret in (
                self.service_jwt_secret, self.internal_service_token):
            raise ValueError(
                "DELEGATION_JWT_SECRET must differ from SERVICE_JWT_SECRET and INTERNAL_SERVICE_TOKEN — "
                "holders of those must not be able to sign tokens that act as a user"
            )
        if self.dq_delegation_jwt_secret and self.dq_delegation_jwt_secret in (
                self.service_jwt_secret, self.internal_service_token, self.delegation_jwt_secret,
                self.scheduler_jwt_secret):
            raise ValueError(
                "DQ_DELEGATION_JWT_SECRET must differ from SERVICE_JWT_SECRET, DELEGATION_JWT_SECRET, SCHEDULER_JWT_SECRET "
                "and INTERNAL_SERVICE_TOKEN — a key that signs a user must not be shared with any other trust domain"
            )
        if self.scheduler_jwt_secret and self.scheduler_jwt_secret in (
                self.service_jwt_secret, self.internal_service_token, self.delegation_jwt_secret):
            raise ValueError(
                "SCHEDULER_JWT_SECRET must differ from SERVICE_JWT_SECRET, DELEGATION_JWT_SECRET and "
                "INTERNAL_SERVICE_TOKEN — holders of those must not be able to sign scheduler tokens"
            )
        if self.workspace_job_jwt_secret and self.workspace_job_jwt_secret in (
                self.service_jwt_secret, self.internal_service_token, self.delegation_jwt_secret,
                self.scheduler_jwt_secret, self.dq_delegation_jwt_secret):
            raise ValueError(
                "WORKSPACE_JOB_JWT_SECRET must differ from every other signing secret of this service — it lets a run call the "
                "Workspace on a connection, and nothing else may be able to sign that token"
            )
        return self

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def effective_database_url(self) -> str:
        """The SQLAlchemy URL to connect with. Built from discrete fields
        with proper escaping when DATABASE_HOST is set; libpq-only query
        params (`sslmode`) are stripped because asyncpg rejects them — the
        equivalent goes into connect_args instead."""
        if self.database_host:
            url = URL.create(
                "postgresql+asyncpg",
                username=self.database_user,
                password=self.database_password,
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
            )
            return url.render_as_string(hide_password=False)
        raw = self.database_url or "postgresql+asyncpg://dbtrunner:dbtrunner@localhost:5432/dbtrunner"
        url = make_url(raw)
        if url.drivername.endswith("asyncpg") and "sslmode" in url.query:
            url = url.difference_update_query(["sslmode"])
        return url.render_as_string(hide_password=False)

    def database_connect_args(self) -> dict:
        url = make_url(self.database_url) if self.database_url and not self.database_host else None
        if url is not None and not url.drivername.endswith("asyncpg"):
            return {}
        ssl = self.database_ssl
        if ssl is None and url is not None:
            ssl = url.query.get("sslmode") or url.query.get("ssl")
            if isinstance(ssl, tuple):
                ssl = ssl[0]
        if not ssl or ssl == "disable":
            return {}
        return {"ssl": ssl}

    @property
    def openlineage_sink_enabled(self) -> bool:
        return bool(self.openlineage_url and self.ucs_internal_token)

    @property
    def datasets_sink_enabled(self) -> bool:
        return bool(self.datasets_url and self.datasets_internal_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()

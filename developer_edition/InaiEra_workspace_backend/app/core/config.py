"""Configuration. All settings driven from environment variables."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity ---
    service_name: str = "workspace"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    org_id: str = Field(
        default="local-dev",
        description="Customer org UUID — set per data plane at provision time",
    )

    # --- Canonical user identity (W-020, X-003) ---
    # The gateway must inject the token's immutable `sub` as x-user-id; it becomes Principal.user_id everywhere (Workspace
    # ownership, the runners' and DQ's audit, the delegated tokens). None = required outside dev, optional in dev, where a
    # missing header falls back to the username (legacy) with a warning so an older local gateway keeps working.
    require_user_subject: bool | None = None

    # --- Governance profile (W-010) ---
    # production-governed (default): UCS is the governance spine and must be configured; outside dev the app refuses to start
    # without it. development-ungoverned: UCS may be absent; unsupported for production (the chart labels it so and requires an
    # explicit acknowledgement). See docker/helm/workspace/values.yaml `governance`.
    governance_profile: Literal["production-governed", "development-ungoverned"] = "production-governed"

    # --- Connection use: owner bypass (W-011) ---
    # May the person who created a connection use it WITHOUT a UCS decision? That is a policy, stated here rather than left implicit.
    # None = decided by the governance profile: allowed in dev and in the development-ungoverned profile (so a deployment without UCS can
    # still query its own connections), NOT allowed in production-governed, where UCS decides every use of every connection, the
    # creator's included. Every bypass is logged (connection_use_owner_bypass).
    connection_owner_bypass: bool | None = None

    # --- Resource/data policy gate (X-006) ---
    # Workspace RBAC is the coarse product gate; UCS is the resource/data policy gate for execution and deployment actions
    # (dbt.run, flink.deploy, dq.publish, git.repo.write, ...). off = no UCS call; audit = ask UCS and record the decision but
    # never block (a would-be denial is logged and audited); enforce = a UCS denial is a 403 and an unreachable/unconfigured UCS
    # is a 503 (fail closed). `data_policy_enforced_actions` narrows enforce to those product actions (empty = all of them);
    # the rest stay in audit. UCS answers only on its own grants, so turn enforce on once governance has granted the
    # objects below (docs/DATA_POLICY.md).
    data_policy_mode: Literal["off", "audit", "enforce"] = "audit"
    data_policy_enforced_actions: list[str] = Field(default_factory=list)
    data_policy_catalog: str = "inaiera_workspace"   # catalog name of the pseudo objects for resources UCS has no object for
    data_policy_timeout_seconds: float = 3.0

    # --- Services behind Workspace (X-007) ---
    # Connecting must succeed quickly (a dead service is noticed in seconds); reading is bounded. Failures are 502 / 504 (app/core/upstream.py).
    upstream_connect_timeout_seconds: float = 5.0
    upstream_timeout_seconds: float = 30.0

    # --- Control plane handoff ---
    control_plane_jwks_url: str = "https://www.app.inaiera.com/.well-known/jwks.json"
    control_plane_issuer: str = "https://www.app.inaiera.com"
    control_plane_audience: str = "workspace.inaiera"
    jwks_cache_ttl_seconds: int = 3600
    # Where the SPA sends a user to log in again (W-006), served at GET /config.json. Unset = derived from the issuer, so a staging /
    # private-cloud / white-label control plane needs no extra setting. Must be an absolute http(s) URL.
    control_plane_login_url: str | None = None

    # --- Storage backend ---
    storage_backend: Literal["s3", "azure_blob", "gcs", "local"] = "s3"
    storage_bucket: str = "inaiera-workspace-files"
    storage_prefix: str = ""  # set to org_id at runtime
    storage_endpoint_url: str | None = None  # set for MinIO/local
    storage_region: str = "us-east-1"

    # --- Metadata database (per-data-plane Postgres) ---
    database_url: str = (
        "postgresql+asyncpg://workspace:workspace@localhost:5432/workspace"
    )
    # Injected separately from a Secret (Helm: database.passwordSecretRef) and
    # merged into database_url below, URL-encoded. Kubernetes can't do this
    # itself: $(VAR) isn't expanded in envFrom ConfigMap values, and a raw
    # password with @ / : # in it would corrupt the URL anyway.
    database_password: str | None = None
    database_pool_size: int = 10
    # Open a fresh connection per session instead of pooling. For processes
    # that run several event loops (test clients); never needed in serving.
    database_null_pool: bool = False

    # --- Redis (pub/sub for WebSocket fan-out) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Data Lake House (SQL execution target) ---
    lakehouse_query_url: str = "http://datalakehouse.dataplane.svc:8080"
    lakehouse_query_timeout_seconds: int = 300

    # --- ClickHouse (engine "clickhouse" of the code assistant) ---
    # The assistant reads table names and columns from ClickHouse's HTTP interface and checks a candidate with EXPLAIN (it never runs the
    # candidate and never reads rows). Empty URL = ClickHouse is not configured: a ClickHouse turn then says so. Jobs run on the dbt Runner,
    # which has its own ClickHouse settings.
    clickhouse_url: str | None = None
    clickhouse_user: str = "default"
    clickhouse_password: str | None = None
    clickhouse_database: str = "default"
    clickhouse_timeout_seconds: int = 30

    # --- JupyterHub (notebook execution target, Phase 2) ---
    jupyterhub_url: str = "http://jupyterhub.dataplane.svc:8081"
    jupyterhub_admin_token: str | None = None
    # When JupyterHub isn't configured, run Python cells in an in-process
    # IPython kernel (per session, state persists across cells). Dev-only:
    # off unless explicitly enabled (docker-compose does), and refused at
    # startup outside dev (WS-RUNTIME-012) — user code would otherwise run
    # inside the worker with no resource or network isolation.
    local_python_kernel_enabled: bool = False

    # --- dbt Runner (deploy target) ---
    # ClusterIP service port is 80 → targetPort 8080 per helm/dbtrunner/values.yaml.
    # Override with DBTRUNNER_URL env var at deploy time if the helm release name differs.
    dbtrunner_url: str = "http://dbtrunner.dataplane.svc:80"
    # --- Dagster ---
    # Dagster's GraphQL endpoint (e.g. http://dagster-webserver:3000/graphql). When set, "Run now" for a dbt / SQL / Python job LAUNCHES
    # THE PROJECT'S DAGSTER JOB instead of calling the runner directly, so the dependency rule (a job runs only when the jobs above it have
    # a recent success, enforced in Dagster) applies to a run started by hand too. Empty keeps the direct call (a deployment without
    # Dagster has no such rule). Run now then needs Dagster to be up.
    dagster_graphql_url: str = ""
    # How long Run now waits for Dagster's submit step (it only hands the run to the runner) before answering 504.
    dagster_run_wait_seconds: float = 30.0
    # Narrow-scope token for the validate-only call from codegen — separate
    # from the deploy-time JWT forward so a leaked token can't register or
    # run a project. See inaiera-dbt-runner/app/core/deps.py.
    dbtrunner_internal_token: str | None = None
    # X-001: HMAC key Workspace signs its short-lived DELEGATED token for the dbt Runner with
    # (aud "dbtrunner", one scope, sub = the gateway-authenticated user, act.sub = "workspace").
    # Replaces forwarding the user's own bearer. Must equal the runner's DELEGATION_JWT_SECRET and
    # differ from dbtrunner_internal_token. Unset = every dbt call is refused (503), never forwarded raw.
    dbtrunner_delegation_jwt_secret: str | None = None

    # SQL jobs on a Workspace connection: the dbt Runner sends a job's statements here (POST /api/v1/internal/jobs/sql) and this
    # service runs them on the connection, as the user who registered the job — the connection's credentials never leave it.
    # The runner proves one run's call with a token signed by this HMAC key (iss "dbtrunner", aud "workspace", scope "sql.jobs.run",
    # single-use); it must equal the runner's WORKSPACE_JOB_JWT_SECRET and differ from every other secret. Unset = refused (503).
    runner_job_jwt_secret: str | None = None

    # --- FlinkSQL Runner (codegen validate target) ---
    # Workspace codegen calls POST /api/v1/validate to syntax-check FlinkSQL via
    # the Flink SQL Gateway EXPLAIN path. Internal-token auth only (no user JWT).
    flinksql_runner_url: str = "http://flinksql-runner.dataplane.svc:80"
    flinksql_internal_token: str | None = None
    # X-001: same for the FlinkSQL Runner (aud "flinksql-runner"); the runner's DELEGATION_JWT_SECRET.
    flinksql_delegation_jwt_secret: str | None = None

    # --- Data Quality Service ---
    # Rule registry, rulesets, bindings, and the rendering engine. Results flow
    # through UCS (POST /quality/results) for scoring and alerting.
    dq_service_url: str = "http://dataquality.dataplane.svc:80"
    # Narrow-scope token for /render/validate/rule, called by the codegen chat.
    # Attached server-side by dq_proxy — never forwarded from the browser.
    dq_internal_token: str | None = None
    # Signs the scoped JWT for DQ's inbound validate route (aud "dataquality",
    # scope "dq.rules.validate") — must equal DQ's INBOUND_SERVICE_JWT_SECRET.
    # The raw dq_internal_token above is only a dev fallback for older DQ images.
    dq_service_jwt_secret: str | None = None
    # X-001: HMAC key Workspace signs its short-lived DELEGATED token for the DQ service with (aud "dataquality", the
    # DQ scope(s) of the call, sub = the gateway-authenticated user, act.sub = "workspace"). Replaces forwarding the user's
    # own bearer, which DQ no longer accepts. Must equal DQ's DELEGATION_JWT_SECRET and differ from every other secret.
    # Unset = every user-facing DQ call is refused (503), never forwarded raw.
    dq_delegation_jwt_secret: str | None = None

    # --- Git provider egress allowlist (WS-GIT-009) ---
    # Only https:// clone/OAuth URLs to one of these hosts (or a subdomain of
    # one) are permitted — see app/core/git_egress_policy.py. Add a
    # self-hosted GitLab/Bitbucket/Azure DevOps host here explicitly; it is
    # never inferred from what a caller supplies.
    git_allowed_hosts: list[str] = [
        "github.com", "gitlab.com", "bitbucket.org", "dev.azure.com", "visualstudio.com",
    ]
    # Self-hosted GitLab hosts a binding with provider="gitlab" may point at,
    # in addition to gitlab.com (per-provider host binding, WS-GIT-009). Each
    # must ALSO be on git_allowed_hosts above.
    git_gitlab_hosts: list[str] = []

    # --- Database-connection egress (WS-009) ---
    # Where a registered connection (test, catalog browse, SQL) may point.
    # Private/internal addresses (RFC1918, CGNAT, ULA, …) are denied unless
    # covered here — e.g. the customer VPC's DB subnet. Loopback, link-local
    # (cloud metadata) and reserved ranges are always denied.
    connection_allowed_networks: list[str] = []   # CIDRs, e.g. ["10.20.0.0/16"]
    connection_allowed_hosts: list[str] = []      # exact names, or ".suffix" for subdomains
    # Public internet databases. Set false to make the lists above the only
    # permitted destinations.
    connection_allow_public: bool = True

    # --- Alerts (Slack / Teams channels, failure rules, the sweep) ---
    # The sweep (app/services/alerts) looks at the runners, Flink and Dagster this often and raises / clears alerts. 0 or alerts_sweep_enabled
    # false = no background sweep (an admin can still press "Run rules now").
    alerts_sweep_enabled: bool = True
    alerts_sweep_interval_seconds: int = 60
    alerts_webhook_timeout_seconds: float = 10.0
    # A webhook that keeps failing is tried this many times, then left as "failed".
    alerts_delivery_max_attempts: int = 5
    # What the sweep has seen of each job is kept this long.
    alerts_retention_days: int = 30
    # A channel webhook must be on its provider's own hosts (Slack: hooks.slack.com; Teams: office.com / logic.azure.com /
    # powerplatform.com). Hosts listed here are accepted too, e.g. a corporate relay or, in dev, a local receiver; "http://" is
    # accepted ONLY for these and only when ENVIRONMENT=dev.
    alerts_webhook_extra_hosts: list[str] = []
    # Acknowledge / Resolve buttons in the Slack / Teams message. Each button is a link to THIS service (alerts_public_url + a signed,
    # expiring token); clicking it opens a small confirmation page and then updates the alert. Both must be set for the buttons to
    # appear. alerts_public_url must be reachable by the people reading the channel (and the route /api/v1/alerts/actions/* must be
    # let through the gateway without a login: the signed token is the credential); the secret signs the tokens.
    alerts_public_url: str = ""
    alerts_action_secret: str | None = None
    alerts_action_link_ttl_seconds: int = 7 * 86_400
    # When an alert is acknowledged, resolved or clears by itself, post a short status message to the channels that received it: a card
    # posted through a webhook cannot be edited, so without this the channel keeps showing it as open.
    alerts_status_updates: bool = True

    # --- Unified Catalog Service (UCS) — governance control plane ---
    # Backs the connection.use authorization check in the SQL execution path
    # (app/services/ucs_client.py). None means UCS isn't wired up for this
    # deployment yet — that path fails closed rather than skipping the check.
    ucs_service_url: str | None = None
    # UCS sits behind the same gateway-auth boundary as this service: every call
    # must carry UCS's ENVOY_INTERNAL_SECRET as X-Inaiera-Gateway-Secret, or UCS
    # answers 401 "request did not come through the gateway".
    ucs_gateway_secret: str | None = None

    # Flink Web UI base URL as reached from the USER'S BROWSER — not the
    # in-cluster JobManager address the runner talks to. The SPA's "View logs"
    # action deep-links here. Empty disables that button.
    flink_ui_url: str = ""

    # --- Codegen chat (SQL / dbt / FlinkSQL / Python authoring assistant) ---
    # By default the codegen agent talks to SELF-HOSTED, in-perimeter models only, through
    # an OpenAI-compatible gateway (LiteLLM/vLLM): no external LLM providers, no provider API keys, no inference egress
    # (agent design doc §4/§11). The gateway is addressed with logical model names so a checkpoint swap is a gateway config
    # change, never an application change.
    # A holder of workspace:settings:manage may instead switch the agent to OpenAI or Anthropic with their own API key (Settings → Codegen agent;
    # app/services/codegen_provider.py). That sends prompts and the schema context OUT of the perimeter, so it is an admin-only,
    # audited choice; the open file's content additionally needs the org policy, the provider on the allowlist and a UCS decision
    # (codegen_governance.py treats these providers as external whatever codegen_gateway_in_perimeter says).
    # False removes the self-hosted gateway option altogether (Developer Edition ships no model gateway): Settings → Codegen
    # agent then offers only OpenAI and Anthropic with the admin's own key, and the assistant answers once a key is saved.
    codegen_gateway_enabled: bool = True
    codegen_gateway_url: str = "http://model-gateway.dataplane.svc:4000/v1"
    # Optional bearer token if the gateway enforces auth (in-cluster; not an
    # external provider key). Fetched by reference from the vault in prod.
    codegen_gateway_api_key: str | None = None
    # The external providers an admin may switch to. Models and endpoints are settings so a retired model id or an Azure/proxy
    # endpoint is a configuration change.
    codegen_openai_base_url: str = "https://api.openai.com/v1"
    codegen_openai_model: str = "gpt-4o"
    codegen_anthropic_base_url: str = "https://api.anthropic.com/v1"
    codegen_anthropic_model: str = "claude-sonnet-5-5"
    # True when the gateway (and every model behind it) runs inside the
    # customer's perimeter. Either way the user's opt-in, the org policy, the
    # provider allowlist and redaction/classification all apply
    # (codegen_governance.py); False additionally requires a per-request UCS
    # `codegen.send_external_context` decision, since the source then leaves
    # the customer boundary.
    codegen_gateway_in_perimeter: bool = True
    # The UCS object that decision is made on — the governed model entry the
    # gateway fronts, as catalog.schema.model (UCS grants USE_MODEL on it).
    # Required when the gateway is not in-perimeter; unset → context withheld.
    codegen_ucs_model_object: str | None = None

    # Logical model names resolved by the gateway (§4.1/§4.4):
    #   generation      — default generator (e.g. Qwen3-Coder-30B)
    #   generation_alt  — user-selectable second generator (e.g. Nemotron-Cascade-2)
    #   utility         — cheap stages: intent, error-normalisation, critic
    codegen_model_generation: str = "codegen-generation"
    codegen_model_generation_alt: str = "codegen-generation-alt"
    codegen_model_utility: str = "codegen-utility"
    # Which generation models the user may pick between in the UI. The default
    # is always first. Kept as logical names; the gateway maps to checkpoints.
    codegen_generation_models: list[str] = ["codegen-generation", "codegen-generation-alt"]

    # Harness budgets (§9.2). Bounded so a repair loop can never run away.
    codegen_max_agent_turns: int = 6          # total generate/repair attempts per turn
    codegen_max_attempts_per_stage: int = 3   # per-stage cap (ground/plan/generate)
    codegen_max_output_tokens: int = 4096
    codegen_request_timeout_seconds: int = 120
    # Sampling per stage (§4.2): planning favours structure, generation a little
    # diversity, repair a touch more to escape a stuck candidate.
    codegen_temp_plan: float = 0.2
    codegen_temp_generate: float = 0.6
    codegen_temp_repair: float = 0.8

    # FlinkSQL job authoring is Phase 4 (agent design §14) — backend is wired but
    # gated off until the catalog of Kafka topics/sources is available.
    codegen_flinksql_enabled: bool = False

    # --- Schema Registry (Kafka topic schemas for Python/Flink grounding, §5.3) ---
    # Confluent/Apicurio-compatible. Used to bind Kafka producers/consumers in
    # generated Python against registered Avro/Protobuf/JSON schemas (§7.5 L2).
    schema_registry_url: str | None = None
    schema_registry_auth: str | None = None  # "user:pass" if the registry needs basic auth

    # --- Secrets vault ---
    secrets_backend: Literal["aws_secrets_manager", "azure_key_vault", "gcp_secret_manager", "local"] = "local"
    secrets_path_prefix: str = "inaiera/workspace"
    # Required when secrets_backend == "azure_key_vault", e.g. https://my-vault.vault.azure.net
    secrets_azure_vault_url: str | None = None
    # Required when secrets_backend == "gcp_secret_manager"
    secrets_gcp_project_id: str | None = None

    # --- Limits ---
    max_file_size_bytes: int = 5 * 1024 * 1024  # 5MB per file
    max_files_per_workspace: int = 10_000
    max_query_rows_returned: int = 10_000
    query_chunk_size: int = 1000
    session_idle_timeout_minutes: int = 30
    websocket_max_per_user: int = 5

    # --- Lifecycle & cleanup (WS-LIFE-013) ---
    # How often the reconciliation loops (sessions/outputs/files, executions)
    # sweep for stale state. One shared interval keeps the loops in lockstep,
    # which makes their combined load on Postgres predictable.
    lifecycle_reconcile_interval_seconds: int = 300
    # How long a completed execution's output chunks are kept before the
    # reconciler deletes them — was a hardcoded local in execution_router.py
    # with nothing ever reading it back against "now".
    execution_output_retention_hours: int = 24
    # A soft-deleted file's blob is kept for this long (in case of an
    # accidental delete / pending git commit that still references it) before
    # the reconciler removes both the blob and its metadata row for good.
    file_soft_delete_retention_hours: int = 24
    # Extra margin added on top of an execution's own timeout_seconds before
    # the reconciler treats a still-"running" row as orphaned. Using the
    # row's own declared budget (not a blind "any running row") means the
    # reconciler never clobbers a sibling replica's execution that's still
    # legitimately within its allowed runtime.
    execution_stale_grace_seconds: int = 60
    # How long a codegen turn may sit with its assistant reply still
    # "running" (mid-agent-loop) before the reconciler treats the owning
    # process as having crashed mid-turn and marks it failed.
    codegen_turn_stale_seconds: int = 1800

    # --- Execution worker (WS-EXEC-005) ---
    # Whether THIS process runs the execution worker. True keeps the
    # single-process dev setup; the Helm chart sets it false on the API pods
    # and runs a separate `python -m app.worker` Deployment instead, so a
    # long query or cell never shares capacity with request handling.
    execution_worker_enabled: bool = True
    # Executions one worker process runs at once.
    execution_worker_concurrency: int = 4
    # Backstop poll for pending rows; submissions also wake the worker
    # immediately over Redis.
    execution_poll_interval_seconds: float = 2.0
    # A running execution's worker extends its lease ON THE EXECUTION ROW
    # (Postgres, not Redis: W-018) every third of this value; the claim sets
    # the first lease. A row whose lease lapsed belongs to a dead worker and is
    # recovered by the reconciler, not after its timeout. The same heartbeat
    # reads the user's cancel request, so a cancel is noticed within about this
    # long even if Redis is down (a Redis hint makes it faster).
    execution_lease_seconds: int = 30

    # --- Telemetry (outbound to control plane Kafka pipeline) ---
    telemetry_endpoint: str = "https://telemetry.inaiera.com/v1/events"
    telemetry_enabled: bool = True

    # --- CORS (browser → workspace) ---
    cors_origins: list[str] = ["https://www.app.inaiera.com"]


    @property
    def owner_bypass_allowed(self) -> bool:
        if self.connection_owner_bypass is not None:
            return self.connection_owner_bypass
        return self.environment == "dev" or self.governance_profile == "development-ungoverned"

    @property
    def resolved_control_plane_login_url(self) -> str:
        return self.control_plane_login_url or f"{self.control_plane_issuer.rstrip('/')}/login"

    @field_validator("control_plane_login_url")
    @classmethod
    def _login_url_is_absolute_http(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        from urllib.parse import urlparse
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("CONTROL_PLANE_LOGIN_URL must be an absolute http(s) URL")
        return v

    @property
    def user_subject_required(self) -> bool:
        """Whether a request without the gateway's x-user-id is refused (always outside dev unless explicitly relaxed)."""
        if self.require_user_subject is not None:
            return self.require_user_subject
        return self.environment != "dev"

    @model_validator(mode="after")
    def _runner_job_secret_is_its_own(self) -> "Settings":
        if self.runner_job_jwt_secret and self.runner_job_jwt_secret in (
                self.dbtrunner_delegation_jwt_secret, self.flinksql_delegation_jwt_secret, self.dq_delegation_jwt_secret,
                self.dq_service_jwt_secret, self.dbtrunner_internal_token, self.flinksql_internal_token, self.dq_internal_token):
            raise ValueError("RUNNER_JOB_JWT_SECRET must differ from every other service secret: it authorises running SQL on a connection")
        return self

    @model_validator(mode="after")
    def _merge_database_password(self) -> "Settings":
        if self.database_password:
            from sqlalchemy.engine import make_url
            self.database_url = (
                make_url(self.database_url)
                .set(password=self.database_password)
                .render_as_string(hide_password=False)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

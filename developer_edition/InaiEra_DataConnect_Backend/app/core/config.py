"""
Application configuration via pydantic-settings.

All values are read from environment variables (prefix DATACONNECT_)
or from a .env file at the project root.

New additions vs original:
- app_version, env fields
- secret_backend (env / postgres) toggle
- JWT settings for future RBAC
- Redis URL for future caching / task queue
- Kafka REST Proxy URL (separate from Connect)
- Configurable log level
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Application ───────────────────────────────────────────────────────────
    app_version: str = "0.2.0"
    env: str = "development"  # development | staging | production
    api_prefix: str = "/api/v1/dataconnect"
    log_level: str = "INFO"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = (
        "postgresql+asyncpg://dataconnect:dataconnect@localhost:5432/dataconnect"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Kafka ─────────────────────────────────────────────────────────────────
    # The Kafka Connect cluster DataConnect talks to when a worker pool has no cluster of its own (see connect_pools).
    kafka_connect_url: str = "http://localhost:8083"
    # Kafka Connect runs as separate worker pools by workload (app/core/connect_pools.py). Each pool is its own distributed
    # Connect cluster; this maps a pool id to that cluster's REST URL, e.g.
    #   DATACONNECT_CONNECT_POOLS='{"connect-cdc-pool": "http://connect-cdc:8083", "connect-sink-pool": "http://connect-sink:8083"}'
    # A pool with no entry runs on `kafka_connect_url` — one shared cluster, which is how a small or development
    # deployment starts. Production gives each pool its own cluster, for plugin, failure and security isolation.
    connect_pools: dict[str, str] = {}
    # Each pool's internal topics (configs / offsets / status) are replicated and need a quorum to be durable: the design's
    # replication.factor 3 / min.insync.replicas 2. A single-broker development cluster overrides both to 1.
    connect_internal_replication_factor: int = 3
    connect_internal_min_insync_replicas: int = 2
    kafka_rest_proxy_url: str = "http://localhost:8082"
    kafka_bootstrap: str = "localhost:9092"
    topic_soft_limit: int = 180
    # Kafka Connect exposes no REST endpoint that lists a worker's actually-
    # configured `config.providers` (unlike connector/SMT/converter plugins,
    # which GET /connector-plugins?connectorsOnly=false does report — see
    # app/services/deploy_preflight.py) — a ConfigProvider is a worker-startup
    # setting, not a discoverable plugin. This is this app's own mirror of
    # that setting, alias -> fully-qualified ConfigProvider class, an
    # operator keeps in sync with the target Connect cluster's own
    # connect-distributed.properties. Preflight rejects a deploy referencing
    # a `${alias:...}` (see app/services/secrets.py's resolve_secret,
    # currently only ever "vault") whose alias isn't a key here.
    kafka_connect_config_providers: dict[str, str] = {}
    # GAP-05: refuse to deploy a connector whose INSTALLED plugin is older than (or a different major version from)
    # the version its template was qualified with. Off = skip that one comparison (an operator override while a
    # template pin is being updated); every other preflight check is unaffected.
    deploy_preflight_enforce_plugin_versions: bool = True
    # GAP-05: the reconciler also preflights a connector config BEFORE applying it (a pipeline edit, a missing
    # connector being recreated) — not just an explicit Deploy. Off restores the old apply-without-checking behaviour.
    reconciler_preflight_enabled: bool = True

    # ── Topic provisioning platform defaults ─────────────────────────────────
    # Used for any field left null by both the pipeline's own TopicPolicy and
    # its domain's default TopicPolicy — see app/services/topic_policy.py.
    # These are deliberately real, sensible production values (Kafka's own
    # actual broker defaults, in most cases), not placeholders: the point of
    # this ticket is that topic provisioning is an explicit, configured
    # choice at every level, never an implicit one.
    topic_default_partitions: int = 6
    topic_default_replication_factor: int = 3
    topic_default_retention_ms: int = 604_800_000  # 7 days
    topic_default_cleanup_policy: str = "delete"
    topic_default_min_insync_replicas: int = 2
    # GAP-06: check a topic policy against the Kafka cluster's real broker count BEFORE anything is asked to create the topic
    # (readiness, deploy, the reconciler, and saving a policy), not only when topic provisioning is on. The count is cached
    # briefly so a page of pipelines does not mean a page of cluster round trips.
    topic_feasibility_check_enabled: bool = True
    kafka_broker_count_cache_seconds: float = 30.0
    topic_default_compression_type: str = "producer"
    topic_default_max_message_bytes: int = 1_048_576  # 1 MiB
    topic_default_schema_strategy: str = "topic_name"

    # ── Runtime feature flags ─────────────────────────────────────────────────
    runtime_deploy_enabled: bool = False
    schema_drift_auto_approve: bool = False
    runtime_reconcile_interval_seconds: float = 15.0
    # GAP-08: how long a RuntimeReconciler process's claim on a pipeline's
    # PipelineRuntimeState row lasts before another live process may take it
    # over — must comfortably exceed a single reconcile_once() pass (there is
    # no mid-tick heartbeat/renewal the way operation_lease_seconds gets one;
    # every row is instead re-claimed, and its lease pushed forward again,
    # at the START of every tick — see app/services/runtime_reconciler.py),
    # so the process ticking every runtime_reconcile_interval_seconds always
    # renews well before this lapses. Only actually matters once more than
    # one reconciler process runs at once; a single process just keeps
    # re-claiming its own rows forever.
    runtime_reconciler_lease_seconds: float = 60.0
    monitoring_cache_ttl_seconds: float = 5.0
    # The Dashboard/Monitoring failure counts read one Kafka dead-letter topic per sink pipeline. Those reads run
    # concurrently (this many at a time) and the result is shared for this long, so a page polling every few seconds —
    # or several tabs — costs one scan, not one per request.
    monitoring_dlq_cache_ttl_seconds: float = 15.0
    monitoring_dlq_read_concurrency: int = 8
    # Threads reserved for the background DLQ counters (services/dlq_failure_counter.py). They poll Kafka forever, one
    # worker per DLQ topic; on the event loop's default thread pool (min(32, cpus+4)) enough of them occupy every thread
    # and every other to_thread call in the app — the metrics endpoints, /runtime/flow, the reconciler — queues behind them.
    dlq_counter_threads: int = 12
    # FE-BUG-17: the shared "recent" window both halves of the Quality/
    # Ingestion failure split (MonitoringService.metrics_summary) are
    # counted over — so a validation-failure count and an ingestion-failure
    # count sitting side by side in the UI are actually comparable numbers,
    # not one all-time total next to one last-minute total.
    monitoring_failure_window_seconds: float = 3600.0
    # Per-connector Prometheus metrics (GET /runtime/connectors/{name}/metrics,
    # app/services/connector_prometheus_metrics.py). Every Connect worker exposes its JMX metrics through the
    # Prometheus JMX exporter (infra/jmx-exporter/kafka-connect.yml) and Prometheus scrapes them; DataConnect only
    # QUERIES Prometheus, it never scrapes workers itself. Empty = not configured: the endpoint then answers
    # `prometheus_enabled: false` instead of failing, and the Monitoring page says so.
    prometheus_url: str = ""
    prometheus_query_timeout_seconds: float = 10.0
    # How often Prometheus scrapes the Connect workers (infra/prometheus/prometheus.yml). Chart points on ranges whose step is wider
    # than this summarise their whole bucket instead of sampling one instant.
    prometheus_scrape_interval_seconds: float = 15.0
    # Responses are cached per (connector, range) this long so a page full of pollers doesn't fan out into
    # dozens of PromQL queries per second.
    prometheus_metrics_cache_ttl_seconds: float = 10.0
    # Operation claim/lease worker (app/services/operation_runner.py): how
    # long a claimed operation's lease lasts before another worker treats it
    # as abandoned and reclaims it (must comfortably exceed how long the
    # slowest real `work()` callable — a Kafka Connect deploy, a source
    # discovery probe — can legitimately take, since the worker only renews
    # it partway through via a heartbeat, not continuously), how often the
    # worker polls for claimable rows when nothing just woke it early, and
    # how many times a single row may be (re)claimed before it's given up on
    # as permanently poisoned (e.g. its work() reliably crashes the process).
    operation_lease_seconds: float = 120.0
    operation_poll_interval_seconds: float = 2.0
    operation_max_attempts: int = 5
    # GAP-11: the largest result (serialized JSON, bytes) an Operation may persist. A real result is a few hundred bytes of
    # ids and a state; anything bigger is a work callable embedding data that should be stored elsewhere and referenced.
    operation_result_max_bytes: int = 16_384
    prefer_config_provider_refs: bool = False
    # Explicitly create/validate a source pipeline's topic via the Kafka
    # Admin API (and record the resolved TopicPolicy to the audit trail)
    # before deploying its connector — see app/services/kafka_admin.py.
    # Off by default: development relies on the Compose broker's
    # KAFKA_AUTO_CREATE_TOPICS_ENABLE=true instead, and tests never need a
    # live broker connection. Should be true in staging/production.
    topic_admin_provisioning_enabled: bool = False

    # ── Streaming quality producer ────────────────────────────────────────────
    kafka_producer_backend: str = "native"  # native (kafka-python) | rest_proxy
    kafka_produce_timeout_seconds: float = 15.0
    # Graceful shutdown (app/core/shutdown.py): each teardown step is abandoned after this long, and a Kafka client waits at most this long for
    # in-flight records / its own poll thread before it is closed anyway. An unreachable broker must not be able to hold shutdown forever.
    shutdown_step_timeout_seconds: float = 15.0
    kafka_close_timeout_seconds: float = 10.0
    kafka_produce_max_retries: int = 3
    kafka_produce_backoff_seconds: float = 0.5

    # ── Quality result persistence ────────────────────────────────────────────
    # Cap on how many failing records are embedded as `sample_failures` inside
    # a persisted dc_quality_results row — a high-volume batch must never turn
    # that JSONB column into an unbounded blob. Every failed record still
    # reaches the policy's quarantine_sink (see app/services/quarantine_sink.py);
    # only the *sample* kept alongside the aggregate in Postgres is bounded.
    quality_max_sample_failures: int = 10

    # Same bounded-JSONB idea, but for dc_quality_stream_stats.sample_failures
    # (QualityStreamWorker's continuous, real-record enforcement — see that
    # module's docstring). Deliberately a separate setting from
    # quality_max_sample_failures above rather than reusing it: that one
    # bounds a single one-off Execute-button/preview batch, while this one
    # bounds a running total that can accumulate for as long as a pipeline
    # keeps running, so it's sized generously enough to review a pipeline's
    # recent failure history without needing every single failure ever seen.
    quality_stream_max_sample_failures: int = 50

    # FE-BUG-17: how long the one-minute failure-count buckets behind the Quality
    # screen's per-rule / per-cause breakdown are kept (see models/failure_count.py).
    failure_count_retention_days: int = 7
    # GAP-10: bounds on connection discovery. A source can expose far more schemas/tables than is sensible to hold in
    # memory and store row-per-object; past this many the run keeps what fits and is marked `truncated`.
    discovery_max_objects: int = 200_000
    # How long a finished discovery run (and its objects) is kept. The most recent successful run per connection is
    # always kept regardless — it is what the UI shows — and queued/running runs are never pruned.
    discovery_retention_days: int = 7
    discovery_cleanup_interval_seconds: float = 3600.0

    # FE-BUG-20: most edges one lineage expansion hop returns before it reports
    # `truncated` (a hub asset can have thousands). The caller pages on with `offset`.
    lineage_max_edges_per_hop: int = 100

    # ── Alerts (app/services/alerts/, docs/ALERTS_IMPLEMENTATION_GUIDE.md) ───────────────────────────────────────────────────
    # The sweep: every interval, evaluate the alert rules of every organization over its pipelines and post new alerts to its
    # Slack / Teams channels. Runs inside this process (one lease per organization, so several replicas take turns). Off in tests.
    alerts_sweep_enabled: bool = True
    alerts_sweep_interval_seconds: int = 60
    alerts_webhook_timeout_seconds: float = 10.0
    # An alert no channel would take is retried every sweep (costs nothing); one every channel refused stops after this many tries.
    alerts_delivery_max_attempts: int = 5
    # How long what the sweep saw (state changes, failed operations) is kept.
    alerts_retention_days: int = 30
    # Webhook hosts allowed besides Slack's and Microsoft's (a corporate relay). In development only, a host listed here may also be
    # plain http (a local test receiver).
    alerts_webhook_extra_hosts: list[str] = []
    # Acknowledge / Resolve buttons in the Slack / Teams message: links to this address (one that people reading the channel can open)
    # signed with the secret below. Both set -> buttons; either missing -> no buttons. The link route is NOT behind the gateway
    # (the person clicking is in Slack / Teams, not signed in): the signed token is the credential.
    alerts_public_url: str = ""
    alerts_action_secret: str | None = None
    alerts_action_link_ttl_seconds: int = 604_800
    # After acknowledge / resolve / an automatic clear, post a short follow-up to the channels that received the alert (a card posted
    # through a webhook cannot be edited, so this is how the channel learns it is no longer open).
    alerts_status_updates: bool = True

    # FE-BUG-24: POST /pipelines/{id}/deploy refuses (422, with the failing checks) when the same
    # readiness checks behind the "Ready to deploy" screen fail, so the API can't bypass what the
    # UI enforces. Off switch for an operator who needs the old behaviour back.
    deploy_requires_readiness: bool = True
    # The readiness screen re-tests a connection live when its stored result is not "ok" or older
    # than this, so a fixed-since connection (or a stale pass) isn't judged on an old test.
    deploy_readiness_connection_max_age_seconds: int = 600

    # Each step of handling one record (the stats write, the quarantine copy) is retried this many
    # times, with linear backoff (quality_stream_handle_backoff_seconds * attempt), before the worker
    # gives up on THAT RECORD and dead-letters it — see quality_stream_worker.py's module docstring.
    # This only bounds how many times a *transient* failure (a DB hiccup, a broker blip) gets a
    # second chance; the offset is never committed until the record was handled or dead-lettered.
    quality_stream_max_handle_attempts: int = 3
    quality_stream_handle_backoff_seconds: float = 1.0
    # BUG-03: once a record's retries are exhausted it is written to the worker dead-letter topic
    # and only committed after that write is acknowledged. If that topic is itself unavailable the
    # worker does NOT commit: it keeps retrying (backoff grows by the base delay per attempt, up to
    # this ceiling) and the partition stalls — backpressure rather than silent loss.
    quality_stream_dead_letter_backoff_max_seconds: float = 30.0
    # GAP-03: a worker stuck on one record (the dead-letter topic is down) never commits past it, so the partition stalls. After
    # it has been stuck this long the pipeline is reported DEGRADED — the connector is fine, the quality check is not moving — and
    # a clear reason is attached. While stuck, the worker refreshes a heartbeat this often; a heartbeat older than three
    # intervals means the worker died, and the pipeline stops being reported degraded on its account.
    quality_stream_degraded_after_seconds: float = 300.0
    quality_stream_blocked_heartbeat_seconds: float = 10.0

    # ── Quality execution: preview vs. job payload limits ────────────────────
    # Preview (POST .../preview) is synchronous, in-request, and never
    # persists a QualityResult or quarantines anything — it exists for
    # UI/testing against a handful of records, so its body is capped small.
    quality_preview_max_records: int = 200
    # Execution jobs (POST .../execute-jobs) run off the request thread (see
    # app/services/quality_job_runner.py), but the request body is still
    # bounded — an unbounded payload is itself a resource-exhaustion risk
    # regardless of where the evaluation loop runs.
    quality_job_max_records: int = 20_000
    # GAP-13: the record-count ceiling above says nothing about SIZE, and 20,000 records can be any number of bytes.
    # Three byte limits, from outermost in: the request body (refused from its Content-Length, or while it streams in,
    # BEFORE it is parsed into memory — app/core/body_limit.py), any single record, and the whole staged batch.
    quality_job_max_request_bytes: int = 35_651_584  # 34 MiB: the batch limit plus JSON framing
    quality_job_max_record_bytes: int = 262_144  # 256 KiB
    quality_job_max_total_bytes: int = 33_554_432  # 32 MiB
    quality_preview_max_request_bytes: int = 2_097_152  # 2 MiB, for the 200-record synchronous preview
    # A staged batch is stored as chunks, and a worker streams through them one at a time (so it holds at most one
    # chunk in memory) and records how many it finished, so a reclaimed job resumes instead of starting over. A chunk
    # closes at whichever of these it reaches first.
    quality_job_chunk_records: int = 500
    quality_job_chunk_bytes: int = 4_194_304  # 4 MiB
    # BUG-02: QualityExecutionJob runs on a claim/lease worker (quality_job_runner.py) like
    # Operation does. A worker renews its lease every lease/2 seconds while a job runs; if it
    # dies, any worker reclaims the job once the lease lapses. A job claimed more than
    # quality_job_max_attempts times without finishing is marked `stale` instead of looping.
    quality_job_lease_seconds: float = 120.0
    quality_job_poll_interval_seconds: float = 2.0
    quality_job_max_attempts: int = 3
    # GAP-13: how long a QualityExecutionJob's persisted `records` row
    # (app/models/quality.py's QualityExecutionJobRecords) may survive at
    # most — a backstop, not the normal path: the common case deletes it
    # immediately once the job reaches a terminal state (see
    # quality_job_runner._execute), so this only matters for a job
    # that never got the chance to (a worker that died mid-run — the job's lease
    # lapses, another worker reclaims it, and it finishes (or fails) normally). Deliberately short: the
    # batch is only ever needed for the run itself, not retained as a
    # matter of course.
    quality_job_records_ttl_seconds: float = 86_400.0  # 24 hours
    # How often the periodic sweep (quality_job_runner.cleanup_expired_records,
    # started from the app lifespan) checks for and deletes any records row
    # past its expires_at that the normal delete-on-completion/resume paths
    # somehow missed.
    quality_job_records_cleanup_interval_seconds: float = 3_600.0  # 1 hour

    # ── Schema inference limits & rules ───────────────────────────────────────
    # POST /schemas/infer bounds — same settings + ValueError→413 convention
    # as the quality preview/execution-job record limits above.
    schema_infer_max_records: int = 500
    schema_infer_max_payload_bytes: int = 5 * 1024 * 1024  # 5 MiB
    # Maximum nesting depth (an object's own fields, or an array's element
    # type, either counts as one level) actually inferred — content nested
    # deeper is reported as an opaque object/array without descending
    # further, both to keep the output bounded and to avoid unbounded
    # recursion on adversarial deeply-nested input.
    schema_infer_max_depth: int = 6
    # Ceiling on the `precision` (total significant digits) reported for a
    # decimal field. A real NUMERIC/DECIMAL column rarely needs more, and it
    # keeps a single malformed sample from reporting an absurd precision.
    schema_infer_decimal_max_precision: int = 38

    # ── Outbound network policy (connection test/discovery/sampling reaches
    # customer-specified hosts — see app/core/network_policy.py) ─────────────
    # Link-local (169.254.0.0/16, fe80::/10 — cloud instance metadata) and
    # loopback are never a legitimate customer source; blocked unconditionally
    # unless explicitly turned off (e.g. for local dev against 127.0.0.1).
    network_policy_block_link_local: bool = True
    network_policy_block_loopback: bool = True
    # Reserved/multicast/unspecified addresses (0.0.0.0/8, 224.0.0.0/4, ...) —
    # never a legitimate source either.
    network_policy_block_reserved: bool = True
    # Private RFC1918/ULA ranges are the NORMAL case for a customer's own
    # VPC-internal database in a BYOC deployment — configurable, not a fixed
    # stance. A shared multi-tenant SaaS deployment should set this True.
    network_policy_block_private: bool = False
    network_probe_connect_timeout_seconds: float = 5.0
    # NEW-09: the connector catalog asks each Kafka Connect cluster which plugins it has installed, to say which connectors can actually run. A
    # cluster's answer is reused for this long, and a cluster that does not answer within the timeout is reported as unreachable (requires_configuration,
    # verified=false) rather than making the catalog wait.
    connector_availability_ttl_seconds: float = 30.0
    connector_availability_timeout_seconds: float = 5.0
    network_probe_read_timeout_seconds: float = 8.0
    network_probe_max_response_bytes: int = 5 * 1024 * 1024  # 5 MB
    network_probe_max_redirects: int = 3
    network_probe_max_concurrency: int = 10
    network_probe_max_per_org_per_minute: int = 30
    max_sample_rows: int = 500

    # ── Shared HTTP client pool (Kafka Connect REST, Kafka REST Proxy) ───────
    # One lifecycle-managed httpx.AsyncClient per endpoint, reused across every
    # request instead of opening a new connection (and TLS handshake, where
    # applicable) per call. See app/core/http_clients.py.
    http_pool_max_connections: int = 50
    http_pool_max_keepalive_connections: int = 20
    http_pool_keepalive_expiry_seconds: float = 30.0
    # Retries only ever apply to connection-level failures (the request never
    # reached the server) — never to a response that was already received —
    # so this is safe regardless of HTTP method (GET/PUT/POST/DELETE).
    http_pool_connect_retries: int = 2

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]

    # ── Security (JWT — wired up when RBAC is added) ──────────────────────────
    jwt_secret: str = "CHANGEME"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ── Gateway / authorisation (app/core/gateway.py) ─────────────────────────
    # Tenant used when Envoy forwards no x-tenant-id (the library's default).
    default_tenant: str = "default"
    # /metrics is behind the gateway like every other route unless an operator opts in for an in-cluster
    # Prometheus scraper that does not go through Envoy (it exposes only cardinality-restricted process metrics).
    metrics_public: bool = False
    # Local development ONLY (DATACONNECT_ENV=development and TRUST_ENVOY_HEADERS not "true"): serve every request
    # as a fixed principal holding these permissions — "all", or a comma list of dataconnect:* IDs. Empty (the
    # default) means no development principal: every request needs Envoy's headers. Startup refuses a non-empty
    # value in any other environment (gateway.assert_explicit_environment). No request header is ever read for it.
    dev_permissions: str = ""
    dev_user: str = "dev-user"
    dev_tenant: str = "org_1001"

    # ── Secrets backend ───────────────────────────────────────────────────────
    # Where connection credentials are stored. Only "postgres" (AES-256-GCM encrypted rows in dc_connection_secrets) is implemented;
    # "env" is the older spelling of the same thing. "vault" is rejected at startup until a Vault-backed store exists — see
    # validate_secret_backend — rather than being accepted and silently ignored.
    secret_backend: str = "env"  # env | postgres
    # GAP-01: AES-256-GCM key (32 bytes, base64) that encrypts connection credentials at rest in
    # dc_connection_secrets — see app/services/secret_crypto.py. REQUIRED outside development (startup
    # fails without it). Rotate by setting a new primary key + id and listing the old one(s) as
    # "kid:key,kid:key" in secret_encryption_previous_keys.
    secret_encryption_key: str = ""
    secret_encryption_key_id: str = "k1"
    secret_encryption_previous_keys: str = ""

    # ── Cache / Queue ─────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Unified Catalog Service (lineage sync) ───────────────────────────────
    # Opt-in, same convention as runtime_deploy_enabled / topic_admin_provisioning_enabled:
    # tests and a bare development checkout never need a live UCS instance.
    ucs_lineage_sync_enabled: bool = False
    ucs_base_url: str = "http://localhost:8000"
    ucs_lineage_sync_user: str = "dataconnect-sync"
    # UCS's header-trust path (app/identity.py::principal_from_headers) fails closed on a
    # request that doesn't carry the same shared secret its own Envoy sidecar would stamp
    # (gateway_auth.verify_gateway_secret) — this service-to-service call has no Envoy in
    # front of it, so it must send the secret itself. Empty by default so a UCS instance
    # without this requirement (or none configured at all, in dev) still gets a real 401
    # to log rather than a silently-wrong header; must equal UCS's own ENVOY_INTERNAL_SECRET.
    ucs_gateway_secret: str = ""
    # Separate flag from ucs_lineage_sync_enabled (shares its base_url/user)
    # so quality-result syncing can be toggled independently — see
    # app/services/ucs_quality_sync.py.
    ucs_quality_sync_enabled: bool = False
    # The quality stream worker reports a pipeline's running pass/fail totals to UCS at most this often, and only when they changed.
    ucs_quality_stream_sync_interval_seconds: float = 60.0
    # How often an ENFORCE sink's quality worker deletes the hand-off records its connector has already written (kafka_admin.trim_consumed_records).
    sink_handoff_trim_interval_seconds: float = 30.0

    @field_validator("secret_backend")
    @classmethod
    def validate_secret_backend(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v == "vault":
            raise ValueError(
                "secret_backend 'vault' is not implemented: connection credentials are stored AES-256-GCM encrypted in PostgreSQL. "
                "Use 'postgres' (or 'env', the same thing)."
            )
        if v not in {"env", "postgres"}:
            raise ValueError("secret_backend must be one of: env, postgres")
        return v

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"env must be one of {allowed}")
        return v

    model_config = SettingsConfigDict(
        env_prefix="DATACONNECT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

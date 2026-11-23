"""Configuration for the FlinkSQL Runner service."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core import quality_contract

PLACEHOLDER_ORG_IDS = frozenset({"", "local-dev", "REPLACE_ME"})


# Audiences that are another service's (or the generic Workspace one): never this runner's.
FORBIDDEN_AUDIENCES = frozenset({"workspace.inaiera", "dbtrunner", "dq", "ucs.inaiera", "datasets.inaiera"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "flinksql-runner"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    # RUN-FLINK-009: the org this dedicated data plane serves. The two
    # placeholder values below are only acceptable in dev — see
    # _refuse_placeholder_org_outside_dev().
    org_id: str = Field(default="local-dev")

    # Unique per process — owner id written into deployment leases
    # (RUN-FLINK-013). Defaults to "<hostname>-<random>" at startup.
    instance_id: str | None = None

    # Lets the test suite (which runs the app against in-memory SQLite)
    # keep the Postgres-backed background loops off. Always true in a
    # real deployment.
    background_tasks_enabled: bool = True

    # Control plane JWT validation
    control_plane_jwks_url: str = "https://www.app.inaiera.com/.well-known/jwks.json"
    control_plane_issuer: str = "https://www.app.inaiera.com"
    # X-001: THIS service's own audience. A token minted for Workspace ("workspace.inaiera") is not
    # accepted here; external CI uses a service-principal token with this audience and the route's scope.
    control_plane_audience: str = "flinksql-runner"
    # DBT-002: the longest an externally issued (service-principal / CI, RS256 via JWKS) token may live: exp - iat. 0 disables the cap.
    external_jwt_max_lifetime_seconds: int = 3600
    # RUN-FLINK-009: JWKS documents are cached for this long (and
    # refreshed early, at most once per jwks_min_refresh_interval_seconds,
    # when a token's signature doesn't verify — i.e. a key rotation).
    jwks_cache_ttl_seconds: int = 3600
    jwks_min_refresh_interval_seconds: int = 30

    # Postgres metadata DB
    database_url: str = "postgresql+asyncpg://flinksql:flinksql@localhost:5432/flinksql"

    # Redis (metrics events pub/sub)
    redis_url: str = "redis://localhost:6379/3"

    # Apache Flink SQL Gateway REST API
    flink_gateway_url: str = "http://flink-sql-gateway.dp-shared.svc.cluster.local:8083"
    flink_rest_url: str = "http://flink-jobmanager.dp-shared.svc.cluster.local:8081"
    flink_gateway_timeout_seconds: int = 30
    flink_submit_timeout_seconds: int = 120

    # Kafka
    kafka_bootstrap_servers: str = "kafka.dp-shared.svc.cluster.local:9092"
    kafka_security_protocol: str = "SASL_SSL"
    kafka_sasl_mechanism: str = "SCRAM-SHA-512"
    # RUN-FLINK-016: the Helm chart historically injected KAFKA_USERNAME/
    # KAFKA_PASSWORD while this read KAFKA_SASL_USERNAME/PASSWORD, so the
    # credentials never reached the app. Both spellings are accepted now;
    # the chart injects the canonical KAFKA_SASL_* names.
    kafka_sasl_username: str | None = Field(
        default=None,
        validation_alias=AliasChoices("kafka_sasl_username", "KAFKA_SASL_USERNAME", "KAFKA_USERNAME"),
    )
    kafka_sasl_password: str | None = Field(
        default=None,
        validation_alias=AliasChoices("kafka_sasl_password", "KAFKA_SASL_PASSWORD", "KAFKA_PASSWORD"),
    )
    # CA bundle for SASL_SSL/SSL brokers (mounted from a Secret by Helm).
    kafka_ssl_cafile: str | None = None
    kafka_admin_timeout_seconds: int = 10

    # Confluent REST Proxy (topic listing, preview, consumer-group lag).
    # RUN-FLINK-016: an explicit URL (http or https), never derived as
    # http://{bootstrap-host}:8082. Unset = the REST-Proxy-backed features
    # report "unavailable" (dev serves fixtures; elsewhere nothing).
    kafka_rest_proxy_url: str | None = None
    kafka_rest_proxy_username: str | None = None   # falls back to the SASL credentials
    kafka_rest_proxy_password: str | None = None
    kafka_rest_proxy_ca_file: str | None = None    # custom CA for https
    kafka_rest_proxy_verify_ssl: bool = True

    # Kafka catalog/preview governance (RUN-FLINK-016) — explicit caps on
    # what a topic preview can return, instead of the only limit being
    # whatever the caller happened to ask the API for.
    kafka_preview_max_messages: int = 50
    kafka_preview_max_bytes_per_message: int = 8192
    kafka_preview_timeout_seconds: float = 15.0
    # Hard cap on the whole preview response (all messages, keys included)
    # and on any single key.
    kafka_preview_max_total_bytes: int = 262_144
    kafka_preview_max_key_bytes: int = 256
    # Org/topic scoping applied to topic listing, schema lookup and preview
    # (fnmatch patterns; "{org_id}" is substituted). Empty = no local
    # pattern filter (UCS policy still applies when configured).
    kafka_topic_allowed_patterns: list[str] = Field(default_factory=list)

    # Schema Registry
    schema_registry_url: str = "http://schema-registry.dp-shared.svc.cluster.local:8081"
    schema_registry_auth_enabled: bool = False
    # RUN-FLINK-016: Schema Registry has its own credentials/TLS (falls
    # back to the Kafka SASL credentials when auth is enabled but these
    # are unset — the previous behaviour).
    schema_registry_username: str | None = None
    schema_registry_password: str | None = None
    schema_registry_ca_file: str | None = None

    # Apache Polaris — address only: the runner holds no Polaris credential (FL-008, CREDENTIAL_OWNERSHIP.md)
    polaris_rest_uri: str = "http://polaris.dp-shared.svc.cluster.local:8181/api/catalog"
    iceberg_warehouse: str = "s3://inaiera-lakehouse/warehouse"

    # FL-001: the governed pg_lake connection, registered by the runner as a named JDBC catalog in each Gateway session.
    # Jobs write `INSERT INTO pg_lake.<database>.<table> ...`; the URL, user and password live here (a Secret in the chart)
    # and never in a job's SQL. All four must be set for the catalog to exist.
    pg_lake_catalog_name: str = "pg_lake"
    pg_lake_jdbc_base_url: str | None = None     # e.g. jdbc:postgresql://pg-lake.dp-shared.svc:5432
    pg_lake_database: str | None = None          # the Postgres database the catalog exposes
    pg_lake_user: str | None = None
    pg_lake_password: str | None = None

    # Checkpoint storage
    checkpoint_storage_backend: Literal["s3", "azure_blob", "gcs", "filesystem"] = "s3"
    checkpoint_storage_path: str = "s3://inaiera-flink-checkpoints"
    checkpoint_interval_seconds: int = 60
    checkpoint_timeout_seconds: int = 600
    checkpoint_min_pause_seconds: int = 5
    checkpoint_max_concurrent: int = 1

    # Internal service token — the HMAC signing key for scoped
    # service-to-service JWTs (app.core.service_auth).
    internal_service_token: str | None = None
    # RUN-FLINK-009: accepting the raw static token itself as a bearer
    # credential (the pre-DQ-AUTH-012 behaviour) is OFF unless explicitly
    # enabled. Unset = enabled only in dev (local stacks whose Workspace
    # still sends the static token); staging/prod must opt in explicitly.
    legacy_internal_token_enabled: bool | None = None

    # X-001: HMAC key Workspace signs its short-lived delegated JWT with (aud "flinksql-runner",
    # ONE scope, sub = the end user, act.sub = "workspace"). A key of its own: it must differ from
    # internal_service_token, so holding the service-to-service key never lets a caller act as a user.
    # No jti replay store here, so the lifetime cap is short (a deploy token is valid for at most this long).
    delegation_jwt_secret: str | None = None
    delegation_jwt_allowed_issuers: list[str] = ["workspace"]
    # X-001: the Data Quality service also acts on a user's behalf here (it reads a job and creates/updates its quality
    # job), signing with a key of ITS OWN. DQ is an accepted issuer only while this is set, and only for the scopes below:
    # it cannot deploy, stop or delete a job.
    dq_delegation_jwt_secret: str | None = None
    dq_delegation_issuer: str = "dataquality"
    dq_delegation_allowed_scopes: list[str] = ["flink.jobs.read", "flink.jobs.edit"]
    delegation_jwt_max_lifetime_seconds: int = 120
    delegation_jwt_clock_skew_seconds: int = 30

    # Metrics polling
    metrics_poll_interval_seconds: int = 15
    metrics_retention_days: int = 30

    # Deployment reconciliation (RUN-FLINK-013) — how often the durable
    # controller re-scans jobs stuck 'deploying'/'pausing' with no live
    # in-process owner (e.g. after a pod restart) and resumes them from
    # persisted desired state + Flink's own runtime state.
    deployment_reconciler_poll_interval_seconds: int = 20
    # FL-011: the leader's reconciler is the only owner of deploy/stop execution. A persisted deploy/stop is picked up at once
    # through a Postgres NOTIFY, and — as the safety net if that is lost — by this short poll of 'deploying'/'pausing' jobs.
    deployment_dispatch_poll_seconds: float = 2.0
    # At most this many deploys/stops run at once in the leader (each holds a Gateway session for its duration).
    deployment_dispatch_concurrency: int = 8
    # Per-deployment ownership lease (RUN-FLINK-013): the process driving a
    # deploy/stop holds it and heartbeats; the reconciler only touches a
    # deployment whose lease has expired, and must re-claim it first.
    deployment_lease_ttl_seconds: int = 60

    # Alert thresholds — every alert is evaluated over its window, and only
    # fires once the snapshots in the window cover at least
    # alert_window_min_coverage of it (no alert off one reading).
    alert_kafka_lag_threshold: int = 100_000
    alert_kafka_lag_window_minutes: int = 5
    alert_window_min_coverage: float = 0.8
    alert_checkpoint_failure_window_minutes: int = 30
    alert_backpressure_threshold: float = 0.8
    alert_backpressure_window_minutes: int = 5
    alert_restart_threshold: int = 3
    alert_restart_window_minutes: int = 10

    # CORS
    cors_origins: list[str] = ["https://www.app.inaiera.com"]

    # -------------------------------------------------------------------------
    # NEW: UCS integration (quality violations → quality scores)
    # -------------------------------------------------------------------------
    # FlinkSQL quality jobs write the shared contract's records (results /
    # gate topics); the quality_consumer reads them, aggregates QualityRuns
    # and delivers them to UCS.
    #
    # Set ucs_url to: http://catalog.dataplane.svc:8000
    # Set ucs_internal_token to the UCS internal service token (same token
    # used by Workspace's catalog proxy — stored in dp-shared secret).
    ucs_url: str | None = None
    ucs_internal_token: str | None = None

    # DQ-CONTRACT-005: the shared, versioned quality contract
    # (app/contracts/quality_contract.json — byte-identical in
    # inaiera-data-quality). Topics are injected from config, defaulting to
    # the contract; overriding one requires the same override on DQ. A
    # configured contract version this build does not implement makes the
    # consumer misconfigured (readiness 503) instead of mis-parsing records.
    quality_contract_version: str = quality_contract.CONTRACT_VERSION
    quality_results_topic: str = quality_contract.RESULTS_TOPIC
    quality_gate_topic: str = quality_contract.GATE_TOPIC
    quality_quarantine_topic: str = quality_contract.QUARANTINE_TOPIC

    # Flink version reported in the StructuredJobPlan (DQ-014 compatibility).
    flink_version: str = "1.19.1"

    # DQ-RESULT-011: deliver QualityRuns (started / completed_* / unknown /
    # stale) and gate events to UCS's contract-v2 endpoints. Off until UCS
    # ships them: completed runs then go to the legacy /quality/results and
    # lifecycle-only events are kept in the outbox as 'skipped'.
    ucs_quality_runs_enabled: bool = False
    # A check whose heartbeat (latest window) is older than
    # max(this, contract stale_after_windows x window) is marked stale.
    quality_check_stale_after_seconds: float = 300.0
    # Runner-level kill switch for the gate's fail_stop action (the per-
    # binding opt-in lives in DQ; this lets ops disable stopping entirely).
    quality_gate_fail_stop_enabled: bool = True

    # RUN-FLINK-010: the consumer runs only when explicitly enabled — never
    # inferred from what the Kafka bootstrap address looks like.
    quality_consumer_enabled: bool = False
    # Kafka connect/poll failures are retried with exponential backoff
    # capped at this many seconds (never "give up and return").
    quality_consumer_max_backoff_seconds: float = 60.0
    # Readiness treats the consumer as stalled if it hasn't completed a
    # poll for this long.
    quality_consumer_stale_after_seconds: float = 120.0

    # Consumer group for the quality violations reader.
    quality_consumer_group_id: str = "flinksql-runner-quality-consumer"

    # How long to wait between UCS POST retries on transient errors.
    quality_consumer_retry_backoff_seconds: float = 5.0

    # Maximum violations to batch before flushing to UCS (reduces HTTP calls).
    quality_consumer_batch_size: int = 50

    # How often to flush the batch even if not full (seconds).
    quality_consumer_flush_interval_seconds: float = 10.0

    # FL-004: a worker claims outbox rows for this long (a lease on the row) while it delivers them OUTSIDE any
    # database transaction. It must outlast one row's delivery (UCS retries ~3 x 10 s); a worker that dies
    # leaves its rows claimable again after this.
    quality_outbox_lease_seconds: float = 300.0
    # FL-005: a row whose delivery failed is retried after base * 2^(attempts-1) seconds, capped at the max
    # (persisted in next_attempt_at, so a restart does not reset it); after max_attempts failed deliveries the
    # row is PARKED (kept, never retried) until an operator requeues it — see docs.
    quality_outbox_retry_base_seconds: float = 30.0
    quality_outbox_retry_max_seconds: float = 900.0
    quality_outbox_max_attempts: int = 20

    # Readiness (/readyz) — which dependencies are probed.
    readiness_check_flink: bool = True
    readiness_probe_timeout_seconds: float = 2.0

    @model_validator(mode="after")
    def _own_audience_only(self) -> "Settings":
        """DBT-002: the audience is THIS service's — never Workspace's generic one nor a sibling service's."""
        aud = (self.control_plane_audience or "").strip()
        if not aud or aud in FORBIDDEN_AUDIENCES:
            raise ValueError(
                f"CONTROL_PLANE_AUDIENCE must be this service's own audience (\"flinksql-runner\"), never empty or one of "
                f"{sorted(FORBIDDEN_AUDIENCES)} (got {self.control_plane_audience!r}): tokens meant for another service must not be accepted here")
        return self

    @model_validator(mode="after")
    def _refuse_placeholder_org_outside_dev(self) -> "Settings":
        """RUN-FLINK-009: outside dev, a placeholder org id would make the
        data-plane org boundary check compare tokens against a value no
        real org has (or, worse, one shared by every unconfigured install)
        — refuse to start instead."""
        if self.environment != "dev" and (self.org_id or "").strip() in PLACEHOLDER_ORG_IDS:
            raise ValueError(
                f"ORG_ID must be set to this data plane's real org id when "
                f"ENVIRONMENT={self.environment} (got {self.org_id!r})"
            )
        if self.delegation_jwt_secret and self.delegation_jwt_secret == self.internal_service_token:
            raise ValueError(
                "DELEGATION_JWT_SECRET must differ from INTERNAL_SERVICE_TOKEN — the service-to-service "
                "key must not be able to sign tokens that act as a user"
            )
        if self.dq_delegation_jwt_secret and self.dq_delegation_jwt_secret in (
                self.internal_service_token, self.delegation_jwt_secret):
            raise ValueError(
                "DQ_DELEGATION_JWT_SECRET must differ from INTERNAL_SERVICE_TOKEN and DELEGATION_JWT_SECRET — "
                "a key that signs a user must not be shared with any other trust domain"
            )
        return self

    @property
    def legacy_internal_token_allowed(self) -> bool:
        if self.legacy_internal_token_enabled is None:
            return self.environment == "dev"
        return self.legacy_internal_token_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()

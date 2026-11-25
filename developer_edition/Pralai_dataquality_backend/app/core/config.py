"""Configuration.

DQ-CFG-001: every outbound integration (UCS, dbt Runner, FlinkSQL Runner)
has its OWN explicit URL, its OWN signing secret, its own audience and an
explicit `*_enabled` switch. There are no in-cluster default URLs any more —
an enabled integration with no URL or no secret is a configuration error
that fails startup (app.main lifespan) and readiness (/readyz), instead of
looking healthy until the first sync/apply soft-fails deep inside a service.
`integration_problems()` is the single source of that judgement.

DQ-AUTH-012: the per-integration secrets are HMAC keys for short-lived,
org-bound, audience+scope JWTs minted per call (app.core.service_auth) —
never sent on the wire themselves. Each integration has a separate secret
so a leak of one cannot mint tokens another service accepts.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core import quality_contract

PLACEHOLDER_ORG_IDS = {"", "local-dev", "REPLACE_ME", "changeme"}
MAX_SERVICE_TOKEN_TTL_SECONDS = 300
MIN_SECRET_LENGTH_NON_DEV = 32

# DQ permission vocabulary (app.core.permissions). Roles on the inbound
# control-plane JWT map onto these; a token may also carry them directly
# in a `permissions` / space-separated `scope` claim.
DEFAULT_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": ["dq.read", "dq.rules.write", "dq.custom_sql.write", "dq.bind", "dq.apply",
              "dq.sync"],
    "workspace_admin": ["dq.read", "dq.rules.write", "dq.custom_sql.write", "dq.bind",
                        "dq.apply", "dq.sync"],
    "dq_admin": ["dq.read", "dq.rules.write", "dq.custom_sql.write", "dq.bind", "dq.apply",
                 "dq.sync"],
    "developer": ["dq.read", "dq.rules.write", "dq.bind", "dq.apply", "dq.sync"],
    "data_engineer": ["dq.read", "dq.rules.write", "dq.bind", "dq.apply", "dq.sync"],
    "streaming_data_engineer": ["dq.read", "dq.rules.write", "dq.bind", "dq.apply", "dq.sync"],
    "analyst": ["dq.read"],
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Service identity ────────────────────────────────────────────────
    service_name: str = "data-quality"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    org_id: str = Field(default="local-dev",
                        description="Customer org id — set per data plane at provision time")

    # ── Inbound user auth (control-plane JWKS; org-bound, app.core.auth) ─
    control_plane_jwks_url: str = "https://www.app.inaiera.com/.well-known/jwks.json"
    control_plane_issuer: str = "https://www.app.inaiera.com"
    control_plane_audience: str = "workspace.inaiera"
    jwks_cache_ttl_seconds: int = 3600
    role_permissions: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_ROLE_PERMISSIONS.items()})

    # ── Inbound service auth (Workspace codegen -> /render/validate/rule) ─
    # Scoped JWT: aud "dataquality", scope "dq.rules.validate", org-bound,
    # signed with this secret. The legacy raw static token is accepted ONLY
    # when explicitly enabled AND environment=dev.
    inbound_service_jwt_secret: str | None = None
    inbound_service_jwt_allowed_issuers: list[str] = ["workspace"]
    legacy_internal_token_enabled: bool = False
    internal_service_token: str | None = None

    # ── Outbound service JWTs (shared claim policy) ─────────────────────
    service_jwt_issuer: str = "dataquality"
    service_jwt_subject: str = "dataquality"
    service_jwt_ttl_seconds: int = 60

    # ── Metadata DB ─────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://dataquality:dataquality@localhost:5432/dataquality"
    # Injected from a Secret separately so it never sits in a URL/ConfigMap
    # (and needs no URL escaping).
    database_password: str | None = None
    database_pool_size: int = 10
    database_pool_disabled: bool = False

    redis_url: str = "redis://localhost:6379/4"

    # ── UCS ─────────────────────────────────────────────────────────────
    ucs_enabled: bool = True
    ucs_url: str | None = None
    ucs_service_jwt_secret: str | None = None
    ucs_audience: str = "ucs"
    # The UCS action authorised before a ruleset may be bound to a dataset.
    ucs_bind_action: str = "dq.bind"
    # Dev-only escape hatch while UCS lacks the v2 contract: bindings are
    # created with resolution.dataset.verified=false. Refused outside dev.
    ucs_bind_dev_bypass: bool = False
    # QualityCheckRegistration / audit-event endpoints (contract v2). UCS
    # does not implement them yet (see README follow-ups) — until it does,
    # outbox rows stay durably `pending` and nothing is lost.
    ucs_contract_v2_enabled: bool = False
    ucs_check_registrations_path: str = "/api/v1/quality/checks/registrations"
    ucs_audit_events_path: str = "/api/v1/audit/events"

    # ── dbt Runner ──────────────────────────────────────────────────────
    dbtrunner_enabled: bool = True
    dbtrunner_url: str | None = None
    # Must equal the dbt runner's SERVICE_JWT_SECRET (NOT its legacy
    # INTERNAL_SERVICE_TOKEN, which the runner rejects on scoped routes).
    dbtrunner_service_jwt_secret: str | None = None
    dbtrunner_audience: str = "dbtrunner"
    # dbt-core version the runner fleet runs (DQ-014 compile-time check;
    # the runner re-checks the artifact's declared requirements on apply).
    dbtrunner_engine_version: str = "1.10.0"

    # ── FlinkSQL Runner ─────────────────────────────────────────────────
    flinksql_enabled: bool = True
    flinksql_runner_url: str | None = None
    flinksql_service_jwt_secret: str | None = None
    flinksql_audience: str = "flinksql-runner"

    # ── Python (GX/Soda pulled by the job) ──────────────────────────────
    python_engine_version: str = "0.18.21"

    # ── DQ-CONTRACT-005: shared quality result contract ─────────────────
    quality_contract_version: str = quality_contract.CONTRACT_VERSION
    quality_results_topic: str = quality_contract.RESULTS_TOPIC
    quality_gate_topic: str = quality_contract.GATE_TOPIC
    quality_quarantine_topic: str = quality_contract.QUARANTINE_TOPIC

    # DQ-014 ops pin: per job_type, the engine contract values this
    # deployment allows to be applied (empty = no extra restriction beyond
    # the renderer's declared supported range).
    supported_engine_versions: dict[str, list[str]] = Field(default_factory=dict)

    # ── Outbox delivery (audit events, check registrations) ─────────────
    outbox_worker_enabled: bool = True
    outbox_poll_interval_seconds: float = 5.0
    outbox_batch_size: int = 50
    outbox_max_backoff_seconds: float = 300.0

    seed_system_rules: bool = True

    max_rules_per_org: int = 1000
    max_rulesets_per_org: int = 200
    max_items_per_ruleset: int = 50
    max_bindings_per_org: int = 500

    cors_origins: list[str] = ["https://www.app.inaiera.com"]

    def effective_database_url(self) -> str:
        if not self.database_password:
            return self.database_url
        from sqlalchemy.engine import make_url
        return make_url(self.database_url).set(password=self.database_password) \
            .render_as_string(hide_password=False)

    @field_validator("service_jwt_ttl_seconds")
    @classmethod
    def _ttl_capped(cls, v: int) -> int:
        if not 1 <= v <= MAX_SERVICE_TOKEN_TTL_SECONDS:
            raise ValueError(f"service_jwt_ttl_seconds must be 1..{MAX_SERVICE_TOKEN_TTL_SECONDS}")
        return v

    # ── DQ-CFG-001 ──────────────────────────────────────────────────────
    def integration_problems(self) -> list[str]:
        """Every reason this deployment is misconfigured. Empty == OK.

        Used by startup (fail fast) and /readyz (503) so an enabled-but-
        misconfigured integration can never look healthy."""
        problems: list[str] = []
        secrets: dict[str, str] = {}
        integrations = (
            ("UCS", self.ucs_enabled, self.ucs_url, self.ucs_service_jwt_secret,
             "UCS_URL", "UCS_SERVICE_JWT_SECRET"),
            ("dbt runner", self.dbtrunner_enabled, self.dbtrunner_url,
             self.dbtrunner_service_jwt_secret, "DBTRUNNER_URL", "DBTRUNNER_SERVICE_JWT_SECRET"),
            ("FlinkSQL runner", self.flinksql_enabled, self.flinksql_runner_url,
             self.flinksql_service_jwt_secret, "FLINKSQL_RUNNER_URL",
             "FLINKSQL_SERVICE_JWT_SECRET"),
        )
        for name, enabled, url, secret, url_env, secret_env in integrations:
            if not enabled:
                continue
            if not url:
                problems.append(f"{name} is enabled but {url_env} is not set")
            else:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    problems.append(f"{url_env}={url!r} is not an absolute http(s) URL")
            if not secret:
                problems.append(f"{name} is enabled but {secret_env} is not set")
            else:
                if self.environment != "dev" and len(secret) < MIN_SECRET_LENGTH_NON_DEV:
                    problems.append(
                        f"{secret_env} must be at least {MIN_SECRET_LENGTH_NON_DEV} characters")
                secrets[secret_env] = secret
        seen: dict[str, str] = {}
        for env_name, value in secrets.items():
            if value in seen:
                problems.append(
                    f"{env_name} and {seen[value]} must be different secrets "
                    "(one signing secret per integration)")
            seen.setdefault(value, env_name)
        if self.quality_contract_version != quality_contract.CONTRACT_VERSION:
            problems.append(
                f"QUALITY_CONTRACT_VERSION={self.quality_contract_version!r} but this build "
                f"implements quality contract {quality_contract.CONTRACT_VERSION!r}")
        if self.ucs_bind_dev_bypass and self.environment != "dev":
            problems.append("UCS_BIND_DEV_BYPASS is only allowed when ENVIRONMENT=dev")
        if self.legacy_internal_token_enabled and self.environment != "dev":
            problems.append("LEGACY_INTERNAL_TOKEN_ENABLED is only allowed when ENVIRONMENT=dev")
        if self.environment != "dev" and self.org_id.strip() in PLACEHOLDER_ORG_IDS:
            problems.append(f"ORG_ID must be set to the real org id (got {self.org_id!r})")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()

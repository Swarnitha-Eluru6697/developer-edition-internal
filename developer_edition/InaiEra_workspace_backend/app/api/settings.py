"""Settings → Engines: what this Workspace is connected to, live.

GET /api/v1/settings/engines probes every engine and platform service the
Workspace depends on, concurrently, and reports for each one whether it is
connected, unreachable, or not configured — and, when something is missing,
exactly which settings (env vars) are still required and what stops working
without them.

Never returns a secret: tokens/keys are reported only as set / not set.
Endpoint URLs are internal topology, so they are included only for callers
holding workspace:settings:manage.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.core.config import get_settings
from app.core.deps import DB
from app.core.errors import WorkspaceError, forbidden, validation_failed
from app.core.rbac import SETTINGS_MANAGE, RequireSettingsEdit, RequireSettingsManage
from app.db.session import AsyncSessionLocal
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_PROBE_TIMEOUT = 3.0

Status = Literal["connected", "unreachable", "not_configured", "disabled"]


class EngineStatus(BaseModel):
    # Response-only: every field is always serialised, so mark the defaulted
    # ones required in the OpenAPI schema too (accurate generated SPA types).
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    key: str
    name: str
    category: Literal["engine", "runner", "platform"]
    description: str
    status: Status
    detail: str | None = None
    latency_ms: int | None = None
    endpoint: str | None = None
    # Env vars still required for this to work (empty when nothing is missing).
    missing: list[str] = []
    # What stops working while this isn't connected.
    impact: str | None = None


class EnginesOut(BaseModel):
    engines: list[EngineStatus]
    checked_at: float


Provider = Literal["codegen", "anthropic", "openai"]


class CodegenAgentConfigOut(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    provider: Provider
    label: str
    # The model that answers: the gateway's default logical model, or the provider's configured model.
    model: str | None = None
    # Only a holder of workspace:settings:manage sees even the masked form of the key; everyone sees whether one is saved.
    masked_key: str | None = None
    has_key: bool
    # Whether the caller may change the agent (holders of workspace:settings:manage).
    can_edit: bool
    # The agents that may be chosen here, in display order (no gateway option when codegen_gateway_enabled is off).
    available_providers: list[Provider] = ["codegen", "anthropic", "openai"]
    status: Status
    message: str | None = None


class VerifyKeyIn(BaseModel):
    provider: Provider
    api_key: str | None = None


class VerifyKeyOut(BaseModel):
    success: bool
    message: str


class UpdateAgentIn(BaseModel):
    provider: Provider
    api_key: str | None = None


async def _timed(probe: Callable[[], Awaitable[str | None]]) -> tuple[Status, str | None, int]:
    """Run a probe; any exception means unreachable. Returns (status, detail, ms)."""
    start = time.monotonic()
    try:
        detail = await asyncio.wait_for(probe(), timeout=_PROBE_TIMEOUT + 1)
        return "connected", detail, int((time.monotonic() - start) * 1000)
    except Exception as e:  # noqa: BLE001 — every failure mode is "unreachable" here
        msg = str(e) or type(e).__name__
        return "unreachable", msg[:200], int((time.monotonic() - start) * 1000)


def _http_probe(base_url: str, path: str = "/healthz") -> Callable[[], Awaitable[str | None]]:
    async def probe() -> str | None:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{base_url.rstrip('/')}{path}")
        if resp.status_code >= 500:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return None
    return probe


async def _check(
    *, key: str, name: str, category: str, description: str, endpoint: str | None,
    probe: Callable[[], Awaitable[str | None]] | None, missing: list[str], impact: str,
    disabled_reason: str | None = None, config_only: bool = False,
) -> EngineStatus:
    base = dict(key=key, name=name, category=category, description=description, endpoint=endpoint)
    if disabled_reason:
        return EngineStatus(**base, status="disabled", detail=disabled_reason, impact=impact)
    if probe is None:
        return EngineStatus(**base, status="not_configured", missing=missing, impact=impact)
    status, detail, ms = await _timed(probe)
    return EngineStatus(
        **base, status=status, detail=detail, latency_ms=None if config_only else ms,
        # A reachable service can still be missing a setting (e.g. its token).
        missing=missing, impact=impact if status != "connected" or missing else None,
    )


async def _db_probe() -> str | None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return None


async def _redis_probe() -> str | None:
    from app.services.execution_router import get_redis
    await get_redis().ping()
    return None


async def _storage_probe() -> str | None:
    from app.storage import get_blob_store
    # A real round trip to the bucket; a missing key is a normal False.
    await get_blob_store().exists("__settings_healthcheck__")
    return None


async def _codegen_gateway_probe() -> str | None:
    """The selected agent must answer. For OpenAI/Anthropic: its API accepts the saved key. For the default: the gateway is up
    AND serving the logical models the harness asks for — a gateway that is up but missing `codegen-generation` would still fail
    every turn, so report which ones are absent."""
    from app.services.codegen_provider import (
        EXTERNAL_PROVIDERS,
        LABELS,
        get_codegen_agent_config,
        model_for,
        verify_provider_key,
    )
    cfg = await get_codegen_agent_config()
    prov = cfg["provider"]
    if prov in EXTERNAL_PROVIDERS:
        ok, msg = await verify_provider_key(prov, cfg.get("api_key"))
        if not ok:
            raise RuntimeError(msg)
        return f"{LABELS[prov].replace(' key', '')} · {model_for(prov)}"

    s = get_settings()
    headers = {"Authorization": f"Bearer {s.codegen_gateway_api_key}"} if s.codegen_gateway_api_key else {}
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        resp = await client.get(f"{s.codegen_gateway_url.rstrip('/')}/models", headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    served = {m.get("id") for m in resp.json().get("data", [])}
    wanted = [*s.codegen_generation_models, s.codegen_model_utility]
    absent = [m for m in wanted if m not in served]
    if absent:
        raise RuntimeError(f"gateway is missing models: {', '.join(absent)}")
    return f"Default model {s.codegen_model_generation}"


def dq_missing_settings(s) -> list[str]:
    """What DQ still needs. The scoped-token dependency is the signing secret:
    outside dev the legacy raw DQ_INTERNAL_TOKEN is never sent (see
    app/core/service_tokens.py), so having only that token leaves
    /dq/validate/rule answering 503 while DQ looks configured (W-002). In dev
    either credential works (the legacy one is the old-image fallback)."""
    missing = [] if s.dq_service_url else ["DQ_SERVICE_URL"]
    has_credential = s.dq_service_jwt_secret or (s.environment == "dev" and s.dq_internal_token)
    if not has_credential:
        missing.append("DQ_SERVICE_JWT_SECRET")
    return missing


@router.get("/engines", response_model=EnginesOut)
async def list_engines(user: RequireSettingsEdit) -> EnginesOut:
    from app.services.codegen_provider import EXTERNAL_PROVIDERS, default_provider, get_codegen_agent_config

    s = get_settings()
    try:
        current_prov = (await get_codegen_agent_config())["provider"]
    except Exception:  # noqa: BLE001 — the setting is unreadable: show the default card; its probe reports the problem
        current_prov = default_provider()
    external = current_prov in EXTERNAL_PROVIDERS
    codegen_name = {"openai": "Codegen agent (OpenAI)", "anthropic": "Codegen agent (Anthropic)"}.get(
        current_prov, "Codegen agent (model gateway)")
    codegen_desc = {
        "openai": "OpenAI, with your API key, powers the ⌘K assistant. Prompts and schema context leave your perimeter.",
        "anthropic": "Anthropic Claude, with your API key, powers the ⌘K assistant. Prompts and schema context leave your perimeter.",
    }.get(current_prov, "Self-hosted models behind the OpenAI-compatible gateway that power the ⌘K assistant.")

    checks = [
        _check(
            key="lakehouse", name="Lakehouse query (Trino / pg_lake)", category="engine",
            description="Runs SQL from the editor against the data lakehouse.",
            endpoint=s.lakehouse_query_url,
            probe=_http_probe(s.lakehouse_query_url) if s.lakehouse_query_url else None,
            missing=[] if s.lakehouse_query_url else ["LAKEHOUSE_QUERY_URL"],
            impact="SQL against the default warehouse can't run.",
        ),
        _check(
            key="flink", name="FlinkSQL Runner", category="runner",
            description="Validates, deploys and monitors Flink streaming jobs.",
            endpoint=s.flinksql_runner_url,
            probe=_http_probe(s.flinksql_runner_url) if s.flinksql_runner_url else None,
            missing=([] if s.flinksql_runner_url else ["FLINKSQL_RUNNER_URL"])
                    + ([] if s.flinksql_internal_token else ["FLINKSQL_INTERNAL_TOKEN"]),
            impact="Flink jobs can't be listed, validated or deployed.",
        ),
        _check(
            key="dbt", name="dbt Runner", category="runner",
            description="Registers dbt projects from Git and runs them.",
            endpoint=s.dbtrunner_url,
            probe=_http_probe(s.dbtrunner_url) if s.dbtrunner_url else None,
            missing=[] if s.dbtrunner_url else ["DBTRUNNER_URL"],
            impact="dbt projects, runs and lineage are unavailable.",
        ),
        _check(
            key="dq", name="Data Quality service", category="runner",
            description="Renders quality checks into dbt / Flink jobs.",
            endpoint=s.dq_service_url,
            probe=_http_probe(s.dq_service_url) if s.dq_service_url else None,
            missing=dq_missing_settings(s),
            impact="Quality bindings can't be rendered or pushed to jobs, and rule validation is refused (503).",
        ),
        _check(
            key="ucs", name="Unified Catalog (UCS)", category="platform",
            description="Governance: catalog discovery and grants for shared connections.",
            endpoint=s.ucs_service_url,
            probe=_http_probe(s.ucs_service_url, "/health") if s.ucs_service_url else None,
            missing=([] if s.ucs_service_url else ["UCS_SERVICE_URL"])
                    + ([] if s.ucs_gateway_secret else ["UCS_GATEWAY_SECRET"]),
            impact="Catalog browsing and using connections created by other users are blocked.",
        ),
        _check(
            key="notebooks", name="Notebook kernels", category="engine",
            description="Python kernels for notebooks.",
            endpoint=None if s.local_python_kernel_enabled else s.jupyterhub_url,
            probe=(
                (lambda: asyncio.sleep(0, result="Local Python kernel (dev)"))
                if s.local_python_kernel_enabled
                else (_http_probe(s.jupyterhub_url, "/hub/health") if s.jupyterhub_admin_token else None)
            ),
            missing=[] if s.local_python_kernel_enabled or s.jupyterhub_admin_token else ["JUPYTERHUB_ADMIN_TOKEN"],
            impact="Notebooks can't start a kernel.", config_only=s.local_python_kernel_enabled,
        ),
        _check(
            key="codegen", name=codegen_name, category="engine",
            description=codegen_desc,
            endpoint=None if external else s.codegen_gateway_url,
            probe=_codegen_gateway_probe if (external or s.codegen_gateway_url) else None,
            missing=[] if (external or s.codegen_gateway_url) else ["CODEGEN_GATEWAY_URL"],
            impact="The AI assistant can't generate code.",
        ),
        _check(
            key="database", name="Workspace database", category="platform",
            description="Stores workspaces, files metadata, connections and history.",
            endpoint=None, probe=_db_probe, missing=[], impact="The Workspace can't function.",
        ),
        _check(
            key="redis", name="Redis", category="platform",
            description="Live execution updates and WebSocket fan-out.",
            endpoint=None, probe=_redis_probe, missing=[], impact="Live query progress and results streaming stop.",
        ),
        _check(
            key="storage", name=f"Object storage ({s.storage_backend})", category="platform",
            description=f"File contents, bucket {s.storage_bucket}.",
            endpoint=s.storage_endpoint_url, probe=_storage_probe, missing=[],
            impact="Files can't be opened or saved.",
        ),
    ]
    engines = list(await asyncio.gather(*checks))

    # Dev-only secrets store is fine locally but must be flagged everywhere else.
    if s.secrets_backend == "local":
        engines.append(EngineStatus(
            key="secrets", name="Secrets backend (local file)", category="platform",
            description="Where connection and Git credentials are kept.",
            status="connected" if s.environment == "dev" else "not_configured",
            detail="Dev-only store — use a cloud secrets manager outside dev.",
            missing=[] if s.environment == "dev" else ["SECRETS_BACKEND"],
            impact=None if s.environment == "dev" else "Credentials aren't stored securely.",
        ))

    if not user.has(SETTINGS_MANAGE):
        engines = [e.model_copy(update={"endpoint": None}) for e in engines]
    return EnginesOut(engines=engines, checked_at=time.time())


# ── Codegen agent (which model answers the ⌘K assistant) ───────────────────────────────────────────────────────────────────

def _require_admin(user) -> None:
    if not user.has(SETTINGS_MANAGE):
        raise forbidden(f"missing permission {SETTINGS_MANAGE}: changing the codegen agent needs it")


async def _agent_out(user, provider: str, key: str | None, ok: bool, message: str | None) -> CodegenAgentConfigOut:
    from app.services.codegen_provider import EXTERNAL_PROVIDERS, LABELS, available_providers, mask_api_key, model_for

    admin = user.has(SETTINGS_MANAGE)
    status: Status = "connected" if ok else ("unreachable" if (key or provider not in EXTERNAL_PROVIDERS) else "not_configured")
    if status == "not_configured" and not get_settings().codegen_gateway_enabled:
        message = "Choose Anthropic or OpenAI and enter an API key to use the AI assistant."
    return CodegenAgentConfigOut(
        provider=provider, label=LABELS[provider], model=model_for(provider) or get_settings().codegen_model_generation,
        masked_key=mask_api_key(key) if admin else None, has_key=bool(key), can_edit=admin, status=status, message=message,
        available_providers=available_providers())


@router.get("/codegen/agent", response_model=CodegenAgentConfigOut)
async def get_codegen_agent(user: RequireSettingsEdit) -> CodegenAgentConfigOut:
    """The agent in use and whether it answers right now. The key is never returned; holders of workspace:settings:manage see its masked form."""
    from app.services.codegen_provider import CodegenConfigError, get_codegen_agent_config, verify_provider_key

    try:
        cfg = await get_codegen_agent_config()
    except CodegenConfigError as e:
        from app.services.codegen_provider import default_provider
        return await _agent_out(user, default_provider(), None, False, str(e))
    ok, msg = await verify_provider_key(cfg["provider"], cfg.get("api_key"))
    return await _agent_out(user, cfg["provider"], cfg.get("api_key"), ok, msg)


@router.post("/codegen/verify", response_model=VerifyKeyOut)
async def verify_codegen_key_endpoint(body: VerifyKeyIn, user: RequireSettingsManage) -> VerifyKeyOut:
    """Check a key with its provider without saving it (empty key: the one already saved for this provider)."""
    from app.services.codegen_provider import (
        EXTERNAL_PROVIDERS,
        CodegenConfigError,
        get_codegen_agent_config,
        verify_provider_key,
    )

    _require_admin(user)
    key = body.api_key.strip() if body.api_key else None
    if not key and body.provider in EXTERNAL_PROVIDERS:
        try:
            saved = await get_codegen_agent_config()
        except CodegenConfigError as e:
            return VerifyKeyOut(success=False, message=str(e))
        if saved["provider"] == body.provider:
            key = saved.get("api_key")
    ok, msg = await verify_provider_key(body.provider, key)
    return VerifyKeyOut(success=ok, message=msg)


@router.put("/codegen/agent", response_model=CodegenAgentConfigOut)
async def update_codegen_agent(body: UpdateAgentIn, user: RequireSettingsManage, db: DB, request: Request) -> CodegenAgentConfigOut:
    """Verify with the provider, then save: from now on every ⌘K answer comes from this agent. Needs workspace:settings:manage and is audited (the audit
    record names the provider, never the key). A key is required for OpenAI/Anthropic unless the same provider is already saved."""
    from app.services.codegen_provider import (
        EXTERNAL_PROVIDERS,
        CodegenConfigError,
        get_codegen_agent_config,
        save_codegen_agent_config,
        verify_provider_key,
    )

    _require_admin(user)
    from app.services.codegen_provider import available_providers
    if body.provider not in available_providers():
        raise validation_failed("provider", "This agent is not available here: choose Anthropic or OpenAI and enter an API key")
    key = body.api_key.strip() if body.api_key else None
    previous = "unknown"
    try:
        saved = await get_codegen_agent_config()
        previous = saved["provider"]
        if body.provider in EXTERNAL_PROVIDERS and not key and saved["provider"] == body.provider:
            key = saved.get("api_key")
    except CodegenConfigError:
        pass
    if body.provider in EXTERNAL_PROVIDERS and not key:
        raise validation_failed("api_key", "Connection failed: API key is required")
    ok, msg = await verify_provider_key(body.provider, key)
    if not ok:
        raise validation_failed("api_key" if body.provider in EXTERNAL_PROVIDERS else "provider", msg)
    try:
        await save_codegen_agent_config(body.provider, key)
    except Exception:  # noqa: BLE001 — the secrets store refused: say so plainly, with no detail that could hold the key
        raise WorkspaceError(503, "SECRETS_UNAVAILABLE", "The setting could not be saved to the secrets store.", {"resource": "codegen_agent"}) from None

    user_row = await WorkspaceService(db).ensure_user(user)
    await AuditService(db).record(
        user_id=user_row.id, action="codegen.agent_updated", resource_type="codegen_agent",
        ip_address=request.client.host if request.client else None,
        metadata={"provider": body.provider, "previous_provider": previous, "key_saved": body.provider in EXTERNAL_PROVIDERS},
    )
    return await _agent_out(user, body.provider, key if body.provider in EXTERNAL_PROVIDERS else None, True, msg)

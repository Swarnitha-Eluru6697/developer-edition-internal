"""Data Quality service proxy.

Same pattern as dbt_proxy.py and flink_proxy.py — the SPA talks to a
single origin (workspace service), and this router forwards calls to the
inaiera-data-quality ClusterIP service.

Proxy scope:
  GET    /api/v1/dq/rules                            → list rules
  POST   /api/v1/dq/rules                            → create rule
  GET    /api/v1/dq/rules/{id}                       → get rule
  PATCH  /api/v1/dq/rules/{id}                       → update rule
  DELETE /api/v1/dq/rules/{id}                       → delete rule

  GET    /api/v1/dq/rulesets                         → list rulesets
  POST   /api/v1/dq/rulesets                         → create ruleset
  GET    /api/v1/dq/rulesets/{id}                    → get ruleset (with items)
  PATCH  /api/v1/dq/rulesets/{id}                    → update ruleset
  DELETE /api/v1/dq/rulesets/{id}                    → delete ruleset
  GET    /api/v1/dq/rulesets/{id}/items              → list items
  POST   /api/v1/dq/rulesets/{id}/items              → add item
  PATCH  /api/v1/dq/rulesets/{id}/items/{item_id}    → update item
  DELETE /api/v1/dq/rulesets/{id}/items/{item_id}    → remove item

  GET    /api/v1/dq/bindings                         → list bindings
  POST   /api/v1/dq/bindings                         → create binding
  GET    /api/v1/dq/bindings/{id}                    → get binding
  DELETE /api/v1/dq/bindings/{id}                    → delete binding

  GET    /api/v1/dq/render/{binding_id}              → get rendered artifacts
  POST   /api/v1/dq/render/{binding_id}/refresh      → force re-render

The /render/validate/rule endpoint takes the internal service token — so
/api/v1/dq/validate is handled server-side here exactly like the FlinkSQL
/validate route (attach the service token, never forward a user JWT there).

Contract: routes declare a ``response_model`` (app/schemas/proxy.py) so the
OpenAPI document describes the DQ service's real shapes; bodies are still
forwarded untouched (a returned ``Response`` skips response validation), so
an upstream addition can never fail the proxy (WS-FE-014).

Auth (X-001): the user's own credential is NEVER forwarded. Each call carries a short-lived DELEGATED token Workspace
mints (HS256, aud "dataquality", sub = the gateway-authenticated user with their roles/org/email, act.sub "workspace",
the DQ scope(s) the route needs, single use for writes). DQ verifies it with its own DELEGATION_JWT_SECRET, builds the
user's permissions from their roles LIMITED to that scope, and enforces org scoping from the token. A token minted for
Workspace means nothing to DQ, and DQ never sees the browser's session.
"""
from __future__ import annotations

from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.upstream import upstream_guard, upstream_timeout
from app.core.service_tokens import (
    DQ_AUDIENCE,
    DQ_RULE_WRITE_SCOPES,
    DQ_SCOPE_APPLY,
    DQ_SCOPE_BIND,
    DQ_SCOPE_READ,
    DQ_SCOPE_RULES_VALIDATE,
    DQ_SCOPE_RULES_WRITE,
    DQ_SCOPE_SYNC,
    delegated_authorization,
    post_with_service_token,
)
from app.core.errors import validation_failed
from app.core.rbac import (
    RequireDqExecute,
    RequireDqPublish,
    RequireDqRead,
    RequireDqRuleCreate,
    RequireDqRuleEdit,
)
from app.schemas.proxy import (
    DqApplyResult,
    DqBinding,
    DqBindingCreate,
    DqRender,
    DqRule,
    DqRuleset,
    DqRulesetCapability,
    DqRulesetItem,
    DqRulesetSummary,
    DqUcsSyncReport,
    DqValidateRuleResponse,
    json_body,
)

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/dq", tags=["dq-proxy"])



async def _proxy(request: Request, path: str, user, scope: str) -> Response:
    """Forward the request to DQ as `user` with a delegated token limited to `scope` (X-001)."""
    settings = get_settings()
    url = f"{settings.dq_service_url}/api/v1/{path.lstrip('/')}"
    body = await request.body()
    headers = {
        "Authorization": delegated_authorization(user, audience=DQ_AUDIENCE, scope=scope),
        "Content-Type": request.headers.get("content-type", "application/json"),
        "X-Forwarded-For": request.client.host if request.client else "unknown",
    }
    with upstream_guard("the Data Quality service"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            upstream = await client.request(
                method=request.method, url=url, content=body,
                headers=headers, params=dict(request.query_params),
            )
    log.info("dq_proxy", method=request.method, path=path, status=upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ── Rules ──────────────────────────────────────────────────────────────────

@router.get("/rules", response_model=list[DqRule])
async def list_rules(request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, "rules", user, DQ_SCOPE_READ)

@router.post("/rules", status_code=status.HTTP_201_CREATED, response_model=DqRule)
async def create_rule(request: Request, user: RequireDqRuleCreate) -> Response:
    return await _proxy(request, "rules", user, DQ_RULE_WRITE_SCOPES)

@router.get("/rules/{rule_id}", response_model=DqRule)
async def get_rule(rule_id: UUID, request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, f"rules/{rule_id}", user, DQ_SCOPE_READ)

@router.patch("/rules/{rule_id}", response_model=DqRule)
async def update_rule(rule_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rules/{rule_id}", user, DQ_RULE_WRITE_SCOPES)

@router.post("/rules/{rule_id}/approve", response_model=DqRule)
async def approve_rule(rule_id: UUID, request: Request, user: RequireDqPublish) -> Response:
    """Approve a custom_sql rule (DQ-SEC-006): custom SQL is privileged and
    doesn't render until approved. DQ re-checks dq.custom_sql permission on
    the user's delegated token (the dq.custom_sql.write scope is included, the user's role decides)."""
    return await _proxy(request, f"rules/{rule_id}/approve", user, DQ_RULE_WRITE_SCOPES)

@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rules/{rule_id}", user, DQ_RULE_WRITE_SCOPES)


# ── Rulesets ───────────────────────────────────────────────────────────────

@router.get("/rulesets", response_model=list[DqRulesetSummary])
async def list_rulesets(request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, "rulesets", user, DQ_SCOPE_READ)

@router.post("/rulesets", status_code=status.HTTP_201_CREATED, response_model=DqRuleset)
async def create_ruleset(request: Request, user: RequireDqRuleCreate) -> Response:
    return await _proxy(request, "rulesets", user, DQ_SCOPE_RULES_WRITE)

@router.get("/rulesets/{ruleset_id}", response_model=DqRuleset)
async def get_ruleset(ruleset_id: UUID, request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}", user, DQ_SCOPE_READ)

@router.patch("/rulesets/{ruleset_id}", response_model=DqRuleset)
async def update_ruleset(ruleset_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}", user, DQ_SCOPE_RULES_WRITE)

@router.delete("/rulesets/{ruleset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ruleset(ruleset_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}", user, DQ_SCOPE_RULES_WRITE)

@router.get("/rulesets/{ruleset_id}/capability", response_model=DqRulesetCapability)
async def ruleset_capability(ruleset_id: UUID, request: Request, user: RequireDqRead) -> Response:
    """Per-rule engine support (supported/unsupported/degraded) before binding (DQ-CAP-007)."""
    return await _proxy(request, f"rulesets/{ruleset_id}/capability", user, DQ_SCOPE_READ)

@router.get("/rulesets/{ruleset_id}/items", response_model=list[DqRulesetItem])
async def list_items(ruleset_id: UUID, request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}/items", user, DQ_SCOPE_READ)

@router.post("/rulesets/{ruleset_id}/items", status_code=status.HTTP_201_CREATED, response_model=DqRulesetItem)
async def add_item(ruleset_id: UUID, request: Request, user: RequireDqRuleCreate) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}/items", user, DQ_SCOPE_RULES_WRITE)

@router.patch("/rulesets/{ruleset_id}/items/{item_id}", response_model=DqRulesetItem)
async def update_item(ruleset_id: UUID, item_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}/items/{item_id}", user, DQ_SCOPE_RULES_WRITE)

@router.delete("/rulesets/{ruleset_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(ruleset_id: UUID, item_id: UUID, request: Request, user: RequireDqRuleEdit) -> Response:
    return await _proxy(request, f"rulesets/{ruleset_id}/items/{item_id}", user, DQ_SCOPE_RULES_WRITE)


# ── Bindings ───────────────────────────────────────────────────────────────

@router.get("/bindings", response_model=list[DqBinding])
async def list_bindings(request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, "bindings", user, DQ_SCOPE_READ)

@router.post(
    "/bindings", status_code=status.HTTP_201_CREATED,
    response_model=DqBinding, openapi_extra=json_body(DqBindingCreate),
)
async def create_binding(request: Request, user: RequireDqPublish) -> Response:
    return await _proxy(request, "bindings", user, DQ_SCOPE_BIND)

@router.get("/bindings/{binding_id}", response_model=DqBinding)
async def get_binding(binding_id: UUID, request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, f"bindings/{binding_id}", user, DQ_SCOPE_READ)

@router.delete("/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(binding_id: UUID, request: Request, user: RequireDqPublish) -> Response:
    return await _proxy(request, f"bindings/{binding_id}", user, DQ_SCOPE_BIND)


@router.post("/bindings/{binding_id}/resolve", response_model=DqBinding)
async def resolve_binding(binding_id: UUID, request: Request, user: RequireDqPublish) -> Response:
    """Re-resolve a binding's job (via its runner) and dataset (via UCS) —
    required before apply for bindings created before resolution existed
    (DQ-BIND-003). DQ resolves the job as the caller, with a delegated token it mints itself."""
    return await _proxy(request, f"bindings/{binding_id}/resolve", user, DQ_SCOPE_BIND)


@router.post("/bindings/{binding_id}/apply", response_model=DqApplyResult)
async def apply_binding(binding_id: UUID, request: Request, user: RequireDqPublish) -> Response:
    """Push the rendered artifact to the dbt or FlinkSQL runner.

    _proxy sends the user's delegated token; DQ then mints its own delegated token for
    the FlinkSQL runner, which creates the quality job under the caller's org and RBAC.
    """
    return await _proxy(request, f"bindings/{binding_id}/apply", user, DQ_SCOPE_APPLY)


# ── Render ─────────────────────────────────────────────────────────────────

@router.get("/render/{binding_id}", response_model=DqRender)
async def get_render(binding_id: UUID, request: Request, user: RequireDqRead) -> Response:
    return await _proxy(request, f"render/{binding_id}", user, DQ_SCOPE_READ)

@router.post("/render/{binding_id}/refresh", response_model=DqRender)
async def refresh_render(binding_id: UUID, request: Request, user: RequireDqExecute) -> Response:
    return await _proxy(request, f"render/{binding_id}/refresh", user, DQ_SCOPE_READ)


# ── Sync ───────────────────────────────────────────────────────────────────

@router.post("/sync/ucs", response_model=DqUcsSyncReport)
async def sync_ucs(request: Request, user: RequireDqPublish) -> Response:
    """Register bound rules as quality checks in the Unified Catalog.

    The DQ service holds the UCS admin credential; the browser never sees it.
    Query params (binding_id) are forwarded by _proxy.
    """
    return await _proxy(request, "sync/ucs", user, DQ_SCOPE_SYNC)


# ── Internal validate (server-side token, not forwarded from browser) ────

class DqValidateRule(BaseModel):
    rule_type: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    severity: str = "ERROR"
    parameters: dict = Field(default_factory=dict)
    column_name: str = ""


@router.post("/validate/rule", response_model=DqValidateRuleResponse)
async def validate_rule(payload: DqValidateRule, user: RequireDqExecute) -> Response:
    """Validate a rule definition via the DQ service.
    The DQ service's /render/validate/rule endpoint requires a scoped service
    JWT (app/core/service_tokens.py) — minted here server-side, never forwarded
    from the browser.
    """
    settings = get_settings()
    if not (settings.dq_service_jwt_secret or settings.dq_internal_token):
        raise validation_failed("rule_type",
                                "DQ rule validation is not configured on this deployment")
    with upstream_guard("the Data Quality service"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            upstream = await post_with_service_token(
                client, f"{settings.dq_service_url}/api/v1/render/validate/rule",
                json=payload.model_dump(),
                signing_secret=settings.dq_service_jwt_secret,
                audience=DQ_AUDIENCE, scope=DQ_SCOPE_RULES_VALIDATE,
                legacy_token=settings.dq_internal_token,
            )
    log.info("dq_validate_rule", status=upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )

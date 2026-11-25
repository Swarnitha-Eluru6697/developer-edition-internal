"""UCS client (contract v2, behind UCS_CONTRACT_V2_ENABLED).

Every call carries a scoped service JWT (aud settings.ucs_audience, signed
with UCS_SERVICE_JWT_SECRET, org-bound):

  resolve_dataset   POST {ucs_dataset_resolve_path}   scope ucs.datasets.read
                    body {fqn} -> {fqn, object_type, org_id}
  authorize         POST {ucs_authz_check_path}       scope ucs.authz.check
                    body {principal, groups, action, object}
                    token claim on_behalf_of=<principal> -> {decision, reason}
  register_checks   POST {ucs_check_registrations_path} scope ucs.quality.checks.write
                    body QualityCheckRegistration, Idempotency-Key
  post_audit_event  POST {ucs_audit_events_path}      scope ucs.audit.events.write
                    body GovernanceAuditEvent, Idempotency-Key

UCS today implements none of these with service-JWT auth (its profile /
authz routes trust gateway identity headers + a shared gateway secret;
check registration needs a platform-admin OIDC bearer; there is no audit
ingest or QualityRun API). That is a cross-service follow-up; until it
lands this client is disabled and callers fail closed (binding) or keep
outbox rows durably pending (registrations, audit).
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.auth import Principal
from app.core.config import Settings, get_settings
from app.core.service_auth import SCOPE_UCS_AUDIT_WRITE, SCOPE_UCS_CHECKS_WRITE, mint_service_token
from app.integrations.common import DEFAULT_TIMEOUT, UpstreamError, raise_for, safe_json

SERVICE = "UCS"
SCOPE_UCS_DATASETS_READ = "ucs.datasets.read"
SCOPE_UCS_AUTHZ_CHECK = "ucs.authz.check"
DATASET_RESOLVE_PATH = "/api/v1/datasets/resolve"
AUTHZ_CHECK_PATH = "/api/v1/authz/check"


class UcsContractUnavailable(Exception):
    pass


class UcsClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def base(self) -> str:
        return (self.settings.ucs_url or "").rstrip("/")

    def _require_v2(self) -> None:
        if not self.settings.ucs_enabled:
            raise UcsContractUnavailable("UCS integration is disabled")
        if not self.settings.ucs_contract_v2_enabled:
            raise UcsContractUnavailable(
                "UCS service-JWT contract (v2) is not enabled (UCS_CONTRACT_V2_ENABLED=false)")

    def _token(self, scope: str, org_id: str, extra: dict | None = None) -> str:
        return mint_service_token("ucs", scope=scope, org_id=org_id, extra_claims=extra,
                                  settings=self.settings)

    async def _post(self, path: str, body: dict, token: str, what: str,
                    idempotency_key: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                return await c.post(f"{self.base}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"{what}: unreachable: {e}")

    async def resolve_dataset(self, fqn: str, *, principal: Principal) -> dict[str, Any]:
        self._require_v2()
        r = await self._post(DATASET_RESOLVE_PATH, {"fqn": fqn},
                             self._token(SCOPE_UCS_DATASETS_READ, principal.org_id,
                                         {"on_behalf_of": principal.email or principal.user_id}),
                             f"dataset {fqn}")
        raise_for(SERVICE, r, f"dataset {fqn}")
        body = safe_json(r)
        if not isinstance(body, dict):
            raise UpstreamError(SERVICE, "dataset resolve returned no JSON object")
        return body

    async def authorize(self, *, principal: Principal, action: str, obj: str) -> dict[str, Any]:
        self._require_v2()
        who = principal.email or principal.user_id
        r = await self._post(AUTHZ_CHECK_PATH,
                             {"principal": who, "groups": list(principal.roles), "action": action,
                              "object": obj},
                             self._token(SCOPE_UCS_AUTHZ_CHECK, principal.org_id,
                                         {"on_behalf_of": who}), "authz check")
        raise_for(SERVICE, r, "authz check")
        body = safe_json(r)
        return body if isinstance(body, dict) else {}

    async def register_check(self, registration: dict, *, org_id: str,
                             idempotency_key: str) -> httpx.Response:
        self._require_v2()
        return await self._post(self.settings.ucs_check_registrations_path, registration,
                                self._token(SCOPE_UCS_CHECKS_WRITE, org_id), "check registration",
                                idempotency_key)

    async def post_audit_event(self, event: dict, *, org_id: str,
                               idempotency_key: str) -> httpx.Response:
        self._require_v2()
        return await self._post(self.settings.ucs_audit_events_path, event,
                                self._token(SCOPE_UCS_AUDIT_WRITE, org_id), "audit event",
                                idempotency_key)

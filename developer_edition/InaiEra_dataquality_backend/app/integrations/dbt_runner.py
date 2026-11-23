"""dbt runner client.

get_project   GET  /api/v1/projects/{id}                (caller's bearer; the
              runner scopes the lookup to the caller's org)
apply         POST /api/v1/projects/{id}/schema-tests   (scoped service JWT)

The apply token matches the runner's verifier (inaiera-dbt-runner
app/core/service_auth.require_service_scope): HS256 signed with the
runner's SERVICE_JWT_SECRET (DQ's DBTRUNNER_SERVICE_JWT_SECRET), aud
"dbtrunner", scope "dbt.schema-tests.write", iss/sub "dataquality",
iat/exp (<= 300s), single-use jti, org_id, and project_id equal to the
path. It travels in X-Internal-Service-Token (the header the runner reads);
the request also carries Idempotency-Key, and the body is the
ApplyDqArtifact shape.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.service_auth import SCOPE_DBT_SCHEMA_TESTS_WRITE, mint_service_token
from app.integrations.common import DEFAULT_TIMEOUT, UpstreamError, raise_for, safe_json

SERVICE = "dbt runner"


class DbtRunnerClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def base(self) -> str:
        return (self.settings.dbtrunner_url or "").rstrip("/")

    async def get_project(self, project_id: uuid.UUID, bearer: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                r = await c.get(f"{self.base}/api/v1/projects/{project_id}",
                                headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"unreachable: {e}")
        raise_for(SERVICE, r, f"project {project_id}")
        body = safe_json(r)
        if not isinstance(body, dict):
            raise UpstreamError(SERVICE, "project lookup returned no JSON object")
        return body

    async def apply_schema_tests(self, *, org_id: str, project_id: uuid.UUID,
                                 artifact: dict[str, Any], idempotency_key: str) -> httpx.Response:
        token = mint_service_token(
            "dbtrunner", scope=SCOPE_DBT_SCHEMA_TESTS_WRITE, org_id=org_id,
            extra_claims={"project_id": str(project_id)}, settings=self.settings)
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                return await c.post(
                    f"{self.base}/api/v1/projects/{project_id}/schema-tests", json=artifact,
                    headers={"X-Internal-Service-Token": token, "Idempotency-Key": idempotency_key})
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"unreachable: {e}")

"""FlinkSQL runner client.

get_job        GET  /api/v1/jobs/{id}   (caller's bearer — org + RBAC scoped)
structured_plan POST /api/v1/validate   (scoped JWT: aud "flinksql-runner",
               scope "flink.jobs.validate", org_id) -> the runner's
               StructuredJobPlan (`plan`) + validity. DQ never parses SQL.
upsert_quality_job  POST /api/v1/jobs (409 -> PATCH)  (caller's bearer, so the
               quality job is created under the caller's org and RBAC)
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.service_auth import SCOPE_FLINK_VALIDATE, mint_service_token
from app.integrations.common import DEFAULT_TIMEOUT, UpstreamError, raise_for, safe_json
from app.renderers.plan import StructuredJobPlan, parse_plan

SERVICE = "FlinkSQL runner"


class FlinkRunnerClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def base(self) -> str:
        return (self.settings.flinksql_runner_url or "").rstrip("/")

    async def get_job(self, job_id: uuid.UUID, bearer: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                r = await c.get(f"{self.base}/api/v1/jobs/{job_id}",
                                headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"unreachable: {e}")
        raise_for(SERVICE, r, f"job {job_id}")
        body = safe_json(r)
        if not isinstance(body, dict):
            raise UpstreamError(SERVICE, "job lookup returned no JSON object")
        return body

    async def validate(self, sql: str, *, org_id: str) -> tuple[bool, list[str], StructuredJobPlan]:
        token = mint_service_token("flinksql", scope=SCOPE_FLINK_VALIDATE, org_id=org_id,
                                   settings=self.settings)
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                r = await c.post(f"{self.base}/api/v1/validate", json={"sql": sql},
                                 headers={"X-Internal-Service-Token": token})
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"unreachable: {e}")
        raise_for(SERVICE, r, "validate")
        body = safe_json(r)
        if not isinstance(body, dict):
            raise UpstreamError(SERVICE, "validate returned no JSON object")
        plan = parse_plan(body.get("plan"))
        return bool(body.get("valid")), list(body.get("errors") or []), plan

    async def upsert_quality_job(self, *, bearer: str, name: str, payload: dict[str, Any]
                                 ) -> tuple[dict[str, Any], bool]:
        headers = {"Authorization": f"Bearer {bearer}"}
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as c:
                r = await c.post(f"{self.base}/api/v1/jobs", json=payload, headers=headers)
                if r.status_code == 409:
                    listing = await c.get(f"{self.base}/api/v1/jobs", headers=headers)
                    raise_for(SERVICE, listing, "job listing")
                    existing = next((j for j in (safe_json(listing) or [])
                                     if isinstance(j, dict) and j.get("name") == name), None)
                    if not existing:
                        raise UpstreamError(SERVICE, f"job {name!r} exists but is not readable")
                    if existing.get("job_kind") != "quality":
                        raise UpstreamError(SERVICE, f"job {name!r} exists and is not a quality job")
                    r = await c.patch(f"{self.base}/api/v1/jobs/{existing['id']}",
                                      json={"sql": payload["sql"],
                                            "description": payload.get("description")},
                                      headers=headers)
                    raise_for(SERVICE, r, "quality job update")
                    return (safe_json(r) or {}), False
        except httpx.HTTPError as e:
            raise UpstreamError(SERVICE, f"unreachable: {e}")
        raise_for(SERVICE, r, "quality job create")
        return (safe_json(r) or {}), True

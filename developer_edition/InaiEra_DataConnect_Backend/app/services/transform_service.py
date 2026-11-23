"""
Transform Policy Service — Postgres-authoritative.

CRUD is backed by Postgres (`dc_transform_policies` via
TransformPolicyRepository) — previously this went through the in-memory
store and never survived a restart. Every write opens its own session and
commits before returning, following the same pattern used throughout this
codebase (see app/services/connection_service.py, which was migrated the
same way in a prior pass).

SMT validation/config-generation (app.services.transform_policy) is unchanged —
it's a pure-function module with no CRUD and stays that way; routes call it
directly around the calls into this service.
"""

from typing import Any

import structlog
from fastapi import HTTPException, status

from app.db.session import AsyncSessionLocal
from app.models.transform import TransformPolicy
from app.repositories.dataconnect import PipelineRepository, TransformPolicyRepository

logger = structlog.get_logger(__name__)


def _policy_to_dict(row: TransformPolicy) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "description": row.description,
        "transforms": row.transforms,
        "origin": row.origin,
        "created_at": row.created_at,
    }


class TransformPolicyService:
    async def list_policies(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await TransformPolicyRepository(session).list_for_org(org_id)
            return [_policy_to_dict(row) for row in rows]

    async def get_policy(self, org_id: str, policy_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await TransformPolicyRepository(session).get_for_org(org_id, policy_id)
            return _policy_to_dict(row) if row else None

    async def get_or_404(self, org_id: str, policy_id: str) -> dict:
        item = await self.get_policy(org_id, policy_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transform policy not found")
        return item

    async def create_policy(self, org_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload["org_id"] = org_id
        async with AsyncSessionLocal() as session:
            row = await TransformPolicyRepository(session).create(payload)
            await session.commit()
            return _policy_to_dict(row)

    async def update_policy(self, org_id: str, policy_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload["org_id"] = org_id
        async with AsyncSessionLocal() as session:
            repo = TransformPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transform policy not found")
            updated_row = await repo.update(row, payload)
            await session.commit()
            result = _policy_to_dict(updated_row)
        await self._mark_users_of_policy_as_changed(org_id, policy_id)
        return result

    async def _mark_users_of_policy_as_changed(self, org_id: str, policy_id: str) -> None:
        """GAP-09: editing a policy changes the connector config of every pipeline that applies it, but touches
        neither their desired_state nor, directly, their stored hash. Bump each one's desired generation so a
        reconcile that already resolved the OLD transforms sees itself as stale instead of deploying them over the
        edit. Best-effort: the edit has been saved, and the next tick's config-hash comparison converges every
        connector regardless — this only closes the window in which a stale tick could act."""
        from app.services.runtime_reconciler import runtime_reconciler

        try:
            async with AsyncSessionLocal() as session:
                pipeline_ids = await PipelineRepository(session).ids_for_transform_policy(org_id, policy_id)
            for pipeline_id in pipeline_ids:
                await runtime_reconciler.bump_generation(pipeline_id)
        except Exception:
            logger.warning("transform_policy.bump_generations_failed", policy_id=policy_id, exc_info=True)

    async def delete_policy(self, org_id: str, policy_id: str) -> None:
        async with AsyncSessionLocal() as session:
            repo = TransformPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transform policy not found")
            await repo.delete(row)
            await session.commit()


transform_service = TransformPolicyService()

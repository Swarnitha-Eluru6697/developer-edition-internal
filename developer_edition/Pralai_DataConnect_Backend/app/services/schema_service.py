from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.schema import SchemaDriftApproval, SchemaVersion
from app.repositories.dataconnect import (
    SchemaDriftApprovalRepository,
    SchemaInferenceRunRepository,
    SchemaVersionRepository,
)
from app.services.schema_drift import approval_required, compare_schema_fields
from app.services.schema_fingerprint import compute_schema_fingerprint
from app.services.schema_inference import infer_schema

# Keys that belong to an approved contract field — everything else on an
# inferred SchemaField (confidence, null_only) is observational metadata
# about *this sample*, not part of the contract, and must never be written
# into a SchemaVersion.schema_definition. See strip_inference_metadata().
_CONTRACT_FIELD_KEYS = {"name", "type", "nullable", "primary_key", "fields", "items", "precision", "scale"}


def strip_inference_metadata(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the approved-contract-relevant keys from an inferred field
    list (recursing into nested `fields`/`items`) — so a caller who copies a
    SchemaInferResult's `fields` straight into a create-schema payload can
    never leak confidence/null_only into the approved contract."""
    stripped = []
    for field in fields:
        clean = {k: v for k, v in field.items() if k in _CONTRACT_FIELD_KEYS}
        if clean.get("fields"):
            clean["fields"] = strip_inference_metadata(clean["fields"])
        if clean.get("items"):
            clean["items"] = strip_inference_metadata([clean["items"]])[0]
        stripped.append(clean)
    return stripped


def _schema_to_dict(row: SchemaVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "domain": row.domain,
        "source_system": row.source_system,
        "entity": row.entity,
        "version": row.version,
        "schema_format": row.schema_format,
        "schema_definition": row.schema_definition,
        "compatibility": row.compatibility,
        "status": row.status,
        "created_at": row.created_at,
    }


def _drift_approval_to_dict(row: SchemaDriftApproval) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "schema_version_id": row.schema_version_id,
        "pipeline_id": row.pipeline_id,
        "run_id": row.run_id,
        "detected_fingerprint": row.detected_fingerprint,
        "current_fingerprint": row.current_fingerprint,
        "detected_at": row.detected_at,
        "observed_schema": row.observed_schema,
        "drift_events": row.drift_events,
        "status": row.status,
        "decision": row.decision,
        "resulting_schema_version_id": row.resulting_schema_version_id,
        "requested_by": row.requested_by,
        "reviewed_by": row.reviewed_by,
        "review_comment": row.review_comment,
        "reviewed_at": row.reviewed_at,
    }


class SchemaService:
    async def infer(self, org_id: str, records: list[dict], *, content_length: int | None = None) -> dict:
        """Bounded, deterministic inference (see app/services/schema_inference.py)
        that persists its own confidence/metadata as a SchemaInferenceRun —
        separate from any SchemaVersion a caller later creates from it."""
        if content_length is not None and content_length > settings.schema_infer_max_payload_bytes:
            raise ValueError(
                f"Schema inference request body exceeds the "
                f"{settings.schema_infer_max_payload_bytes}-byte limit "
                f"(Content-Length was {content_length})."
            )
        if len(records) > settings.schema_infer_max_records:
            raise ValueError(
                f"Schema inference is limited to {settings.schema_infer_max_records} records "
                f"per request (got {len(records)})."
            )

        result = infer_schema(records)

        async with AsyncSessionLocal() as session:
            repo = SchemaInferenceRunRepository(session)
            run = await repo.create(
                {
                    "org_id": org_id,
                    "sample_size": result["sample_size"],
                    "confidence": result["confidence"],
                    "fields": result["fields"],
                }
            )
            await session.commit()
            result["inference_run_id"] = run.id
        return result

    async def list_schemas(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await SchemaVersionRepository(session).list_for_org(org_id)
            return [_schema_to_dict(row) for row in rows]

    async def get_schema(self, org_id: str, schema_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await SchemaVersionRepository(session).get_for_org(org_id, schema_id)
            return _schema_to_dict(row) if row else None

    async def get_by_contract(
        self, org_id: str, domain: str, source_system: str, entity: str, version: str
    ) -> dict | None:
        """Look up an already-registered schema version for this exact
        (domain, source_system, entity, version) contract — the same natural
        key dc_schema_versions uniquely constrains on. Callers that are about
        to register a schema for a contract (e.g. pipeline auto-introspection)
        must check this first: a prior pipeline against the same contract may
        already have registered one, and INSERTing another unconditionally
        would just hit that unique constraint."""
        async with AsyncSessionLocal() as session:
            row = await SchemaVersionRepository(session).find_contract(
                org_id, domain, source_system, entity, version
            )
            return _schema_to_dict(row) if row else None

    async def create_schema(self, org_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload["org_id"] = org_id
        definition = payload.get("schema_definition")
        if definition and definition.get("fields"):
            # Defensive: a caller may have copied a SchemaInferResult's
            # `fields` straight into this payload — never let confidence/
            # null_only reach the approved contract.
            definition["fields"] = strip_inference_metadata(definition["fields"])
        async with AsyncSessionLocal() as session:
            row = await SchemaVersionRepository(session).create(payload)
            await session.commit()
            return _schema_to_dict(row)

    def validate_compatibility(self, schema: dict) -> dict:
        return {"valid": True, "errors": []}

    def detect_drift(self, approved_schema: dict, observed_records: list[dict]) -> dict:
        # Drift checks run automatically and often — persisting a
        # SchemaInferenceRun on every check would be noise, not audit value,
        # so this calls the pure inference function directly rather than
        # self.infer() (which is for the explicit, user-invoked /infer call).
        observed_schema = infer_schema(observed_records)
        approved_fields = approved_schema.get("schema_definition", {}).get("fields", [])
        # Sample records cannot show which field is a primary key — inference always says "false" — so a field that is in the contract keeps the
        # contract's own answer. Without this, every contract with a primary key reported "primary key changed" (blocking) on records that match it.
        approved_by_name = {f.get("name"): f for f in approved_fields}
        for field in observed_schema["fields"]:
            known = approved_by_name.get(field.get("name"))
            if known is not None:
                field["primary_key"] = bool(known.get("primary_key"))
        drift = compare_schema_fields(approved_fields, observed_schema["fields"])
        drift["approval_required"] = approval_required(drift["events"])
        drift["observed_schema"] = observed_schema
        # Fingerprinted and timestamped here (not only when an approval row
        # is created) so a caller can see exactly what was compared even for
        # a compatible check that never raises an approval.
        drift["detected_fingerprint"] = compute_schema_fingerprint(observed_schema["fields"])
        drift["current_fingerprint"] = compute_schema_fingerprint(approved_fields)
        drift["detected_at"] = datetime.now(UTC)
        return drift

    def drift_events(self, schema: dict) -> list[dict]:
        return schema.get("drift_events", [])

    async def create_drift_approval(self, org_id: str, payload: dict) -> dict:
        """Persists a drift event/approval row to Postgres (previously this
        went through the in-memory store and never survived a restart —
        see SchemaDriftApproval's docstring for why this table is the
        durable record operational traceability needs). Fingerprints are
        computed here so every creation path (the automatic detect-drift
        route and the manual POST /drift-approvals endpoint) gets them
        consistently, not just callers that happen to pass them in."""
        payload = dict(payload)
        payload["org_id"] = org_id
        payload.setdefault("status", "pending")
        observed_fields = (payload.get("observed_schema") or {}).get("fields", [])
        payload.setdefault("detected_fingerprint", compute_schema_fingerprint(observed_fields))
        approved_schema = await self.get_schema(org_id, payload["schema_version_id"])
        approved_fields = (approved_schema or {}).get("schema_definition", {}).get("fields", [])
        payload.setdefault("current_fingerprint", compute_schema_fingerprint(approved_fields))

        async with AsyncSessionLocal() as session:
            row = await SchemaDriftApprovalRepository(session).create(payload)
            await session.commit()
            return _drift_approval_to_dict(row)

    async def list_drift_approvals(self, org_id: str, status: str | None = None) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await SchemaDriftApprovalRepository(session).list_for_org_with_status(org_id, status)
            return [_drift_approval_to_dict(row) for row in rows]

    async def get_drift_approval(self, org_id: str, approval_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await SchemaDriftApprovalRepository(session).get_for_org(org_id, approval_id)
            return _drift_approval_to_dict(row) if row else None

    async def _decide_drift(
        self,
        org_id: str,
        approval_id: str,
        *,
        status: str,
        decision: str,
        reviewer: str,
        comment: str | None,
        resulting_schema_version_id: str | None,
    ) -> dict | None:
        async with AsyncSessionLocal() as session:
            repo = SchemaDriftApprovalRepository(session)
            row = await repo.get_for_org(org_id, approval_id)
            if row is None:
                return None
            updated = await repo.update(
                row,
                {
                    "status": status,
                    "decision": decision,
                    "reviewed_by": reviewer,
                    "review_comment": comment,
                    "reviewed_at": datetime.now(UTC),
                    "resulting_schema_version_id": resulting_schema_version_id,
                },
            )
            await session.commit()
            return _drift_approval_to_dict(updated)

    async def approve_drift(
        self,
        org_id: str,
        approval_id: str,
        reviewer: str,
        comment: str | None,
        resulting_schema_version_id: str | None = None,
    ) -> dict | None:
        return await self._decide_drift(
            org_id,
            approval_id,
            status="approved",
            decision="approved",
            reviewer=reviewer,
            comment=comment,
            resulting_schema_version_id=resulting_schema_version_id,
        )

    async def reject_drift(self, org_id: str, approval_id: str, reviewer: str, comment: str | None) -> dict | None:
        return await self._decide_drift(
            org_id,
            approval_id,
            status="rejected",
            decision="rejected",
            reviewer=reviewer,
            comment=comment,
            resulting_schema_version_id=None,
        )


schema_service = SchemaService()

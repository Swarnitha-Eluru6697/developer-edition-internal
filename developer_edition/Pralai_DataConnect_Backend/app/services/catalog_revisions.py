"""
Connector-template revisioning.

REVISION_FIELDS are the fields that actually shape a deployed connector.
Changing any of them creates a new, immutable ConnectorTemplateRevision
rather than mutating the template in place, and advances the template's
current_revision_id to point at it — see the model docstrings in
app/models/catalog.py for why, and PipelineService._build_pipeline_row for
how a pipeline pins the revision it was built against.

Shared by app/services/catalog_service.py (the CRUD API, at request time)
and app/db/seed_templates.py (the one-time startup load/seed), so neither
duplicates — or drifts from — the other's revisioning logic.
"""

from typing import Any

from app.models.catalog import ConnectorTemplate, ConnectorTemplateRevision
from app.repositories.dataconnect import ConnectorTemplateRevisionRepository

REVISION_FIELDS = (
    "connector_class",
    "default_config",
    "default_transforms",
    "config_schema",
    "supported_transforms",
    # Plugin/version compatibility contract — also reproducibility-relevant:
    # a pipeline pinned to connector_version 2.4 must stay pinned to it even
    # after the template is qualified against 2.7, and its pinned snapshot
    # should keep recording which Kafka Connect/source versions 2.4 was
    # actually qualified against at the time.
    "connector_plugin",
    "connector_version",
    "supported_kafka_connect_versions",
    "supported_source_versions",
    "config_schema_version",
    "transform_compatibility",
    "migration_notes",
    # Preflight compatibility contract (GAP-05) — see app/models/catalog.py.
    "supported_modes",
    # FE-BUG-23 — see app/models/catalog.py.
    "supports_dlq",
    "supports_schema_discovery",
)


def revision_to_dict(row: ConnectorTemplateRevision) -> dict[str, Any]:
    return {
        "id": row.id, "template_id": row.template_id, "revision_number": row.revision_number,
        "connector_class": row.connector_class, "default_config": row.default_config or {},
        "default_transforms": row.default_transforms or [], "config_schema": row.config_schema or {},
        "supported_transforms": row.supported_transforms or [],
        "connector_plugin": row.connector_plugin,
        "connector_version": row.connector_version,
        "supported_kafka_connect_versions": row.supported_kafka_connect_versions or [],
        "supported_source_versions": row.supported_source_versions or [],
        "config_schema_version": row.config_schema_version,
        "transform_compatibility": row.transform_compatibility or {},
        "migration_notes": row.migration_notes,
        "supported_modes": row.supported_modes or [],
        "supports_dlq": row.supports_dlq if row.supports_dlq is not None else True,
        "supports_schema_discovery": row.supports_schema_discovery if row.supports_schema_discovery is not None else True,
        "created_at": row.created_at,
    }


async def create_revision(session, template: ConnectorTemplate) -> ConnectorTemplateRevision:
    """Snapshot template's current REVISION_FIELDS as a new, immutable
    revision, and advance the template's current_revision_id to it."""
    revisions = ConnectorTemplateRevisionRepository(session)
    next_number = await revisions.latest_revision_number(template.id) + 1
    revision = await revisions.create(
        {
            "template_id": template.id,
            "revision_number": next_number,
            "connector_class": template.connector_class,
            "default_config": template.default_config,
            "default_transforms": template.default_transforms,
            "config_schema": template.config_schema,
            "supported_transforms": template.supported_transforms,
            "connector_plugin": template.connector_plugin,
            "connector_version": template.connector_version,
            "supported_kafka_connect_versions": template.supported_kafka_connect_versions,
            "supported_source_versions": template.supported_source_versions,
            "config_schema_version": template.config_schema_version,
            "transform_compatibility": template.transform_compatibility,
            "migration_notes": template.migration_notes,
            "supported_modes": template.supported_modes,
            "supports_dlq": template.supports_dlq,
            "supports_schema_discovery": template.supports_schema_discovery,
        }
    )
    template.current_revision_id = revision.id
    await session.flush()
    return revision


def revision_fields_changed(template: ConnectorTemplate, payload: dict[str, Any]) -> bool:
    """True if `payload` would change any field that actually shapes a
    deployed connector — the trigger for creating a new revision. A payload
    that only touches display metadata (name, description, ...) doesn't."""
    for field in REVISION_FIELDS:
        if field in payload and payload[field] != getattr(template, field):
            return True
    return False

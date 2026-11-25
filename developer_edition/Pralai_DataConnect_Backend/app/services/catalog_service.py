"""
Connector-template catalog service.

PostgreSQL is authoritative for both reads and writes — list_templates()/
get_template() query the database directly on every call, and every write
goes through Postgres before returning. There is no in-memory cache: an
earlier version of this service kept a `store["connector_templates"]`
read-through cache synced from Postgres (see the old
app/services/catalog_store_sync.py), which meant reads and writes could
observe different data if the sync step was ever skipped or lagged. See
app/services/catalog_revisions.py for the revision-tracking logic shared
with app/db/seed_templates.py.
"""

from typing import Any

import structlog
from fastapi import HTTPException, status

from app.db.session import AsyncSessionLocal
from app.models.catalog import ConnectorTemplate
from app.repositories.dataconnect import (
    ConnectorTemplateRepository,
    ConnectorTemplateRevisionRepository,
)
from app.services.catalog_revisions import (
    create_revision,
    revision_fields_changed,
    revision_to_dict,
)

logger = structlog.get_logger(__name__)


def _template_to_dict(row: ConnectorTemplate) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "connector_type": row.connector_type,
        "category": row.category, "connector_class": row.connector_class,
        "runtime_pool_type": row.runtime_pool_type, "config_schema": row.config_schema or {},
        "default_config": row.default_config or {}, "default_transforms": row.default_transforms or [],
        "version": row.version, "is_active": row.is_active,
        "description": row.description, "instructions_url": row.instructions_url,
        "supported_transforms": row.supported_transforms or [],
        "current_revision_id": row.current_revision_id,
        "connector_plugin": row.connector_plugin,
        "connector_version": row.connector_version,
        "supported_kafka_connect_versions": row.supported_kafka_connect_versions or [],
        "supported_source_versions": row.supported_source_versions or [],
        "config_schema_version": row.config_schema_version,
        "transform_compatibility": row.transform_compatibility or {},
        "migration_notes": row.migration_notes,
        "supported_modes": row.supported_modes or [],
        # NOT NULL DEFAULT true columns — never actually None from a real
        # row, but coalesced the same way for a hand-built test double (e.g.
        # fake_postgres.create()) that didn't set them, so it reads as the
        # same "supported" default a real pre-FE-BUG-23 row gets.
        "supports_dlq": row.supports_dlq if row.supports_dlq is not None else True,
        "supports_schema_discovery": row.supports_schema_discovery if row.supports_schema_discovery is not None else True,
    }


class ConnectorCatalogService:
    async def list_templates(self) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await ConnectorTemplateRepository(session).list()
            return [_template_to_dict(r) for r in rows]

    async def get_template(self, template_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await ConnectorTemplateRepository(session).get(template_id)
            return _template_to_dict(row) if row else None

    async def create_template(self, payload: dict) -> dict:
        payload = dict(payload)
        async with AsyncSessionLocal() as session:
            repo = ConnectorTemplateRepository(session)
            template = await repo.create(payload)
            await create_revision(session, template)
            await session.commit()
            cached = _template_to_dict(template)
        logger.info("catalog.template_created", template_id=template.id)
        return cached

    async def update_template(self, template_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload.pop("id", None)  # the path param owns identity — never let the body move it
        async with AsyncSessionLocal() as session:
            repo = ConnectorTemplateRepository(session)
            template = await repo.get(template_id)
            if template is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector template not found")
            needs_new_revision = revision_fields_changed(template, payload)
            template = await repo.update(template, payload)
            if needs_new_revision:
                await create_revision(session, template)
            await session.commit()
            cached = _template_to_dict(template)
        logger.info(
            "catalog.template_updated", template_id=template_id, new_revision=needs_new_revision
        )
        return cached

    async def delete_template(self, template_id: str) -> None:
        async with AsyncSessionLocal() as session:
            repo = ConnectorTemplateRepository(session)
            template = await repo.get(template_id)
            if template is not None:
                await repo.delete(template)
                await session.commit()
        # Revision history is deliberately untouched — see ConnectorTemplateRevision's docstring.
        logger.info("catalog.template_deleted", template_id=template_id)

    async def list_revisions(self, template_id: str) -> list[dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            rows = await ConnectorTemplateRevisionRepository(session).list_for_template(template_id)
            return [revision_to_dict(r) for r in rows]


catalog_service = ConnectorCatalogService()

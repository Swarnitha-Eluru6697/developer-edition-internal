"""Connector Template model — reusable Kafka Connect connector blueprints."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class ConnectorTemplate(Base, TimestampMixin):
    """
    A blueprint for a Kafka Connect connector — the pipeline service uses it
    to generate a full connector config on demand.

    Templates are global (no org_id) — they are managed by platform admins
    and shared across all tenants.

    The fields that actually shape a deployed connector or its compatibility
    contract (connector_class, default_config, default_transforms,
    config_schema, supported_transforms, connector_plugin, connector_version,
    supported_kafka_connect_versions, supported_source_versions,
    config_schema_version, transform_compatibility, migration_notes) are NOT
    edited in place: every change to any of them is captured as a new,
    immutable ConnectorTemplateRevision (see below), and `current_revision_id`
    is only ever advanced to point at the newest one. A pipeline pins the
    revision it was built against at creation time (see
    PipelineService._build_pipeline_row) and keeps using that snapshot for
    every future deploy, redeploy, or reconcile — so editing a template here
    can never retroactively change what an existing pipeline deploys, and a
    pipeline built against connector plugin X 2.4 stays reproducible on X 2.4
    even after the template is qualified against a newer plugin release.
    """

    __tablename__ = "dc_connector_templates"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    connector_type: Mapped[str] = mapped_column(
        String(50),
        comment="source | sink",
    )
    category: Mapped[str] = mapped_column(
        String(100),
        comment="e.g. database_cdc, jdbc_source, file_source, api_source",
    )
    connector_class: Mapped[str] = mapped_column(
        String(500),
        comment="Fully-qualified Java class name, e.g. io.debezium.connector.postgresql.PostgresConnector",
    )
    runtime_pool_type: Mapped[str] = mapped_column(
        String(100),
        comment="Which worker pool to place connectors of this type, e.g. connect-cdc-pool",
    )
    config_schema: Mapped[dict] = mapped_column(
        JSONB,
        comment="JSON Schema describing required/optional connector config fields",
    )
    default_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    default_transforms: Mapped[list] = mapped_column(JSONB, default=list)
    version: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    supported_transforms: Mapped[list] = mapped_column(JSONB, default=list)
    current_revision_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("dc_connector_template_revisions.id"),
        nullable=True,
        comment="The revision currently in effect for NEW pipelines created against this template",
    )

    # Connector plugin/version compatibility contract — nullable because it
    # postdates the table and existing rows are backfilled by
    # app/db/seed_templates.py the same way current_revision_id is, not
    # because a production template should ever ship without it.
    connector_plugin: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="The distributable plugin/artifact this connector_class ships in, e.g. debezium-connector-postgresql",
    )
    connector_version: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="The connector plugin's own version, e.g. 2.7.0 — distinct from `version`, the template's own revision label",
    )
    supported_kafka_connect_versions: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        comment="Kafka Connect worker versions this connector plugin version is qualified against, e.g. ['3.6', '3.7', '3.8']",
    )
    supported_source_versions: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        comment="Versions of the external source/sink system this connector is qualified against, e.g. ['12','13','14','15','16'] for PostgreSQL; empty when not applicable",
    )
    config_schema_version: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Version tag for the shape of config_schema itself, bumped when its structure (not just values) changes",
    )
    transform_compatibility: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        comment="Per-SMT compatibility notes/constraints for this connector_version, e.g. {'ExtractNewRecordState': 'requires connector_version >= 1.9'}",
    )
    migration_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-text guidance for upgrading a deployed connector from the previous connector_version to this one",
    )
    supported_modes: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        comment=(
            "Pipeline `mode` values (cdc/snapshot/batch/api/stream/validation/replay) this "
            "connector actually supports — checked by app/services/deploy_preflight.py before "
            "deploy. Empty means unrestricted/undeclared, not 'supports nothing': existing "
            "templates predate this field and are treated as unrestricted until curated."
        ),
    )
    # FE-BUG-23: unlike supported_modes/supported_transforms above, DLQ
    # routing and server-side schema auto-discovery have no existing list to
    # derive a yes/no from, so each gets its own explicit flag. Both default
    # True — the behavior every template had before this field existed —
    # rather than False, so an uncurated template (a future one, or one a
    # test fixture builds without setting these) keeps working exactly as
    # today's do, instead of silently losing DLQ/schema-inference support.
    supports_dlq: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="Whether this connector's errors can be routed to a dead-letter topic — gates error_handling_policy=DLQ_AND_CONTINUE/THRESHOLD at pipeline-create time (pipeline_spec_validator.py) and the Pipeline Designer's DLQ options.",
    )
    supports_schema_discovery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="Whether PipelineService.autocreate_schema_from_source may attempt server-side schema introspection for this connector — false for sources with no introspectable structure (webhook, file, REST).",
    )


class ConnectorTemplateRevision(Base, UuidPk, TimestampMixin):
    """
    An immutable snapshot of one ConnectorTemplate's deploy-relevant fields.
    Rows here are append-only — never updated, never deleted (even when the
    parent template is), so a pipeline that pinned one keeps working exactly
    as it did the day it was created, no matter what happens to the
    template afterward.

    template_id is deliberately not a hard foreign key: deleting a template
    must not be blocked by (or cascade into deleting) its own revision
    history — the whole point is that this history outlives the template.
    """

    __tablename__ = "dc_connector_template_revisions"

    template_id: Mapped[str] = mapped_column(String(100), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    connector_class: Mapped[str] = mapped_column(String(500))
    default_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    default_transforms: Mapped[list] = mapped_column(JSONB, default=list)
    config_schema: Mapped[dict] = mapped_column(JSONB, default=dict)
    supported_transforms: Mapped[list] = mapped_column(JSONB, default=list)
    connector_plugin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connector_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    supported_kafka_connect_versions: Mapped[list] = mapped_column(JSONB, default=list)
    supported_source_versions: Mapped[list] = mapped_column(JSONB, default=list)
    config_schema_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    transform_compatibility: Mapped[dict] = mapped_column(JSONB, default=dict)
    migration_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    supported_modes: Mapped[list] = mapped_column(JSONB, default=list)
    supports_dlq: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_schema_discovery: Mapped[bool] = mapped_column(Boolean, default=True)

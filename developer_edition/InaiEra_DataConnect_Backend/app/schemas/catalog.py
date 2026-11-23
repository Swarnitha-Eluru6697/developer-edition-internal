from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.core.error_policies import is_sink
from app.schemas.capability import Availability


def _capabilities(
    *, supported_modes: list[str], supported_transforms: list[dict[str, Any]],
    supports_dlq: bool, supports_schema_discovery: bool,
) -> dict[str, bool]:
    """FE-BUG-23: the Pipeline Designer's single source of truth for which
    options a template actually supports — see Pipelines.tsx's PipelineForm.
    cdc/snapshot fold in supported_modes' existing "empty means
    unrestricted/undeclared" convention (see ConnectorTemplate's docstring
    in app/models/catalog.py) so an uncurated template's Designer keeps
    showing every mode, exactly like today, rather than hiding all of them.
    supportsSMT is derived from supported_transforms rather than its own
    stored flag, since that list is already the curated truth for it
    (app/db/seed_templates.py's _transforms_for()) and a separate boolean
    could silently drift from it."""
    def _mode(mode: str) -> bool:
        return not supported_modes or mode in supported_modes

    return {
        "cdc": _mode("cdc"),
        "snapshot": _mode("snapshot"),
        "schemaDiscovery": supports_schema_discovery,
        "supportsSMT": bool(supported_transforms),
        "supportsDLQ": supports_dlq,
    }


class ConnectorTemplateBase(BaseModel):
    name: str
    connector_type: str
    category: str
    connector_class: str
    runtime_pool_type: str
    config_schema: dict[str, Any] = Field(default_factory=dict)
    default_config: dict[str, Any] = Field(default_factory=dict)
    default_transforms: list[dict[str, Any]] = Field(default_factory=list)
    version: str = "v1"
    is_active: bool = True
    description: str | None = None
    instructions_url: str | None = None
    supported_transforms: list[dict[str, Any]] = Field(default_factory=list)

    # Connector plugin/version compatibility contract — see
    # app/models/catalog.py for what each field means and why it's
    # revisioned alongside connector_class/default_config/etc.
    connector_plugin: str | None = None
    connector_version: str | None = None
    supported_kafka_connect_versions: list[str] = Field(default_factory=list)
    supported_source_versions: list[str] = Field(default_factory=list)
    config_schema_version: str | None = None
    transform_compatibility: dict[str, str] = Field(default_factory=dict)
    migration_notes: str | None = None
    # Preflight compatibility contract (GAP-05) — see
    # app/services/deploy_preflight.py and app/models/catalog.py.
    supported_modes: list[str] = Field(default_factory=list)
    # FE-BUG-23 — see app/models/catalog.py for why these two are explicit
    # flags rather than derived from another list.
    supports_dlq: bool = True
    supports_schema_discovery: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capabilities(self) -> dict[str, bool]:
        return _capabilities(
            supported_modes=self.supported_modes, supported_transforms=self.supported_transforms,
            # Kafka Connect only offers a dead-letter queue to SINK connectors (app/core/error_policies.py),
            # so a source template can never report supportsDLQ whatever its stored flag says.
            supports_dlq=self.supports_dlq and is_sink(self.connector_type),
            supports_schema_discovery=self.supports_schema_discovery,
        )


class ConnectorTemplateCreate(ConnectorTemplateBase):
    id: str | None = None


class ConnectorTemplate(ConnectorTemplateBase):
    id: str
    # NEW-09: whether this connector can actually run here — supported, requires_configuration or unavailable, with a stable reason code. Computed
    # from the Kafka Connect worker's installed plugins on every list/get; None only on a response that was not asked to check (create/update).
    availability: Availability | None = None
    # The revision currently in effect for new pipelines — None only for a
    # template predating this field until it's next written through
    # catalog_service (which always creates one). See
    # GET /connector-templates/{id}/revisions for the full history.
    current_revision_id: str | None = None


class ConnectorTemplateRevision(BaseModel):
    id: str
    template_id: str
    revision_number: int
    connector_class: str
    default_config: dict[str, Any] = Field(default_factory=dict)
    default_transforms: list[dict[str, Any]] = Field(default_factory=list)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    supported_transforms: list[dict[str, Any]] = Field(default_factory=list)
    connector_plugin: str | None = None
    connector_version: str | None = None
    supported_kafka_connect_versions: list[str] = Field(default_factory=list)
    supported_source_versions: list[str] = Field(default_factory=list)
    config_schema_version: str | None = None
    transform_compatibility: dict[str, str] = Field(default_factory=dict)
    migration_notes: str | None = None
    supported_modes: list[str] = Field(default_factory=list)
    supports_dlq: bool = True
    supports_schema_discovery: bool = True
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capabilities(self) -> dict[str, bool]:
        return _capabilities(
            supported_modes=self.supported_modes, supported_transforms=self.supported_transforms,
            supports_dlq=self.supports_dlq, supports_schema_discovery=self.supports_schema_discovery,
        )


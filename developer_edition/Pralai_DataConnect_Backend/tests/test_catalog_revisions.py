from types import SimpleNamespace

from app.schemas.catalog import ConnectorTemplate
from app.services.catalog_revisions import (
    REVISION_FIELDS,
    revision_fields_changed,
    revision_to_dict,
)
from app.services.catalog_service import _template_to_dict


def _template(**overrides):
    base = {
        "id": "tpl_1", "name": "Postgres CDC", "connector_type": "source", "category": "database_cdc",
        "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
        "runtime_pool_type": "connect-cdc-pool", "config_schema": {}, "default_config": {"tasks.max": "1"},
        "default_transforms": [], "version": "v1", "is_active": True, "description": None,
        "instructions_url": None, "supported_transforms": [], "current_revision_id": "rev_1",
        "connector_plugin": "debezium-connector-postgresql", "connector_version": "2.7.3.Final",
        "supported_kafka_connect_versions": ["3.6", "3.7"], "supported_source_versions": ["14", "15"],
        "config_schema_version": "1.0", "transform_compatibility": {}, "migration_notes": None,
        "supported_modes": [], "supports_dlq": True, "supports_schema_discovery": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_template_to_dict_includes_current_revision_id() -> None:
    cached = _template_to_dict(_template(current_revision_id="rev_42"))
    assert cached["current_revision_id"] == "rev_42"
    assert cached["id"] == "tpl_1"


def test_template_to_dict_defaults_null_collections_to_empty() -> None:
    cached = _template_to_dict(_template(config_schema=None, default_config=None, default_transforms=None, supported_transforms=None))
    assert cached["config_schema"] == {}
    assert cached["default_config"] == {}
    assert cached["default_transforms"] == []
    assert cached["supported_transforms"] == []


def test_template_to_dict_includes_compatibility_fields() -> None:
    cached = _template_to_dict(_template())
    assert cached["connector_plugin"] == "debezium-connector-postgresql"
    assert cached["connector_version"] == "2.7.3.Final"
    assert cached["supported_kafka_connect_versions"] == ["3.6", "3.7"]
    assert cached["supported_source_versions"] == ["14", "15"]
    assert cached["config_schema_version"] == "1.0"


def test_template_to_dict_includes_supported_modes() -> None:
    cached = _template_to_dict(_template(supported_modes=["cdc"]))
    assert cached["supported_modes"] == ["cdc"]


def test_template_to_dict_defaults_null_supported_modes_to_empty() -> None:
    cached = _template_to_dict(_template(supported_modes=None))
    assert cached["supported_modes"] == []


def test_template_to_dict_defaults_null_compatibility_collections_to_empty() -> None:
    cached = _template_to_dict(
        _template(supported_kafka_connect_versions=None, supported_source_versions=None, transform_compatibility=None)
    )
    assert cached["supported_kafka_connect_versions"] == []
    assert cached["supported_source_versions"] == []
    assert cached["transform_compatibility"] == {}


def test_revision_to_dict_shape() -> None:
    revision = SimpleNamespace(
        id="rev_1", template_id="tpl_1", revision_number=1,
        connector_class="io.debezium.connector.postgresql.PostgresConnector",
        default_config={"tasks.max": "1"}, default_transforms=[], config_schema={},
        supported_transforms=[], created_at="2026-01-01T00:00:00Z",
        connector_plugin="debezium-connector-postgresql", connector_version="2.7.3.Final",
        supported_kafka_connect_versions=["3.6"], supported_source_versions=["15"],
        config_schema_version="1.0", transform_compatibility={}, migration_notes=None,
        supported_modes=[], supports_dlq=True, supports_schema_discovery=True,
    )
    as_dict = revision_to_dict(revision)
    assert as_dict["revision_number"] == 1
    assert as_dict["template_id"] == "tpl_1"
    assert as_dict["connector_version"] == "2.7.3.Final"
    assert as_dict["supported_kafka_connect_versions"] == ["3.6"]


def test_revision_fields_changed_true_when_connector_class_changes() -> None:
    template = _template()
    assert revision_fields_changed(template, {"connector_class": "some.other.Connector"}) is True


def test_revision_fields_changed_true_when_default_config_changes() -> None:
    template = _template()
    assert revision_fields_changed(template, {"default_config": {"tasks.max": "4"}}) is True


def test_revision_fields_changed_false_for_display_only_edits() -> None:
    """Editing name/description/instructions_url/is_active must never trigger
    a new revision — those don't change what gets deployed."""
    template = _template()
    assert revision_fields_changed(template, {"name": "New Name", "description": "updated"}) is False


def test_revision_fields_changed_false_when_new_value_equals_current() -> None:
    template = _template()
    assert revision_fields_changed(template, {"connector_class": template.connector_class}) is False


def test_revision_fields_changed_false_for_empty_payload() -> None:
    assert revision_fields_changed(_template(), {}) is False


def test_revision_fields_changed_true_when_connector_version_changes() -> None:
    """Re-qualifying a template against a newer connector plugin release
    must create a new revision — existing pipelines stay pinned to the old
    connector_version's snapshot, see test_pipeline_template_pinning.py."""
    template = _template()
    assert revision_fields_changed(template, {"connector_version": "3.0.0.Final"}) is True


def test_revision_fields_changed_true_when_supported_kafka_connect_versions_changes() -> None:
    template = _template()
    assert revision_fields_changed(template, {"supported_kafka_connect_versions": ["3.8", "3.9"]}) is True


def test_revision_fields_changed_true_when_supported_modes_changes() -> None:
    template = _template()
    assert revision_fields_changed(template, {"supported_modes": ["cdc", "snapshot"]}) is True


def test_revision_fields_changed_false_when_migration_notes_alone_matches_current() -> None:
    template = _template(migration_notes="upgrade carefully")
    assert revision_fields_changed(template, {"migration_notes": "upgrade carefully"}) is False


def test_revision_fields_cover_every_field_checked() -> None:
    """A regression guard: every REVISION_FIELDS entry must actually be a
    real attribute revision_fields_changed can read."""
    template = _template()
    for field in REVISION_FIELDS:
        getattr(template, field)  # must not raise


# ── FE-BUG-23: ConnectorTemplate.capabilities (app/schemas/catalog.py) ──

def _capable_template(**overrides) -> ConnectorTemplate:
    base = {
        "id": "tpl_1", "name": "Postgres CDC", "connector_type": "source", "category": "database_cdc",
        "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
        "runtime_pool_type": "connect-cdc-pool",
        "supported_modes": ["cdc", "snapshot"], "supported_transforms": [{"type": "MaskField"}],
        "supports_dlq": True, "supports_schema_discovery": True,
    }
    base.update(overrides)
    return ConnectorTemplate(**base)


def test_capabilities_reflects_supported_modes() -> None:
    caps = _capable_template(supported_modes=["cdc", "snapshot"]).capabilities
    assert caps == {
        "cdc": True, "snapshot": True, "schemaDiscovery": True,
        "supportsSMT": True,
        # A SOURCE template: Kafka Connect gives sources no dead-letter queue, whatever the stored flag says.
        "supportsDLQ": False,
    }


def test_a_sink_template_can_support_the_dead_letter_queue() -> None:
    sink = _capable_template(connector_type="sink", connector_class="io.confluent.connect.jdbc.JdbcSinkConnector")

    assert sink.capabilities["supportsDLQ"] is True


def test_a_sink_curated_to_opt_out_of_the_dead_letter_queue_reports_it() -> None:
    sink = _capable_template(
        connector_type="sink", connector_class="io.confluent.connect.jdbc.JdbcSinkConnector", supports_dlq=False,
    )

    assert sink.capabilities["supportsDLQ"] is False


def test_capabilities_cdc_false_when_not_in_supported_modes() -> None:
    caps = _capable_template(supported_modes=["batch"]).capabilities
    assert caps["cdc"] is False
    assert caps["snapshot"] is False


def test_capabilities_unrestricted_when_supported_modes_is_empty() -> None:
    """Same "empty means unrestricted" convention as supported_modes itself
    — a template that hasn't been curated yet shouldn't have the Designer
    hide every mode outright."""
    caps = _capable_template(supported_modes=[]).capabilities
    assert caps["cdc"] is True
    assert caps["snapshot"] is True


def test_capabilities_supports_smt_derived_from_supported_transforms() -> None:
    assert _capable_template(supported_transforms=[]).capabilities["supportsSMT"] is False
    assert _capable_template(supported_transforms=[{"type": "MaskField"}]).capabilities["supportsSMT"] is True


def test_capabilities_reflects_supports_dlq_and_schema_discovery_flags() -> None:
    caps = _capable_template(supports_dlq=False, supports_schema_discovery=False).capabilities
    assert caps["supportsDLQ"] is False
    assert caps["schemaDiscovery"] is False

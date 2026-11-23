"""
GAP-12: a pipeline's direction (SOURCE/SINK) is now explicit — derived from
the resolved template's connector_type at creation/update time
(pipeline_spec_validator.py's _validate_direction) — and drives deterministic,
creation-time validation instead of only surfacing as a deploy-time
ValueError from connect_config.py's _sink_topic()/_build_connector.
"""

import pytest

from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service


def _seed_source(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


def _seed_sink(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSinkConnector", "connector_type": "sink", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


def _payload(template: dict, connection: dict, **overrides) -> PipelineCreate:
    base = {
        "name": "Orders", "template_id": template["id"], "connection_profile_id": connection["id"],
        "zone": "raw", "domain": "sales", "source_system": "orders_app", "entity": "orders",
        "mode": "cdc", "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)


@pytest.mark.asyncio
async def test_source_pipeline_is_created_with_direction_source(fake_postgres) -> None:
    template, connection = _seed_source(fake_postgres, "org_dir_1")

    created = await pipeline_service.create_pipeline("org_dir_1", _payload(template, connection))

    assert created["direction"] == "SOURCE"


@pytest.mark.asyncio
async def test_sink_pipeline_without_input_topic_is_rejected_at_creation(fake_postgres) -> None:
    """The bug this closes: previously this succeeded (201) and only failed
    later, at deploy(), via connect_config.py's _sink_topic() raising."""
    template, connection = _seed_sink(fake_postgres, "org_dir_2")

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_dir_2", _payload(template, connection, mode="api"))

    assert "input topic" in exc_info.value.message
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_sink_pipeline_with_input_topic_is_created_with_direction_sink(fake_postgres) -> None:
    template, connection = _seed_sink(fake_postgres, "org_dir_3")

    created = await pipeline_service.create_pipeline(
        "org_dir_3", _payload(template, connection, mode="api", connector_config={"topics": "raw-sales-pg-orders-cdc-v1"}),
    )

    assert created["direction"] == "SINK"


@pytest.mark.asyncio
async def test_a_generated_topic_name_never_satisfies_a_sinks_input_topic_requirement(fake_postgres) -> None:
    """GAP-12's "generated source topic prohibited" for SINK: topic_name is
    computed for every pipeline regardless of direction (identity/naming),
    but a sink must never be considered complete just because it has one —
    only an explicit connector_config.topics counts."""
    template, connection = _seed_sink(fake_postgres, "org_dir_4")

    with pytest.raises(ValidationError):
        # No connector_config.topics — topic_name will still get generated
        # by generate_pipeline_config regardless, but must not count.
        await pipeline_service.create_pipeline("org_dir_4", _payload(template, connection, mode="api"))


@pytest.mark.asyncio
async def test_template_with_unrecognized_connector_type_is_rejected(fake_postgres) -> None:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "x", "connector_type": "transform", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": "org_dir_5", "name": "c", "connection_config": {}},
    )

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_dir_5", _payload(template, connection))

    assert "connector_type" in exc_info.value.message


@pytest.mark.asyncio
async def test_retargeting_a_pipeline_from_source_to_sink_recomputes_direction_and_validates(fake_postgres) -> None:
    source_template, connection = _seed_source(fake_postgres, "org_dir_6")
    created = await pipeline_service.create_pipeline("org_dir_6", _payload(source_template, connection))
    assert created["direction"] == "SOURCE"

    sink_template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSinkConnector", "connector_type": "sink", "default_config": {}},
    )

    # Retargeting to a sink template without also supplying an input topic
    # must be rejected the same way a fresh sink creation would be.
    with pytest.raises(ValidationError):
        await pipeline_service.update_pipeline(
            "org_dir_6", created["id"], PipelineUpdate(template_id=sink_template["id"]),
        )

    updated = await pipeline_service.update_pipeline(
        "org_dir_6", created["id"],
        PipelineUpdate(template_id=sink_template["id"], connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"}),
    )
    assert updated["direction"] == "SINK"


@pytest.mark.asyncio
async def test_an_unrelated_edit_to_an_already_valid_sink_pipeline_still_succeeds(fake_postgres) -> None:
    template, connection = _seed_sink(fake_postgres, "org_dir_7")
    created = await pipeline_service.create_pipeline(
        "org_dir_7", _payload(template, connection, mode="api", connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"}),
    )

    updated = await pipeline_service.update_pipeline(
        "org_dir_7", created["id"], PipelineUpdate(description="a note"),
    )

    assert updated["description"] == "a note"
    assert updated["direction"] == "SINK"

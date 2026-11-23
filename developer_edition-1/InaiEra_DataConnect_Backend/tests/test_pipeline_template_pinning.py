"""
The behavioral guarantee this whole fix is for: once a pipeline is created
against a connector template, editing that template afterward must never
change what the pipeline actually deploys. These tests simulate a template
edit the way catalog_service.py performs one in practice — updating the
same row pipeline_service reads through — without needing a real Postgres,
since the pinning behavior under test lives entirely in
pipeline_service.py/runtime_reconciler.py's use of the snapshot, not in the
database write path itself (covered separately by test_catalog_revisions.py).

Connection profiles, pipelines, and connector templates are all
Postgres-authoritative, so this uses the `fake_postgres` fixture (see
conftest.py) for all three.
"""

import pytest

from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service
from app.services.runtime_reconciler import _resolve_config


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {
            "connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector",
            "connector_type": "source",
            "default_config": {"tasks.max": "1"},
            "default_transforms": [],
            "config_schema": {},
            "supported_transforms": [],
            "current_revision_id": "rev_1",
        },
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict) -> dict:
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


def _edit_template_in_place(fake_postgres, template: dict, **changes) -> None:
    """What catalog_service.py's update_template() ultimately does to the
    row pipeline_service reads through (and, in the real service, a new
    current_revision_id — bumped here too, for realism)."""
    row = fake_postgres.connector_templates[template["id"]]
    for key, value in {**changes, "current_revision_id": "rev_2"}.items():
        setattr(row, key, value)


@pytest.mark.asyncio
async def test_pipeline_pins_template_snapshot_at_creation(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_pin_1")
    created = await _create_pipeline("org_pin_1", template, connection)

    assert created["template_revision_id"] == "rev_1"
    assert created["template_snapshot"]["connector_class"] == template["connector_class"]
    assert created["template_snapshot"]["default_config"] == {"tasks.max": "1"}


@pytest.mark.asyncio
async def test_editing_the_template_does_not_change_an_existing_pipelines_snapshot(fake_postgres) -> None:
    """The core guarantee: a template edit must never retroactively reshape
    what an already-created pipeline is pinned to."""
    template, connection = _seed_org(fake_postgres, "org_pin_2")
    created = await _create_pipeline("org_pin_2", template, connection)

    _edit_template_in_place(
        fake_postgres, template,
        connector_class="io.debezium.connector.postgresql.PostgresConnector",
        default_config={"tasks.max": "8"},
    )

    unchanged = await pipeline_service.get_or_404("org_pin_2", created["id"])
    assert unchanged["template_revision_id"] == "rev_1"
    assert unchanged["template_snapshot"]["connector_class"] == "io.confluent.connect.jdbc.JdbcSourceConnector"
    assert unchanged["template_snapshot"]["default_config"] == {"tasks.max": "1"}


@pytest.mark.asyncio
async def test_unrelated_pipeline_edit_does_not_advance_the_pinned_revision(fake_postgres) -> None:
    """Even a PATCH to the SAME pipeline (touching an unrelated field) must
    not silently pick up a template edit that happened in between."""
    template, connection = _seed_org(fake_postgres, "org_pin_3")
    created = await _create_pipeline("org_pin_3", template, connection)

    _edit_template_in_place(fake_postgres, template, default_config={"tasks.max": "99"})

    updated = await pipeline_service.update_pipeline(
        "org_pin_3", created["id"], PipelineUpdate(description="just a note")
    )

    assert updated["template_revision_id"] == "rev_1"
    assert updated["template_snapshot"]["default_config"] == {"tasks.max": "1"}


@pytest.mark.asyncio
async def test_explicitly_changing_template_id_re_pins_to_the_new_templates_current_revision(fake_postgres) -> None:
    """Deliberately retargeting a pipeline at a different (or the same,
    freshly-edited) template IS supposed to pick up its current state —
    only UNRELATED edits are protected against."""
    template, connection = _seed_org(fake_postgres, "org_pin_4")
    created = await _create_pipeline("org_pin_4", template, connection)

    other_template = fake_postgres.create(
        "connector_templates",
        {
            "connector_class": "io.debezium.connector.mysql.MySqlConnector",
            "connector_type": "source",
            "default_config": {"tasks.max": "2"},
            "default_transforms": [],
            "config_schema": {},
            "supported_transforms": [],
            "current_revision_id": "rev_9",
        },
    )

    updated = await pipeline_service.update_pipeline(
        "org_pin_4", created["id"], PipelineUpdate(template_id=other_template["id"])
    )

    assert updated["template_revision_id"] == "rev_9"
    assert updated["template_snapshot"]["connector_class"] == "io.debezium.connector.mysql.MySqlConnector"


@pytest.mark.asyncio
async def test_runtime_reconciler_resolve_config_prefers_the_pinned_snapshot(fake_postgres) -> None:
    """The reconciler rebuilds and redeploys a pipeline's config on every
    tick — it must use the pinned snapshot too, or an already-running
    connector would silently drift onto a template edit made after the
    fact."""
    template, connection = _seed_org(fake_postgres, "org_pin_5")
    created = await _create_pipeline("org_pin_5", template, connection)

    _edit_template_in_place(fake_postgres, template, connector_class="something.totally.Different")

    config, _config_hash = await _resolve_config(created)

    assert config["connector.class"] == "io.confluent.connect.jdbc.JdbcSourceConnector"

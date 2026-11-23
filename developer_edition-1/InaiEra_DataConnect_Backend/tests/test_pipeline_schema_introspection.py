"""
BUG-10: PipelineService.autocreate_schema_from_source's outcome must be
recorded on the pipeline (schema_introspection_status/_message) and logged
for every case — including an unexpected failure such as the source
connection timing out, which the create_pipeline route previously
discarded via a bare `except Exception: pass`.
"""

import pytest
from structlog.testing import capture_logs

import app.services.connection_discovery as connection_discovery_module
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict, **overrides) -> dict:
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
        **overrides,
    )
    return await pipeline_service.create_pipeline(org_id, payload)


@pytest.mark.asyncio
async def test_linking_an_existing_schema_persists_linked_existing_status(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_1")
    fake_postgres.create(
        "schema_versions",
        {
            "org_id": "org_schema_1", "domain": "sales", "source_system": "orders_app",
            "entity": "orders", "version": "v1", "schema_format": "json_schema",
            "compatibility": "BACKWARD", "status": "active", "schema_definition": {"fields": []},
        },
    )
    created = await _create_pipeline("org_schema_1", template, connection)

    result = await pipeline_service.autocreate_schema_from_source("org_schema_1", created)

    assert result is not None
    updated = await pipeline_service.get_pipeline("org_schema_1", created["id"])
    assert updated["schema_introspection_status"] == "linked_existing"
    assert updated["schema_introspection_message"] is None
    assert updated["schema_version_id"] == result["id"]


@pytest.mark.asyncio
async def test_successful_introspection_persists_created_status(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_2")
    created = await _create_pipeline("org_schema_2", template, connection)

    async def _fake_introspect_columns(connection, entity, *, org_id):
        return {"connected": True, "fields": [{"name": "id", "type": "integer"}]}

    monkeypatch.setattr(connection_discovery_module, "introspect_columns", _fake_introspect_columns)

    result = await pipeline_service.autocreate_schema_from_source("org_schema_2", created)

    assert result is not None
    updated = await pipeline_service.get_pipeline("org_schema_2", created["id"])
    assert updated["schema_introspection_status"] == "created"
    assert updated["schema_introspection_message"] is None
    assert updated["schema_version_id"] == result["id"]


@pytest.mark.asyncio
async def test_unreachable_source_persists_skipped_status_with_reason(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_3")
    created = await _create_pipeline("org_schema_3", template, connection)

    async def _fake_introspect_columns(connection, entity, *, org_id):
        return {"connected": False, "fields": [], "message": "Connection refused"}

    monkeypatch.setattr(connection_discovery_module, "introspect_columns", _fake_introspect_columns)

    result = await pipeline_service.autocreate_schema_from_source("org_schema_3", created)

    assert result is None
    updated = await pipeline_service.get_pipeline("org_schema_3", created["id"])
    assert updated["schema_introspection_status"] == "skipped"
    assert updated["schema_introspection_message"] == "Connection refused"
    assert updated["schema_version_id"] is None


@pytest.mark.asyncio
async def test_unexpected_failure_is_recorded_as_failed_and_logged_not_swallowed(monkeypatch, fake_postgres) -> None:
    """The bug: a source connection timing out (or any other unexpected
    exception) used to vanish entirely — no persisted record, no log line.
    Pipeline creation must still succeed (this method never raises), but
    the failure must now be both recorded on the pipeline and logged."""
    template, connection = _seed_org(fake_postgres, "org_schema_4")
    created = await _create_pipeline("org_schema_4", template, connection)

    async def _timeout(connection, entity, *, org_id):
        raise TimeoutError("Source connection timed out")

    monkeypatch.setattr(connection_discovery_module, "introspect_columns", _timeout)

    with capture_logs() as logs:
        result = await pipeline_service.autocreate_schema_from_source("org_schema_4", created)

    assert result is None  # never raises — pipeline creation is unaffected
    updated = await pipeline_service.get_pipeline("org_schema_4", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    assert "SOURCE_CONNECTION_TIMEOUT" in updated["schema_introspection_message"]
    assert "Source connection timed out" not in updated["schema_introspection_message"]  # the raw text is log-only
    assert any(entry.get("event") == "pipeline.schema_autocreate_failed" for entry in logs)


@pytest.mark.asyncio
async def test_a_credential_leaking_into_the_exception_message_is_scrubbed(monkeypatch, fake_postgres) -> None:
    """The exception's own str() is outside this app's control (e.g. a
    driver embeds the DSN it tried to connect to) — must go through the
    same sanitize_probe_message scrubbing BUG-08 already applies to
    connection probe failures, not be persisted raw."""
    template, connection = _seed_org(fake_postgres, "org_schema_5")
    created = await _create_pipeline("org_schema_5", template, connection)

    async def _leaky(connection, entity, *, org_id):
        raise ConnectionError("could not connect to postgres://dataconnect:hunter2@db:5432/erp")

    monkeypatch.setattr(connection_discovery_module, "introspect_columns", _leaky)

    await pipeline_service.autocreate_schema_from_source("org_schema_5", created)

    updated = await pipeline_service.get_pipeline("org_schema_5", created["id"])
    assert "hunter2" not in updated["schema_introspection_message"]


@pytest.mark.asyncio
async def test_already_linked_schema_skips_introspection_entirely(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_6")
    schema = fake_postgres.create(
        "schema_versions",
        {
            "org_id": "org_schema_6", "domain": "sales", "source_system": "orders_app",
            "entity": "orders", "version": "v1", "schema_format": "json_schema",
            "compatibility": "BACKWARD", "status": "active", "schema_definition": {"fields": []},
        },
    )
    created = await _create_pipeline(
        "org_schema_6", template, connection, schema_version_id=schema["id"],
    )

    result = await pipeline_service.autocreate_schema_from_source("org_schema_6", created)

    assert result is None  # nothing to do — already linked
    updated = await pipeline_service.get_pipeline("org_schema_6", created["id"])
    assert updated["schema_introspection_status"] is None  # never attempted


# ── the real failure path: a driver error must come out of introspect_columns as a FAILURE, with a reason ───────────
# (the tests above replace introspect_columns wholesale, so they never saw it swallow a real timeout into "skipped")


def _seed_postgres_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles",
        {"org_id": org_id, "name": "pg", "source_type": "postgres", "connection_config": {"hostname": "db.example.test"}},
    )
    return template, connection


def _driver_raises(monkeypatch, error: Exception) -> None:
    async def _no_network_policy(cfg, org_id, purpose):
        return None

    async def _driver(cfg, entity, timeout):
        raise error

    monkeypatch.setattr(connection_discovery_module, "_guard_destination", _no_network_policy)
    monkeypatch.setitem(connection_discovery_module._COL, "postgres", _driver)


@pytest.mark.asyncio
async def test_a_real_source_timeout_is_recorded_as_failed_with_a_reason_and_logged(monkeypatch, fake_postgres) -> None:
    """The ticket's own example. asyncio's TimeoutError has an EMPTY str(), so the reason used to come out blank."""
    template, connection = _seed_postgres_org(fake_postgres, "org_schema_7")
    created = await _create_pipeline("org_schema_7", template, connection)
    _driver_raises(monkeypatch, TimeoutError())

    with capture_logs() as logs:
        result = await pipeline_service.autocreate_schema_from_source("org_schema_7", created)

    assert result is None  # creation is unaffected
    updated = await pipeline_service.get_pipeline("org_schema_7", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    assert "did not respond within 8 seconds" in updated["schema_introspection_message"]
    assert "SOURCE_CONNECTION_TIMEOUT" in updated["schema_introspection_message"]
    events = {entry["event"] for entry in logs}
    assert "connection_discovery.introspect_columns_failed" in events  # logged where the cause is known
    assert "pipeline.schema_introspection_failed" in events  # and where it is recorded


@pytest.mark.asyncio
async def test_a_refused_connection_is_recorded_as_failed_with_the_driver_error(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_postgres_org(fake_postgres, "org_schema_8")
    created = await _create_pipeline("org_schema_8", template, connection)
    _driver_raises(monkeypatch, ConnectionRefusedError("[Errno 111] Connect call failed ('10.0.0.5', 5432)"))

    await pipeline_service.autocreate_schema_from_source("org_schema_8", created)

    updated = await pipeline_service.get_pipeline("org_schema_8", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    message = updated["schema_introspection_message"]
    assert "SOURCE_CONNECTION_REFUSED" in message
    assert "10.0.0.5" not in message  # no host or port in what is stored and returned
    assert "5432" not in message


@pytest.mark.asyncio
async def test_a_missing_driver_is_a_failure_not_a_skip(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_postgres_org(fake_postgres, "org_schema_9")
    created = await _create_pipeline("org_schema_9", template, connection)
    _driver_raises(monkeypatch, ModuleNotFoundError("No module named 'asyncpg'"))

    await pipeline_service.autocreate_schema_from_source("org_schema_9", created)

    updated = await pipeline_service.get_pipeline("org_schema_9", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    assert "DRIVER_NOT_INSTALLED" in updated["schema_introspection_message"]


@pytest.mark.asyncio
async def test_a_destination_blocked_by_network_policy_stays_a_skip(fake_postgres) -> None:
    """Policy choosing not to connect is expected, not a fault."""
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles",
        {"org_id": "org_schema_10", "name": "pg", "source_type": "postgres", "connection_config": {"hostname": "127.0.0.1"}},
    )
    created = await _create_pipeline("org_schema_10", template, connection)

    await pipeline_service.autocreate_schema_from_source("org_schema_10", created)

    updated = await pipeline_service.get_pipeline("org_schema_10", created["id"])
    assert updated["schema_introspection_status"] == "skipped"
    assert "does not permit" in updated["schema_introspection_message"]


@pytest.mark.asyncio
async def test_an_exception_with_no_message_still_records_a_reason(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_11")
    created = await _create_pipeline("org_schema_11", template, connection)

    async def _bare_timeout(connection, entity, *, org_id):
        raise TimeoutError()

    monkeypatch.setattr(connection_discovery_module, "introspect_columns", _bare_timeout)

    await pipeline_service.autocreate_schema_from_source("org_schema_11", created)

    updated = await pipeline_service.get_pipeline("org_schema_11", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    assert "SOURCE_CONNECTION_TIMEOUT" in updated["schema_introspection_message"]


@pytest.mark.asyncio
async def test_a_connection_that_no_longer_exists_is_recorded_not_dropped(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_schema_12")
    created = await _create_pipeline("org_schema_12", template, connection)
    del fake_postgres.connection_profiles[connection["id"]]

    result = await pipeline_service.autocreate_schema_from_source("org_schema_12", created)

    assert result is None
    updated = await pipeline_service.get_pipeline("org_schema_12", created["id"])
    assert updated["schema_introspection_status"] == "failed"
    assert "was not found" in updated["schema_introspection_message"]

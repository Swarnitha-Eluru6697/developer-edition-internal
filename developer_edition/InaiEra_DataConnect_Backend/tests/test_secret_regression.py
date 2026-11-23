"""
GAP-01 regression suite: a known marker secret, pushed through the real
connection create/update/deploy-config paths, must never surface in
Postgres JSON, an audit record, a structlog log event, an error message, or
a pipeline's stored/returned generated_config — every surface the bug
report named. Only the transient, hydrated config actually handed to Kafka
Connect at deploy time (build_connector's own return value, never persisted
or returned by any route) is EXPECTED to carry the real value — that's
checked separately, as a positive case, so this suite can't pass by
accident (e.g. secret_store silently losing the value instead of properly
isolating it).
"""

from typing import Any

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.core.errors import ConnectorConfigError
from app.main import app
from app.schemas.pipeline import PipelineCreate
from app.services.connect_config import DBZ_PG, build_connector
from app.services.connection_service import connection_service
from app.services.pipeline_config import generate_pipeline_config
from app.services.secrets import mask_connection_profile

MARKER = "TEST_SECRET_9f32d1e7c8b4a5069d2f1e3c4b5a6978"

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


def _contains_marker(value: Any) -> bool:
    """Recursively stringifies `value` and checks for the marker — a
    substring check on the whole structure, not just a top-level key lookup,
    so the marker can't hide inside a nested dict/list some future field
    reshuffle introduces."""
    return MARKER in repr(value)


@pytest.mark.asyncio
async def test_marker_never_reaches_the_persisted_connection_config(fake_postgres) -> None:
    created = await connection_service.create_connection(
        "org_secret_1",
        {
            "name": "ERP Postgres", "source_type": "postgres",
            "connection_config": {
                "hostname": "erp-db.internal", "database": "erp",
                "username": "dataconnect", "password": MARKER,
            },
        },
    )

    stored_row = fake_postgres.connection_profiles[created["id"]]
    assert not _contains_marker(stored_row.connection_config)
    assert stored_row.connection_config == {
        "hostname": "erp-db.internal", "database": "erp", "username": "dataconnect",
    }
    # secret_ref points at it, never the value itself.
    assert stored_row.secret_ref == f"secret://dataconnect/connections/{created['id']}"
    assert not _contains_marker(stored_row.secret_ref)

    # It IS retrievable from secret_store — this isn't just data loss.
    assert fake_postgres.secret_store._by_connection[created["id"]]["password"] == MARKER


@pytest.mark.asyncio
async def test_marker_never_reaches_the_connections_api_response(fake_postgres) -> None:
    created = await connection_service.create_connection(
        "org_secret_2",
        {
            "name": "ERP Postgres", "source_type": "postgres",
            "connection_config": {"hostname": "erp-db.internal", "database": "erp",
                                   "username": "dataconnect", "password": MARKER},
        },
    )
    fetched = await connection_service.get_connection("org_secret_2", created["id"])

    for payload in (created, fetched, mask_connection_profile(created), mask_connection_profile(fetched)):
        assert not _contains_marker(payload), payload


def test_marker_never_reaches_the_connections_route_response_or_audit(fake_postgres, monkeypatch) -> None:
    """The same check, but through the real HTTP route and its real
    record_audit call — "through the system", not just the service layer."""
    audit_calls: list[dict] = []

    async def _capture_audit(**kwargs) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.connections.record_audit", _capture_audit)

    resp = client.post(
        "/api/v1/dataconnect/connections",
        headers={"X-Tenant-Id": "org_secret_3"},
        json={
            "name": "ERP Postgres", "source_type": "postgres",
            "connection_config": {"hostname": "erp-db.internal", "database": "erp",
                                   "username": "dataconnect", "password": MARKER},
        },
    )
    assert resp.status_code == 201, resp.text
    assert not _contains_marker(resp.json())
    assert not _contains_marker(resp.text)

    assert audit_calls, "record_audit was never called"
    assert not _contains_marker(audit_calls[0].get("after_state"))
    assert not _contains_marker(audit_calls)


@pytest.mark.asyncio
async def test_marker_never_appears_in_a_log_event_during_create_update_or_delete(fake_postgres) -> None:
    with capture_logs() as logs:
        created = await connection_service.create_connection(
            "org_secret_4",
            {
                "name": "ERP Postgres", "source_type": "postgres",
                "connection_config": {"hostname": "erp-db.internal", "database": "erp",
                                       "username": "dataconnect", "password": MARKER},
            },
        )
        await connection_service.update_connection(
            "org_secret_4", created["id"],
            {
                "name": "ERP Postgres", "source_type": "postgres",
                "connection_config": {"hostname": "erp-db-2.internal", "database": "erp",
                                       "username": "dataconnect", "password": "********"},
            },
        )
        await connection_service.delete_connection("org_secret_4", created["id"])

    assert logs, "no log events were captured — the test isn't exercising what it thinks it is"
    for event in logs:
        assert not _contains_marker(event), event


@pytest.mark.asyncio
async def test_marker_never_appears_in_a_validation_error(fake_postgres) -> None:
    with pytest.raises(ConnectorConfigError) as exc_info:
        await connection_service.create_connection(
            "org_secret_5",
            {
                "name": "Bad Postgres", "source_type": "postgres",
                # database missing -> a real validation error, with the
                # marker still present on the field that DID validate.
                "connection_config": {"hostname": "erp-db.internal", "username": "dataconnect", "password": MARKER},
            },
        )

    assert not _contains_marker(exc_info.value.message)
    assert not _contains_marker(exc_info.value.field_errors)
    assert not _contains_marker(str(exc_info.value))


@pytest.mark.asyncio
async def test_marker_never_appears_in_a_live_probe_error(fake_postgres) -> None:
    """A real (network-level) failure against an address nothing is
    listening on — the error message construction itself
    (f"...: {exc}"-style) must never have embedded the whole config."""
    created = await connection_service.create_connection(
        "org_secret_6",
        {
            "name": "Unreachable Postgres", "source_type": "postgres",
            "connection_config": {
                "hostname": "203.0.113.1",  # TEST-NET-3 — reserved, never routable
                "port": 5432, "database": "erp", "username": "dataconnect", "password": MARKER,
            },
        },
    )
    fetched = await connection_service.get_connection("org_secret_6", created["id"])

    result = await connection_service.live_test_connection(fetched, "org_secret_6")

    assert result["status"] == "failed"
    assert not _contains_marker(result)


@pytest.mark.asyncio
async def test_marker_never_appears_in_a_pipelines_stored_generated_config(fake_postgres) -> None:
    """generate_pipeline_config is what builds Pipeline.generated_config —
    the PERSISTED, API-returned preview (GET .../generated-config), not the
    transient payload actually sent to Kafka Connect at deploy time (see
    build_connector's own positive-case test below)."""
    created = await connection_service.create_connection(
        "org_secret_7",
        {
            "name": "ERP Postgres", "source_type": "postgres",
            "connection_config": {"hostname": "erp-db.internal", "database": "erp",
                                   "username": "dataconnect", "password": MARKER},
        },
    )
    connection_profile = await connection_service.get_connection("org_secret_7", created["id"])

    pipeline = PipelineCreate(
        name="ERP customers", template_id="tpl", connection_profile_id=created["id"],
        zone="raw", domain="erp", source_system="postgres", entity="customers", mode="cdc", version="v1",
    )
    result = await generate_pipeline_config(
        org_id="org_secret_7", pipeline=pipeline,
        connector_template={"connector_class": DBZ_PG, "default_config": {}},
        connection_profile=connection_profile,
    )

    assert not _contains_marker(result["generated_config"])
    assert not _contains_marker(result)


@pytest.mark.asyncio
async def test_build_connector_does_hydrate_the_real_secret_for_an_actual_deploy(fake_postgres) -> None:
    """The one place the marker MUST show up: the transient config
    build_connector hands to the real Kafka Connect deploy call. Proves the
    fix isolates the secret rather than just losing it — hydration has to
    actually work, or every real deploy would silently authenticate with a
    literal "********"."""
    created = await connection_service.create_connection(
        "org_secret_8",
        {
            "name": "ERP Postgres", "source_type": "postgres",
            "connection_config": {"hostname": "erp-db.internal", "database": "erp",
                                   "username": "dataconnect", "password": MARKER},
        },
    )
    connection_profile = await connection_service.get_connection("org_secret_8", created["id"])
    assert not _contains_marker(connection_profile)  # sanity: fetched dict is still masked

    pipeline = {
        "id": "pipe-1", "org_id": "org_secret_8", "domain": "erp", "source_system": "postgres",
        "entity": "customers", "version": "v1", "topic_name": "raw-erp-postgres-customers-cdc-v1",
        "connector_name": "raw--erp--postgres--customers--cdc--v1", "connector_config": {},
    }
    config, _ = await build_connector(
        pipeline, connection_profile, {"connector_class": DBZ_PG}, schema=None,
    )

    assert config["database.password"] == MARKER

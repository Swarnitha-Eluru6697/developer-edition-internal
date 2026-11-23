"""GAP-01: a pipeline's free-form connector_config must not carry credentials — it is stored in Postgres as-is,
returned in every Pipeline response and copied into the audit log. Credentials belong on the connection."""
import pytest

from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service
from app.services.secrets import find_secret_shaped_entries

MARKER = "TEST_SECRET_9f32d1e7c8b4a5069d2f1e3c4b5a6978"


def _seed(fake_postgres, org_id="org_cc"):
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "c", "connection_config": {}, "verification_status": "ok"},
    )
    return template, connection


def _payload(template, connection, connector_config) -> PipelineCreate:
    return PipelineCreate(
        name="Orders", template_id=template["id"], connection_profile_id=connection["id"], zone="raw", domain="sales",
        source_system="orders_app", entity="orders", mode="cdc", version="v1", connector_config=connector_config,
    )


# ── the detector ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("config", [
    {"connection.password": MARKER},
    {"password": MARKER},
    {"aws.secret.access.key": MARKER},
    {"api_key": MARKER},
    {"auth": {"token": MARKER}},                                   # nested
    {"extras": [{"client_secret": MARKER}]},                       # inside a list
    {"connection.url": f"jdbc:postgresql://svc:{MARKER}@db.internal:5432/erp"},  # credentials embedded in a URI
])
def test_inline_credentials_are_detected(config) -> None:
    assert find_secret_shaped_entries(config)


@pytest.mark.parametrize("config", [
    {},
    {"tables": "orders,customers", "topics": "raw-t-s-raw_x-batch-v1", "snapshot_mode": "initial", "load_mode": "incrementing"},
    {"key.converter": "org.apache.kafka.connect.json.JsonConverter", "message.key.columns": "orders:id"},
    {"connection.password": "********"},                           # the mask
    {"connection.password": "${vault:dataconnect/org/name:password}"},  # a ConfigProvider placeholder
    {"secret_ref": "secret://dataconnect/connections/123"},       # a reference, not a secret
    {"connection.url": "jdbc:postgresql://db.internal:5432/erp"},  # no credentials in the URI
])
def test_ordinary_config_references_and_masks_are_not_flagged(config) -> None:
    assert find_secret_shaped_entries(config) == []


def test_the_detector_returns_paths_never_values() -> None:
    found = find_secret_shaped_entries({"auth": {"token": MARKER}, "tables": "a"})

    assert found == ["auth.token"]
    assert MARKER not in str(found)


# ── the API path ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_creating_a_pipeline_with_an_inline_credential_is_rejected_and_nothing_is_stored(fake_postgres) -> None:
    template, connection = _seed(fake_postgres)

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_cc", _payload(template, connection, {"connection.password": MARKER, "tables": "a"}))

    assert "connection.password" in str(exc_info.value)
    assert [f.field for f in exc_info.value.field_errors] == ["connector_config.connection.password"]
    assert MARKER not in str(exc_info.value)
    assert MARKER not in str([f.message for f in exc_info.value.field_errors])
    assert fake_postgres.pipelines == {}


@pytest.mark.asyncio
async def test_updating_a_pipeline_to_add_a_credential_is_rejected_too(fake_postgres) -> None:
    template, connection = _seed(fake_postgres)
    created = await pipeline_service.create_pipeline("org_cc", _payload(template, connection, {"tables": "orders"}))

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.update_pipeline(
            "org_cc", created["id"], PipelineUpdate(connector_config={"tables": "orders", "api_key": MARKER}),
        )

    assert "api_key" in str(exc_info.value)
    assert MARKER not in str(exc_info.value)
    stored = fake_postgres.pipelines[created["id"]].connector_config
    assert MARKER not in str(stored)


@pytest.mark.asyncio
async def test_a_normal_pipeline_still_creates(fake_postgres) -> None:
    template, connection = _seed(fake_postgres)

    created = await pipeline_service.create_pipeline(
        "org_cc", _payload(template, connection, {"tables": "orders", "snapshot_mode": "initial"}),
    )

    assert created["connector_config"]["tables"] == "orders"

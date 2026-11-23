import pytest

from app.services.connect_config import ADLS_GEN2_SINK, GCS_SINK, ICEBERG_SINK, build_connector
from app.services.connection_config_schema import validate_connection_config


def _pipeline(**overrides) -> dict:
    base = {
        "id": "pipe-metrics",
        "org_id": "org_sinks",
        "domain": "analytics",
        "source_system": "app",
        "entity": "metrics",
        "version": "v1",
        "topic_name": "raw-analytics-app-metrics-stream-v1",
        "connector_name": "raw--analytics--app--metrics--stream--v1",
        "connector_config": {"topics": "raw-t-s-some_input_topic-batch-v1"},
    }
    base.update(overrides)
    return base


def _connection(config: dict) -> dict:
    return {"connection_config": config}


# ── GCS sink ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gcs_sink_builds_storage_common_config(fake_postgres) -> None:
    template = {"connector_class": GCS_SINK}
    connection = _connection({"bucket": "my-lake"})

    config, produced_topic = await build_connector(_pipeline(), connection, template, schema=None)

    assert produced_topic is None
    assert config["connector.class"] == GCS_SINK
    assert config["gcs.bucket.name"] == "my-lake"
    assert config["storage.class"] == "io.confluent.connect.gcs.storage.GcsStorage"
    assert config["format.class"] == "io.confluent.connect.gcs.format.json.JsonFormat"
    assert config["topics"] == "raw-t-s-some_input_topic-batch-v1"
    assert "topic.creation.enable" not in config  # sinks never provision topics
    assert "gcs.credentials.json" not in config  # optional, omitted when unset


@pytest.mark.asyncio
async def test_gcs_sink_includes_credentials_when_configured(fake_postgres) -> None:
    template = {"connector_class": GCS_SINK}
    connection = _connection({"bucket": "my-lake", "credentials_json": '{"type": "service_account"}'})

    config, _ = await build_connector(_pipeline(), connection, template, schema=None)

    assert config["gcs.credentials.json"] == '{"type": "service_account"}'


@pytest.mark.asyncio
async def test_gcs_sink_requires_bucket(fake_postgres) -> None:
    template = {"connector_class": GCS_SINK}
    with pytest.raises(ValueError, match="bucket"):
        await build_connector(_pipeline(), _connection({}), template, schema=None)


# ── ADLS Gen2 sink ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adls_gen2_sink_builds_storage_common_config(fake_postgres) -> None:
    template = {"connector_class": ADLS_GEN2_SINK}
    connection = _connection({
        "account_name": "mydatalake", "filesystem": "raw", "client_id": "app-id",
        "client_secret": "app-secret", "tenant_id": "tenant-1",
    })

    config, produced_topic = await build_connector(_pipeline(), connection, template, schema=None)

    assert produced_topic is None
    assert config["connector.class"] == ADLS_GEN2_SINK
    assert config["azure.datalake.gen2.account.name"] == "mydatalake"
    assert config["azure.datalake.gen2.filesystem"] == "raw"
    assert config["azure.datalake.gen2.client.id"] == "app-id"
    assert config["azure.datalake.gen2.client.key"] == "app-secret"
    assert config["azure.datalake.gen2.token.endpoint"] == "https://login.microsoftonline.com/tenant-1/oauth2/token"
    assert config["storage.class"] == "io.confluent.connect.azure.datalake.gen2.storage.DataLakeGen2Storage"
    assert "topic.creation.enable" not in config


@pytest.mark.asyncio
async def test_adls_gen2_sink_requires_account_and_filesystem(fake_postgres) -> None:
    template = {"connector_class": ADLS_GEN2_SINK}
    with pytest.raises(ValueError, match="account_name"):
        await build_connector(_pipeline(), _connection({}), template, schema=None)


# ── Iceberg sink ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_iceberg_sink_builds_catalog_config(fake_postgres) -> None:
    template = {"connector_class": ICEBERG_SINK}
    connection = _connection({
        "warehouse": "s3://my-lake/warehouse", "catalog_name": "prod", "catalog_type": "rest",
        "catalog_uri": "https://catalog.internal:8181", "namespace": "analytics",
    })

    config, produced_topic = await build_connector(_pipeline(), connection, template, schema=None)

    assert produced_topic is None
    assert config["connector.class"] == ICEBERG_SINK
    assert config["iceberg.warehouse"] == "s3://my-lake/warehouse"
    assert config["iceberg.name"] == "prod"
    assert config["iceberg.type"] == "rest"
    assert config["iceberg.uri"] == "https://catalog.internal:8181"
    assert config["table.namespace"] == "analytics"
    assert config["table.auto-create"] == "true"
    # Iceberg's own config shape has no storage-common format/partitioner keys.
    assert "format.class" not in config
    assert "partitioner.class" not in config
    assert "topic.creation.enable" not in config
    # Verified against a live deploy: without schemas.enable=true the connector
    # throws "Failed to get event schema, event schema has no fields!" on its
    # first record — it derives the Iceberg table schema from each record's own
    # embedded Connect schema, unlike the schemaless storage-common sinks above.
    assert config["value.converter.schemas.enable"] == "true"
    assert config["key.converter.schemas.enable"] == "true"
    # Verified against a live deploy: upsert=true (the connector's own default)
    # reads Debezium CDC envelope fields to decide insert/update/delete, and
    # NullPointerExceptions on a plain (non-CDC) record that lacks them.
    assert config["upsert"] == "false"


@pytest.mark.asyncio
async def test_iceberg_sink_upsert_opts_in_via_connector_config(fake_postgres) -> None:
    """A pipeline reading a genuine Debezium CDC topic can opt back into
    upsert/delta mode explicitly."""
    template = {"connector_class": ICEBERG_SINK}
    connection = _connection({"warehouse": "hdfs://warehouse"})
    pipeline = _pipeline(connector_config={"topics": "raw-t-s-some_input_topic-batch-v1", "upsert": True})

    config, _ = await build_connector(pipeline, connection, template, schema=None)

    assert config["upsert"] == "true"


@pytest.mark.asyncio
async def test_iceberg_sink_defaults_catalog_when_unset(fake_postgres) -> None:
    template = {"connector_class": ICEBERG_SINK}
    connection = _connection({"warehouse": "hdfs://warehouse"})

    config, _ = await build_connector(_pipeline(), connection, template, schema=None)

    assert config["iceberg.name"] == "default"
    assert config["iceberg.type"] == "hadoop"
    assert config["table.namespace"] == "default"
    assert "iceberg.uri" not in config


@pytest.mark.asyncio
async def test_iceberg_sink_requires_warehouse(fake_postgres) -> None:
    template = {"connector_class": ICEBERG_SINK}
    with pytest.raises(ValueError, match="warehouse"):
        await build_connector(_pipeline(), _connection({}), template, schema=None)


# ── connection_config_schema validation for the new source types ────────────

def test_iceberg_connection_requires_warehouse() -> None:
    errors = validate_connection_config("iceberg", {})
    assert any("warehouse" in e for e in errors)
    assert not validate_connection_config("iceberg", {"warehouse": "s3://bucket/warehouse"})


def test_adls_gen2_connection_requires_core_fields() -> None:
    errors = validate_connection_config("adls_gen2", {})
    assert any("account_name" in e for e in errors)
    assert any("filesystem" in e for e in errors)
    assert any("client_id" in e for e in errors)
    assert any("client_secret" in e for e in errors)
    assert not validate_connection_config("adls_gen2", {
        "account_name": "acct", "filesystem": "fs", "client_id": "id", "client_secret": "secret",
    })

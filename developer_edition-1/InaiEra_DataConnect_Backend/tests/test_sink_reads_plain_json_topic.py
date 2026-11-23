"""
A sink's JsonConverter must match how its input topic was written. Found in the 2026-10-28 large-data test: a JDBC sink reading a CDC
(Debezium) topic — plain JSON, no embedded schema — failed every record at the converter, skipped all 101,500 of them under
errors.tolerance=all and wrote 0 rows, while Kafka Connect and every page showed it RUNNING. Only Mongo-sourced topics were handled.
"""

import pytest

from app.services.connect_config import DBZ_PG, ES_SINK, JDBC_SINK, JDBC_SOURCE, build_connector
from tests.conftest import _FakeRow

CDC_TOPIC = "raw-sales-pg-orders-cdc-v1"
JDBC_TOPIC = "raw-sales-pg-orders-batch-v1"


def _producer(fake_postgres, klass: str, topic: str, schemas_enable: str) -> None:
    fake_postgres.pipelines[f"src-{topic}"] = _FakeRow({
        "id": f"src-{topic}", "org_id": "org_1", "name": f"source-of-{topic}", "topic_name": topic,
        "generated_config": {"connector.class": klass, "value.converter": "org.apache.kafka.connect.json.JsonConverter",
                             "value.converter.schemas.enable": schemas_enable},
    })


def _sink(topic: str) -> dict:
    return {
        "id": "sink-1", "org_id": "org_1", "domain": "sales", "source_system": "pg", "entity": "orders_copy", "version": "v1",
        "topic_name": None, "connector_name": "sales--pg--orders_copy--stream--v1", "connector_config": {"topics": topic},
    }


def _connection() -> dict:
    return {"connection_config": {"hostname": "db.internal", "port": 5432, "database": "target", "username": "svc", "password": "pw"}}


@pytest.mark.asyncio
async def test_a_jdbc_sink_on_a_cdc_topic_is_refused_with_the_reason(fake_postgres) -> None:
    _producer(fake_postgres, DBZ_PG, CDC_TOPIC, "false")
    with pytest.raises(ValueError, match="plain JSON without a schema") as err:
        await build_connector(_sink(CDC_TOPIC), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)
    message = str(err.value)
    assert CDC_TOPIC in message
    assert f"source-of-{CDC_TOPIC}" in message
    assert "plain JSON without a schema" in message
    assert "every record would be skipped" in message


@pytest.mark.asyncio
async def test_a_jdbc_sink_on_a_jdbc_source_topic_still_deploys_with_schemas(fake_postgres) -> None:
    _producer(fake_postgres, JDBC_SOURCE, JDBC_TOPIC, "true")
    config, _ = await build_connector(_sink(JDBC_TOPIC), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)
    assert config["value.converter.schemas.enable"] == "true"


@pytest.mark.asyncio
async def test_a_sink_that_can_take_plain_json_is_switched_to_it_for_a_cdc_topic(fake_postgres) -> None:
    _producer(fake_postgres, DBZ_PG, CDC_TOPIC, "false")
    es_sink = {"connector_class": ES_SINK, "connector_type": "sink"}       # schema.ignore=true: it indexes plain JSON documents
    config, _ = await build_connector(_sink(CDC_TOPIC), {"connection_config": {"hostname": "es.internal", "port": 9200}}, es_sink, schema=None)
    assert config["value.converter"] == "org.apache.kafka.connect.json.JsonConverter"
    assert config["value.converter.schemas.enable"] == "false"
    assert config["key.converter.schemas.enable"] == "false"


@pytest.mark.asyncio
async def test_a_topic_nothing_in_the_org_produced_keeps_the_platform_default(fake_postgres) -> None:
    config, _ = await build_connector(_sink("raw-sales-pg-handmade-batch-v1"), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)
    assert config["value.converter.schemas.enable"] == "true"

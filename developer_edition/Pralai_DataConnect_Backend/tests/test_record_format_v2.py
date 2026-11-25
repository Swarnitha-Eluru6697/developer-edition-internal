"""
The 2026-11-02 MySQL large-data test, fixed (record_format "v2" on new pipelines; migration 0056):

  money          a Postgres NUMERIC(12,2) became a double (sums off by float error), while MySQL's DECIMAL stayed an exact decimal that the
                 quality worker could not read (every MySQL record failed its price rules) -> v2 keeps exact decimals on both, the worker
                 decodes them (for every pipeline, v1 too);
  duplicates     a JDBC source restarted mid-load re-sent rows that an insert-only sink stored twice -> a v2 JDBC source keys each record
                 by the table's primary key and a v2 JDBC sink upserts by it;
  CDC -> sink    a CDC topic was the Debezium envelope as plain JSON, which no database sink can read -> a v2 CDC source writes the row
                 with its schema, deletes as tombstones, and a v2 JDBC sink upserts and deletes;
  MySQL test     the connection test only checked the port -> it logs in;
  readiness      a config that couldn't be built was also reported as "plugin not installed".

v1 (every pipeline created before) must keep producing exactly what it did: the reconciler re-applies each connector's config on every tick.
"""

import base64
import json
import types

import pytest

from app.services import connect_config
from app.services.connect_config import (
    DBZ_MYSQL,
    DBZ_PG,
    JDBC_SINK,
    JDBC_SOURCE,
    _build_connector,
    _changes_key,
    _upsert_settings,
    build_connector,
    record_format_of,
)
from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality import evaluate_record
from app.services.quality_stream_worker import (
    QualityStreamWorker,
    _decode_logical_types,
    _unwrap_connect_envelope,
)
from tests.conftest import _FakeRow

TOPIC = "raw-shop-pg-orders-batch-v1"
SCHEMA = {"schema_definition": {"fields": [{"name": "row_id", "primary_key": True}, {"name": "price"}]}}


def _connection(source_type="postgresql"):
    return {"source_type": source_type,
            "connection_config": {"hostname": "db", "port": 5432, "database": "shop", "username": "u", "password": "p"}}


def _source(record_format=None, **cc):
    p = {"id": "p1", "org_id": "org_1", "domain": "shop", "source_system": "pg", "entity": "orders", "version": "v1", "mode": "batch",
         "topic_name": TOPIC, "connector_name": "shop--pg--orders--batch--v1",
         "connector_config": {"tables": ["orders"], "load_mode": "incrementing", **cc}}
    if record_format:
        p["record_format"] = record_format
    return p


def _jdbc(pipeline, transforms=None, schema=SCHEMA):
    return _build_connector(pipeline, _connection(), {"connector_class": JDBC_SOURCE}, schema,
                            {"transforms": transforms} if transforms else None)[0]


def _cdc(pipeline, klass=DBZ_PG, transforms=None):
    return _build_connector(pipeline, _connection(), {"connector_class": klass}, SCHEMA, {"transforms": transforms} if transforms else None)[0]


def _names(config):
    return config.get("transforms", "").split(",")


VALUE_TO_KEY = [{"order": 1, "name": "ValueToKey", "type": "ValueToKey", "scope": "value", "config": {"fields": "order_id"}}]
MASK = [{"order": 1, "name": "MaskField", "type": "MaskField", "scope": "value", "config": {"fields": "email", "replacement": "x"}}]


# ── record_format itself ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("value", "expected"), [("v2", "v2"), ("v1", "v1"), (None, "v1"), ("", "v1"), ("v3", "v1")])
def test_only_an_explicit_v2_is_v2(value, expected):
    assert record_format_of({"record_format": value}) == expected
    assert record_format_of(None) == "v1"


# ── JDBC source ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("record_format", [None, "v1"])
def test_a_v1_jdbc_source_is_exactly_what_it_was(record_format):
    c = _jdbc(_source(record_format))
    assert c["numeric.mapping"] == "best_fit"
    assert "pkkey" not in _names(c)
    assert "transforms.pkkey.type" not in c


def test_a_v2_jdbc_source_keeps_decimals_exact_and_keys_by_the_primary_key_before_the_users_transforms():
    c = _jdbc(_source("v2"), MASK)
    assert c["numeric.mapping"] == "precision_only"
    assert _names(c)[0] == "pkkey"
    assert _names(c)[-1] == "reroute"
    assert c["transforms.pkkey.type"] == "org.apache.kafka.connect.transforms.ValueToKey"
    assert c["transforms.pkkey.fields"] == "row_id"
    assert c["key.converter.schemas.enable"] == "true"
    assert c["incrementing.column.name"] == "row_id"


def test_a_composite_primary_key_keys_by_every_column():
    schema = {"schema_definition": {"fields": [{"name": "a", "primary_key": True}, {"name": "b", "primary_key": True}]}}
    assert _jdbc(_source("v2"), schema=schema)["transforms.pkkey.fields"] == "a,b"


def test_without_a_primary_key_or_with_a_users_key_field_there_is_no_platform_key():
    assert "transforms.pkkey.type" not in _jdbc(_source("v2"), schema={"schema_definition": {"fields": [{"name": "x"}]}})
    c = _jdbc(_source("v2", key_field="order_id"))
    assert "transforms.pkkey.type" not in c
    assert c["key.converter"] == "org.apache.kafka.connect.storage.StringConverter"


# ── CDC source ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("klass", [DBZ_PG, DBZ_MYSQL])
def test_a_v1_cdc_source_is_exactly_what_it_was(klass):
    c = _cdc(_source(None, ), klass)
    assert c["value.converter.schemas.enable"] == "false"
    assert c["decimal.handling.mode"] == "double"
    assert "unwrap" not in _names(c)
    assert "time.precision.mode" not in c


@pytest.mark.parametrize("klass", [DBZ_PG, DBZ_MYSQL])
def test_a_v2_cdc_source_writes_rows_with_a_schema_exact_decimals_and_tombstones(klass):
    c = _cdc(_source("v2"), klass, MASK)
    assert c["value.converter.schemas.enable"] == "true"
    assert c["key.converter.schemas.enable"] == "true"
    assert c["decimal.handling.mode"] == "precise"
    assert c["time.precision.mode"] == "connect"
    assert _names(c)[0] == "unwrap"
    assert _names(c)[-1] == "reroute"
    assert c["transforms.unwrap.type"] == "io.debezium.transforms.ExtractNewRecordState"
    assert c["transforms.unwrap.delete.handling.mode"] == "rewrite"
    assert c["transforms.unwrap.drop.tombstones"] == "false"


def test_a_v2_cdc_source_unwraps_even_without_user_transforms():
    assert _names(_cdc(_source("v2")))[0] == "unwrap"


def test_a_users_own_unwrap_is_not_doubled():
    mine = [{"order": 1, "name": "ExtractNewRecordState", "type": "ExtractNewRecordState", "scope": "value", "config": {}}]
    c = _cdc(_source("v2"), DBZ_PG, mine)
    assert sum(1 for n in _names(c) if c.get(f"transforms.{n}.type") == "io.debezium.transforms.ExtractNewRecordState") == 1


# ── which keys a sink may upsert by ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_user_transform_that_rewrites_the_key_is_seen():
    assert not _changes_key(_jdbc(_source("v2"), MASK))
    assert _changes_key(_jdbc(_source("v2"), VALUE_TO_KEY))
    assert _changes_key({"transforms": "pkkey,h", "transforms.h.type": "org.apache.kafka.connect.transforms.HoistField$Key"})


def _producer(record_format="v2", config=None, tables=("orders",), schema_version_id=None):
    return _FakeRow({"record_format": record_format, "generated_config": config, "connector_config": {"tables": list(tables)},
                     "schema_version_id": schema_version_id, "name": "src"})


@pytest.mark.asyncio
async def test_a_v2_jdbc_producer_keyed_by_its_primary_key_makes_the_sink_upsert():
    assert await _upsert_settings("org_1", _producer(config=_jdbc(_source("v2")))) == {
        "insert.mode": "upsert", "pk.mode": "record_key", "pk.fields": "row_id"}


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["v1 producer", "user key", "two tables", "all tables", "string key", "no key"])
async def test_otherwise_the_sink_keeps_inserting(why):
    producer = {
        "v1 producer": _producer("v1", _jdbc(_source("v1"))),
        "user key": _producer(config=_jdbc(_source("v2"), VALUE_TO_KEY)),
        "two tables": _producer(config=_jdbc(_source("v2")), tables=("a", "b")),
        "all tables": _producer(config=_jdbc(_source("v2")), tables=("*",)),
        "string key": _producer(config=_jdbc(_source("v2", key_field="order_id"))),
        "no key": _producer(config=_jdbc(_source("v2"), schema={"schema_definition": {"fields": [{"name": "x"}]}})),
    }[why]
    assert await _upsert_settings("org_1", producer) == {}


VALUE_TO_KEY_PK = [{"order": 1, "name": "ValueToKey", "type": "ValueToKey", "scope": "value", "config": {"fields": "row_id"}}]


def test_a_value_to_key_on_exactly_the_primary_key_is_the_same_key():
    assert not _changes_key(_jdbc(_source("v2"), VALUE_TO_KEY_PK), ["row_id"])
    assert _changes_key(_jdbc(_source("v2"), VALUE_TO_KEY_PK), None)                    # without knowing the key: assume it changed
    assert _changes_key(_jdbc(_source("v2"), VALUE_TO_KEY), ["row_id"])


@pytest.mark.asyncio
async def test_the_forms_standard_value_to_key_on_the_primary_key_still_upserts():
    assert await _upsert_settings("org_1", _producer(config=_jdbc(_source("v2"), VALUE_TO_KEY_PK))) == {
        "insert.mode": "upsert", "pk.mode": "record_key", "pk.fields": "row_id"}


@pytest.mark.asyncio
async def test_only_a_producer_recorded_as_v2_is_trusted_whatever_its_stored_config_says():
    """generated_config is a snapshot written at deploy; record_format is what the producer row IS. A v1 row never makes a sink upsert."""
    assert await _upsert_settings("org_1", _producer("v1", _jdbc(_source("v2")))) == {}


@pytest.mark.asyncio
async def test_a_v2_cdc_producer_makes_the_sink_upsert_by_the_tables_key_and_delete(monkeypatch):
    async def _schema(org_id, schema_id):
        return SCHEMA if schema_id == "s1" else {"schema_definition": {"fields": [{"name": "x"}]}}

    monkeypatch.setattr("app.services.schema_service.schema_service.get_schema", _schema)
    cdc = _cdc(_source("v2"))
    assert await _upsert_settings("org_1", _producer(config=cdc, schema_version_id="s1")) == {
        "insert.mode": "upsert", "pk.mode": "record_key", "pk.fields": "row_id", "delete.enabled": "true"}
    assert await _upsert_settings("org_1", _producer(config=cdc, schema_version_id="s2")) == {}     # a table without a primary key
    assert await _upsert_settings("org_1", _producer(config=cdc)) == {}                           # no schema known


# ── the sink, end to end through build_connector ─────────────────────────────────────────────────────────────────────────────────────
def _sink(record_format=None, topic=TOPIC):
    p = {"id": "s1", "org_id": "org_1", "domain": "shop", "source_system": "pg", "entity": "orders_copy", "version": "v1",
         "topic_name": None, "connector_name": "shop--pg--orders_copy--stream--v1", "connector_config": {"topics": topic}}
    if record_format:
        p["record_format"] = record_format
    return p


def _register(fake_postgres, config, record_format="v2", schema_version_id=None):
    fake_postgres.pipelines["src"] = _FakeRow({
        "id": "src", "org_id": "org_1", "name": "orders-cdc", "topic_name": TOPIC, "record_format": record_format,
        "generated_config": config, "connector_config": {"tables": ["orders"]}, "schema_version_id": schema_version_id})


@pytest.mark.asyncio
async def test_a_v2_jdbc_sink_reads_a_v2_cdc_topic_and_upserts_and_deletes(fake_postgres, monkeypatch):
    async def _schema(org_id, schema_id):
        return SCHEMA

    monkeypatch.setattr("app.services.schema_service.schema_service.get_schema", _schema)
    _register(fake_postgres, _cdc(_source("v2")), schema_version_id="s1")
    config, _ = await build_connector(_sink("v2"), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)
    assert config["value.converter.schemas.enable"] == "true"
    assert (config["insert.mode"], config["pk.mode"], config["pk.fields"], config["delete.enabled"]) == ("upsert", "record_key", "row_id", "true")


@pytest.mark.asyncio
async def test_a_v1_sink_keeps_inserting_whatever_its_producer(fake_postgres):
    _register(fake_postgres, _jdbc(_source("v2")))
    config, _ = await build_connector(_sink(), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)
    assert (config["insert.mode"], config["pk.mode"]) == ("insert", "none")


@pytest.mark.asyncio
async def test_a_v1_cdc_topic_is_still_refused_to_a_jdbc_sink(fake_postgres):
    _register(fake_postgres, _cdc(_source("v1")), record_format="v1")
    with pytest.raises(ValueError, match="plain JSON without a schema"):
        await build_connector(_sink("v2"), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)


# ── the quality worker reads exact decimals ───────────────────────────────────────────────────────────────────────────────────────────
def _b64(unscaled: int) -> str:
    return base64.b64encode(unscaled.to_bytes((unscaled.bit_length() + 8) // 8 or 1, "big", signed=True)).decode()


def _decimal_field(name, scale):
    return {"field": name, "type": "bytes", "name": "org.apache.kafka.connect.data.Decimal", "parameters": {"scale": str(scale)}}


def test_a_connect_decimal_is_decoded_with_its_scale():
    schema = {"type": "struct", "fields": [_decimal_field("price", 2), _decimal_field("qty", 0), _decimal_field("neg", 2),
                                           {"field": "name", "type": "string"}]}
    payload = {"price": _b64(303094), "qty": _b64(7), "neg": _b64(-1250), "name": "ITcg"}
    assert _decode_logical_types(schema, payload) == {"price": 3030.94, "qty": 7, "neg": -12.5, "name": "ITcg"}


def test_the_mysql_record_that_failed_every_price_rule_now_passes():
    """2026-11-02: row 9's unit_price, 21768.00 in MySQL, arrived as "ITcg" and min_max_numeric failed on every MySQL record."""
    envelope = {"schema": {"type": "struct", "fields": [_decimal_field("unit_price", 2)]}, "payload": {"unit_price": "ITcg"}}
    rule = {"name": "unit_price_range", "type": "min_max_numeric", "field": "unit_price", "min": 18.5, "max": 323000}
    assert evaluate_record(envelope["payload"], {"rules": [rule]})["valid"] is False                 # what happened
    decoded = _unwrap_connect_envelope(envelope)
    assert decoded == {"unit_price": 21768.0}
    assert evaluate_record(decoded, {"rules": [rule]})["valid"] is True


def test_debezium_variable_scale_decimals_nested_structs_and_arrays_are_decoded():
    vsd = {"field": "amount", "type": "struct", "name": "io.debezium.data.VariableScaleDecimal",
           "fields": [{"field": "scale", "type": "int32"}, {"field": "value", "type": "bytes"}]}
    schema = {"type": "struct", "fields": [vsd, {"field": "items", "type": "array", "items": _decimal_field(None, 1)},
                                           {"field": "inner", "type": "struct", "fields": [_decimal_field("p", 3)]}]}
    payload = {"amount": {"scale": 3, "value": _b64(12345)}, "items": [_b64(15), _b64(-5)], "inner": {"p": _b64(1)}}
    assert _decode_logical_types(schema, payload) == {"amount": 12.345, "items": [1.5, -0.5], "inner": {"p": 0.001}}


def test_what_is_not_a_decimal_is_left_alone():
    schema = {"type": "struct", "fields": [_decimal_field("bad", 2), _decimal_field("none", 2)]}
    assert _decode_logical_types(schema, {"bad": "not base64!", "none": None}) == {"bad": "not base64!", "none": None}
    assert _unwrap_connect_envelope({"price": "ITcg"}) == {"price": "ITcg"}                        # no envelope: as-is


# ── the quality worker passes deletes on ──────────────────────────────────────────────────────────────────────────────────────────────
PIPE = {"id": "pipe_1", "org_id": "org_1", "direction": "SOURCE", "topic_name": TOPIC, "zone": "raw", "domain": "shop",
        "source_system": "pg", "entity": "orders", "mode": "batch", "version": "v1", "schema_version_id": None, "connector_config": {},
        "quality_policy_id": "q1"}
POLICY = {"id": "q1", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
          "rules": [{"name": "r", "type": "min_max_numeric", "field": "price", "min": 1, "max": 10}]}


@pytest.fixture
def kafka(monkeypatch):
    box = types.SimpleNamespace(producer=InMemoryKafkaProducer(), outcomes=[], dead=[])

    async def _ensure(topic, **kwargs):
        pass

    async def _producer():
        return box.producer

    async def _record_outcome(self, *, valid, **kwargs):
        box.outcomes.append(valid)

    async def _dead_letter(self, **kwargs):
        box.dead.append(kwargs)

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    monkeypatch.setattr(QualityStreamWorker, "_dead_letter", _dead_letter)
    return box


def _tombstone():
    return KafkaRecord(topic=TOPIC, key='{"schema":{},"payload":{"row_id":8}}', value={"_raw": None}, partition=0, offset=5)


@pytest.mark.asyncio
async def test_enforce_forwards_a_tombstone_under_its_key_without_counting_it(kafka):
    worker = QualityStreamWorker(org_id="org_1", pipeline={**PIPE, "quality_mode": "ENFORCE"}, policy=POLICY)
    await worker._process(_tombstone())
    [sent] = kafka.producer.records
    assert sent.topic == "prepared-shop-pg-orders-batch-v1"
    assert sent.value is None
    assert sent.key == _tombstone().key
    assert kafka.outcomes == []
    assert kafka.dead == []


@pytest.mark.asyncio
async def test_observe_ignores_a_tombstone(kafka):
    worker = QualityStreamWorker(org_id="org_1", pipeline={**PIPE, "quality_mode": "OBSERVE"}, policy=POLICY)
    await worker._process(_tombstone())
    assert kafka.producer.records == []
    assert kafka.outcomes == []
    assert kafka.dead == []


@pytest.mark.asyncio
async def test_an_unreadable_record_is_still_dead_lettered(kafka):
    worker = QualityStreamWorker(org_id="org_1", pipeline={**PIPE, "quality_mode": "ENFORCE"}, policy=POLICY)
    await worker._process(KafkaRecord(topic=TOPIC, key=None, value={"_raw": "not json"}, partition=0, offset=6))
    assert [d["reason"] for d in kafka.dead] == ["undecodable_record"]


@pytest.mark.asyncio
async def test_a_forwarded_record_keeps_its_exact_decimal_bytes(kafka):
    """The rules read the decoded copy; the sink must still get the exact value Connect wrote."""
    worker = QualityStreamWorker(org_id="org_1", pipeline={**PIPE, "quality_mode": "ENFORCE"}, policy=POLICY)
    envelope = {"schema": {"type": "struct", "fields": [_decimal_field("price", 2)]}, "payload": {"price": _b64(250)}}
    await worker._process(KafkaRecord(topic=TOPIC, key="k", value={"_raw": json.dumps(envelope)}, partition=0, offset=7))
    [sent] = kafka.producer.records
    assert sent.value == envelope
    assert kafka.outcomes == [True]


def test_the_real_producer_sends_none_as_a_tombstone():
    import inspect

    from app.services import kafka_runtime

    assert "None if v is None" in inspect.getsource(kafka_runtime.NativeKafkaProducer)


# ── MySQL connection test logs in ─────────────────────────────────────────────────────────────────────────────────────────────────────
class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql):
        self.sql = sql

    async def fetchone(self):
        return ("8.0.46",)


class _Conn:
    closed = False

    def cursor(self):
        return _Cursor()

    def close(self):
        _Conn.closed = True


@pytest.fixture
def mysql(monkeypatch):
    import aiomysql

    from app.services import connection_adapters

    async def _allowed(host, org_id):
        return None

    monkeypatch.setattr(connection_adapters, "_validated", _allowed)
    box = types.SimpleNamespace(kwargs=None, fail=None)

    async def _connect(**kwargs):
        box.kwargs = kwargs
        if box.fail:
            raise box.fail
        return _Conn()

    monkeypatch.setattr(aiomysql, "connect", _connect)
    return box


MYSQL = {"source_type": "mysql", "connection_config": {"hostname": "mysql80", "port": 3306, "database": "dataconnect",
                                                       "username": "dataconnect", "password": "pw"}}


@pytest.mark.asyncio
async def test_a_mysql_connection_test_logs_in(mysql):
    from app.services.connection_adapters import live_probe

    result = await live_probe(MYSQL)
    assert result.status == "ok"
    assert result.message == "Authenticated to MySQL mysql80:3306/dataconnect"
    assert result.details["auth"] is True
    assert result.details["server_version"] == "8.0.46"
    assert (mysql.kwargs["user"], mysql.kwargs["password"], mysql.kwargs["db"]) == ("dataconnect", "pw", "dataconnect")
    assert _Conn.closed


@pytest.mark.asyncio
async def test_a_wrong_mysql_password_fails_the_test(mysql):
    from app.services.connection_adapters import live_probe

    mysql.fail = RuntimeError("(1045, \"Access denied for user 'dataconnect'@'172.18.0.7' (using password: YES)\")")
    result = await live_probe(MYSQL)
    assert result.status == "failed"
    assert "Access denied" in result.message
    assert result.details["auth"] is False


# ── readiness: a config that can't be built is not "plugin not installed" ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize(("installed", "plugin_ok"), [(True, True), (False, False)])
async def test_a_config_that_cant_be_built_fails_compatibility_and_the_plugin_check_answers_for_itself(
        fake_postgres, monkeypatch, installed, plugin_ok):
    from app.core.config import settings
    from app.schemas.pipeline import PipelineCreate
    from app.services.pipeline_service import pipeline_service
    from app.services.runtime_orchestrator import KafkaConnectClient

    template = fake_postgres.create("connector_templates", {"connector_class": JDBC_SOURCE, "connector_type": "source", "default_config": {}})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_rf", "name": "c", "connection_config": {}, "verification_status": "ok"})
    created = await pipeline_service.create_pipeline("org_rf", PipelineCreate(
        name="p", template_id=template["id"], connection_profile_id=connection["id"], zone="raw", domain="sales", source_system="app",
        entity="orders", mode="batch", version="v1"))
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def _plugins(self):
        return [{"class": JDBC_SOURCE, "type": "source", "version": "10.9.5"}] if installed else []

    async def _cannot_build(*args, **kwargs):
        raise ValueError("This sink can't read topic 'x'")

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _plugins)
    monkeypatch.setattr(connect_config, "build_connector", _cannot_build)
    result = await pipeline_service.check_deploy_readiness("org_rf", created["id"])
    checks = {c["key"]: c for c in result["checks"]}
    assert checks["connector_compatibility"]["ok"] is False
    assert "can't read topic" in checks["connector_compatibility"]["reason"]
    assert checks["connector_plugin_installed"]["ok"] is plugin_ok
    if plugin_ok:
        assert checks["connector_plugin_installed"]["reason"] is None
    else:
        assert "is not installed" in checks["connector_plugin_installed"]["reason"]


@pytest.mark.asyncio
async def test_a_v2_sink_reading_an_enforce_sources_prepared_topic_still_upserts(fake_postgres):
    """A sink may only read an ENFORCE pipeline's PREPARED topic, which carries that pipeline's own records unchanged. Its producer used to
    be looked up by topic name alone, found nothing for a prepared topic, and the sink fell back to plain inserts: an updated row then
    failed on the table's primary key."""
    _register(fake_postgres, _jdbc(_source("v2")))
    fake_postgres.pipelines["src"].__dict__.update(
        {"quality_mode": "ENFORCE", "domain": "shop", "source_system": "pg", "entity": "orders", "mode": "batch", "version": "v1"})
    prepared = "prepared-shop-pg-orders-batch-v1"

    config, _ = await build_connector(_sink("v2", topic=prepared), _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"},
                                      schema=None)

    assert (config["insert.mode"], config["pk.mode"], config["pk.fields"]) == ("upsert", "record_key", "row_id")


@pytest.mark.asyncio
async def test_a_prepared_topic_name_of_an_observe_pipeline_finds_no_producer(fake_postgres):
    """OBSERVE writes no prepared topic, so a topic merely NAMED like one is not that pipeline's: the sink keeps inserting."""
    _register(fake_postgres, _jdbc(_source("v2")))
    fake_postgres.pipelines["src"].__dict__.update(
        {"quality_mode": "OBSERVE", "domain": "shop", "source_system": "pg", "entity": "orders", "mode": "batch", "version": "v1"})

    config, _ = await build_connector(_sink("v2", topic="prepared-shop-pg-orders-batch-v1"), _connection(),
                                      {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None)

    assert (config["insert.mode"], config["pk.mode"]) == ("insert", "none")

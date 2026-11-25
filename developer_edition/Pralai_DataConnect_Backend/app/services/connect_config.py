"""
Turn an app pipeline into a REAL, deployable Kafka Connect connector config,
for every connector class whose plugin is installed on the worker.

Sources : JDBC (Postgres/MySQL), Debezium (PG/MySQL/Mongo/SQLServer/Oracle),
          MongoDB (standard), S3.
Sinks   : JDBC (Postgres/MySQL), Elasticsearch, MongoDB (standard), S3, GCS,
          Azure Data Lake Gen2, Apache Iceberg.

The pipeline's connection profile supplies host/port/db/credentials (+ bucket/
region/keys for S3, url for ES); the inferred schema supplies the PK for
incrementing-mode JDBC sources. Sources reroute their output to the pipeline's
canonical `topic_name` so the UI's topic matches reality.
"""

from app.core.config import settings
from app.core.error_policies import default_policy
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import PipelineRepository
from app.services.connect_config_t1 import _with_key, build_t1_connector
from app.services.debezium_identifiers import (
    debezium_publication_name,
    debezium_server_id,
    debezium_slot_name,
    debezium_topic_prefix,
)
from app.services.pipeline_config import error_handling_connect_config
from app.services.quality_mode import prepared_topic_for
from app.services.secret_store import hydrate_connection_secrets
from app.services.secrets import resolve_secret
from app.services.sink_topics import SinkTopicError, normalize_sink_topics
from app.services.topic_policy import (
    resolve_topic_config,
    schema_strategy_converter_config,
    topic_creation_connect_config,
)

# ── connector classes ───────────────────────────────────────────────────────
JDBC_SOURCE = "io.confluent.connect.jdbc.JdbcSourceConnector"
JDBC_SINK = "io.confluent.connect.jdbc.JdbcSinkConnector"
MONGO_SOURCE = "com.mongodb.kafka.connect.MongoSourceConnector"
MONGO_SINK = "com.mongodb.kafka.connect.MongoSinkConnector"
ES_SINK = "io.confluent.connect.elasticsearch.ElasticsearchSinkConnector"
S3_SINK = "io.confluent.connect.s3.S3SinkConnector"
S3_SOURCE = "io.confluent.connect.s3.source.S3SourceConnector"
GCS_SINK = "io.confluent.connect.gcs.GcsSinkConnector"
ADLS_GEN2_SINK = "io.confluent.connect.azure.datalake.gen2.AzureDataLakeGen2SinkConnector"
ICEBERG_SINK = "com.getindata.kafka.connect.iceberg.sink.IcebergSink"
DBZ_PG = "io.debezium.connector.postgresql.PostgresConnector"
DBZ_MYSQL = "io.debezium.connector.mysql.MySqlConnector"
DBZ_MONGO = "io.debezium.connector.mongodb.MongoDbConnector"
DBZ_MSSQL = "io.debezium.connector.sqlserver.SqlServerConnector"
DBZ_ORACLE = "io.debezium.connector.oracle.OracleConnector"
FILE_SOURCE = "org.apache.kafka.connect.file.FileStreamSourceConnector"
FILE_SINK = "org.apache.kafka.connect.file.FileStreamSinkConnector"
FILECHUNK_SOURCE = "com.github.streamsend.file.chunk.source.ChunkSourceConnector"
FILECHUNK_SINK = "com.github.streamsend.file.chunk.sink.ChunkSinkConnector"

_JSON_SCHEMA = {
    "key.converter": "org.apache.kafka.connect.json.JsonConverter", "key.converter.schemas.enable": "true",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter", "value.converter.schemas.enable": "true",
}
_JSON_PLAIN = {
    "key.converter": "org.apache.kafka.connect.json.JsonConverter", "key.converter.schemas.enable": "false",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter", "value.converter.schemas.enable": "false",
}
# Sinks that build what they write from each record's own embedded Connect schema: the JDBC sink (table rows, auto.create) and
# Iceberg (see its branch below). Neither can be fed plain JSON.
SCHEMA_REQUIRED_SINKS = frozenset({JDBC_SINK, ICEBERG_SINK})
_BYTES_PASSTHROUGH = {
    "key.converter": "org.apache.kafka.connect.converters.ByteArrayConverter",
    "value.converter": "org.apache.kafka.connect.converters.ByteArrayConverter",
}
_STRING_CONVERTER = {
    "key.converter": "org.apache.kafka.connect.storage.StringConverter", "key.converter.schemas.enable": "false",
    "value.converter": "org.apache.kafka.connect.storage.StringConverter", "value.converter.schemas.enable": "false",
}
_DEFAULT_PORT = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "mongodb": 27017, "elasticsearch": 9200}


def _host(cfg):
    return cfg.get("hostname") or cfg.get("host")


def _port(cfg, st):
    return int(cfg.get("port") or _DEFAULT_PORT.get(st, 5432))


# JDBC source polling. An incrementing pipeline only reads NEW rows each poll, so polling every few seconds is cheap and keeps it close to real time. A bulk
# pipeline re-reads the WHOLE table on every poll and publishes every row again, so at 5 s the topic fills with copies of the same rows; 5 minutes
# leaves time to look at one read. Either can be set per pipeline with the option `poll_interval_seconds`.
JDBC_POLL_MS = 5_000
JDBC_BULK_POLL_MS = 300_000


# Pipeline option `tasks_max` — how many tasks Kafka Connect may run for the connector. Every builder sets one task; only a connector
# that really divides its work accepts more: a JDBC source reads its tables across tasks (one task per table at most), a JDBC sink
# reads a topic's partitions across tasks (one task per partition at most). Connect starts fewer when there is less to divide, so
# asking for more than that is harmless. Every other connector refuses it: a Debezium connector, for one, only ever runs one task.
PARALLEL_TASK_CONNECTORS = frozenset({JDBC_SOURCE, JDBC_SINK})
MAX_TASKS = 16


def _tasks_max(cc: dict, klass: str | None) -> int | None:
    """The `tasks_max` a pipeline asked for, or None when it didn't. Raises ValueError (a 422 at deploy) for a value that can't work."""
    chosen = cc.get("tasks_max")
    if chosen is None or chosen == "":
        return None
    try:
        number = float(chosen)
        count = int(number)
        if count != number:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Pipeline option 'tasks_max' must be a whole number.") from exc
    if not 1 <= count <= MAX_TASKS:
        raise ValueError(f"Pipeline option 'tasks_max' must be between 1 and {MAX_TASKS}.")
    if count > 1 and klass not in PARALLEL_TASK_CONNECTORS:
        raise ValueError("Pipeline option 'tasks_max' above 1 is only supported for the JDBC source and JDBC sink connectors.")
    return count


def _jdbc_poll_interval_ms(cc: dict, mode: str) -> int:
    chosen = cc.get("poll_interval_seconds")
    if chosen is None or chosen == "":
        return JDBC_BULK_POLL_MS if mode == "bulk" else JDBC_POLL_MS
    try:
        ms = int(float(chosen) * 1000)
    except (TypeError, ValueError) as exc:
        raise ValueError("JDBC source option 'poll_interval_seconds' must be a number.") from exc
    if ms <= 0:
        raise ValueError("JDBC source option 'poll_interval_seconds' must be greater than zero.")
    return ms



def _jdbc_url(cfg, st):
    if st == "mysql":
        return f"jdbc:mysql://{_host(cfg)}:{_port(cfg, st)}/{cfg.get('database')}"
    elif st == "sqlserver":
        # encrypt=false: the mssql-jdbc driver has defaulted encrypt=true since
        # v9, which then requires a CA-trusted TLS cert from the server or the
        # driver fails the connection entirely (PKIX path building failed) —
        # a local/dev SQL Server never has one. Same fix already applied to
        # the Debezium SQL Server CDC path's database.encrypt config below;
        # this is the JDBC-URL-level equivalent for the plain JDBC source/sink.
        return f"jdbc:sqlserver://{_host(cfg)}:{_port(cfg, st)};databaseName={cfg.get('database')};encrypt=false"
    return f"jdbc:postgresql://{_host(cfg)}:{_port(cfg, st)}/{cfg.get('database')}"


def _mongo_uri(cfg, connection):
    user = cfg.get("username")
    pwd = resolve_secret(connection, "password", "password")
    auth = f"{user}:{pwd}@" if user else ""
    # The same database the schema check authenticates against (connection_discovery.py): the user's own
    # `auth_source`, else `admin` where MongoDB users are normally defined.
    src = f"?authSource={cfg.get('auth_source') or 'admin'}" if user else ""
    return f"mongodb://{auth}{_host(cfg)}:{_port(cfg, 'mongodb')}/{src}"


# Debezium names a table topic <prefix>.<schema>.<table>, except SQL Server, which adds the
# database: <prefix>.<database>.<schema>.<table>. The reroute pattern must match every segment.
_DBZ_DEFAULT_TAIL = r"\.[^.]+\.[^.]+"
_DBZ_TABLE_TOPIC_TAIL = {DBZ_MSSQL: r"\.[^.]+\.[^.]+\.[^.]+"}


def _reroute(regex, topic):
    return {
        "transforms": "reroute",
        "transforms.reroute.type": "org.apache.kafka.connect.transforms.RegexRouter",
        "transforms.reroute.regex": regex,
        "transforms.reroute.replacement": topic,
    }


def _combine(*smt_dicts):
    """Merge several SMT config dicts into one, concatenating their `transforms`
    name lists (so user-selected transforms + the source's reroute all deploy)."""
    names: list[str] = []
    out: dict = {}
    for d in smt_dicts:
        if not d:
            continue
        for k, v in d.items():
            if k == "transforms":
                names.extend(x for x in str(v).split(",") if x)
            else:
                out[k] = v
    if names:
        out["transforms"] = ",".join(names)
    return out


def _sink_topic(pipeline):
    # A quality_policy_id alone never redirects this: in OBSERVE mode (the default) the quality worker only evaluates and counts what is on
    # the configured topic (app/services/quality_stream_worker.py), so the sink reads exactly the topic the user configured. In ENFORCE
    # mode (NEW-01, app/services/quality_mode.py) the worker is the gate: it writes only the records that passed to the pipeline's
    # prepared topic, and the sink reads THAT, so it can never receive a record that failed. The configured topic is still validated
    # below (it is what the worker consumes).
    topics = (pipeline.get("connector_config") or {}).get("topics")
    if not topics:
        raise ValueError("Sink pipeline needs an input topic — set 'Source topic to read' in the form.")
    # GAP-12: re-checked here, not only at creation, because every deploy and every reconciler tick builds the
    # config through this function — a row edited outside the API, or created before the rule existed, still can't
    # make a sink read something that is not a topic name this platform builds.
    try:
        normalized = normalize_sink_topics(topics)
    except SinkTopicError as exc:
        raise ValueError(f"{exc.code}: {exc}") from exc
    return prepared_topic_for(pipeline) or normalized


# ── multi-table selection ────────────────────────────────────────────────────
# Every snapshot.mode name Debezium has used, in any connector or version. A value outside this set is not a mode at all
# (a typo, an old row) and still falls back to "initial", as it always has.
VALID_SNAPSHOT_MODES = {
    "always", "initial", "initial_only", "no_data", "never", "schema_only", "schema_only_recovery",
    "when_needed", "configuration_based", "custom",
}
# What the Debezium 2.5.4 plugins installed on Kafka Connect actually accept, per connector (checked against Connect's own
# config validator on 2026-10-28; MySQL's plugin is installed on demand, so its list is the Debezium 2.5 documentation's).
# The form used to offer one list for every Debezium connector, so e.g. PostgreSQL + "no_data" deployed and was rejected.
# "custom" is left out everywhere: it needs a snapshot.custom.class nobody can set from the form.
SNAPSHOT_MODES_BY_CLASS = {
    DBZ_PG: ("initial", "initial_only", "always", "never"),
    DBZ_MYSQL: ("initial", "initial_only", "when_needed", "never", "schema_only", "schema_only_recovery"),
    DBZ_MSSQL: ("initial", "initial_only", "schema_only"),
    DBZ_ORACLE: ("initial", "initial_only", "always", "schema_only", "schema_only_recovery"),
    DBZ_MONGO: ("initial", "never"),
}
# "no_data" is Debezium 2.6's name for "stream changes only, no snapshot of the rows" — "never" or "schema_only" in 2.5.
_STREAM_ONLY_MODE = {DBZ_PG: "never", DBZ_MYSQL: "never", DBZ_MONGO: "never", DBZ_MSSQL: "schema_only", DBZ_ORACLE: "schema_only"}


def _unqualified(t: str) -> str:
    """'public.orders' -> 'orders'."""
    return t.split(".")[-1]


def _qualify(t: str, schema: str) -> str:
    """'orders' -> 'public.orders'; leaves already-qualified names untouched."""
    return t if "." in t else f"{schema}.{t}"


def resolve_tables(pipeline: dict) -> list[str]:
    """The list of tables/collections this pipeline ingests.

    Reads connector_config.tables (a list or comma string; '*' is expanded to the
    real table list earlier, in deploy()). Falls back to [entity] when unset.
    """
    cc = pipeline.get("connector_config") or {}
    raw = cc.get("tables")
    if isinstance(raw, str):
        raw = [t.strip() for t in raw.split(",") if t.strip()]
    tables = [t for t in (raw or []) if t and t != "*"]
    return tables or [pipeline["entity"]]


def _snapshot_mode(pipeline: dict, klass: str | None = None) -> str:
    """The Debezium snapshot.mode to deploy. Raises ValueError (a refused deploy, with the modes that would work) for a real mode
    this connector does not support — Kafka Connect would reject it anyway, with a message that never reached the user."""
    mode = ((pipeline.get("connector_config") or {}).get("snapshot_mode") or "initial")
    if mode not in VALID_SNAPSHOT_MODES:
        return "initial"
    allowed = SNAPSHOT_MODES_BY_CLASS.get(klass or "")
    if not allowed:
        return mode
    if mode == "no_data":
        mode = _STREAM_ONLY_MODE[klass]
    if mode not in allowed:
        raise ValueError(
            f"Snapshot mode '{mode}' is not supported by this connector. Choose one of: {', '.join(allowed)}."
        )
    return mode


_UNWRAP_CLASS = "io.debezium.transforms.ExtractNewRecordState"
_TIMESTAMP_CONVERTER = "org.apache.kafka.connect.transforms.TimestampConverter"


def _dbz_unwrap():
    return {"transforms": "unwrap", "transforms.unwrap.type": _UNWRAP_CLASS,
            "transforms.unwrap.delete.handling.mode": "rewrite"}


# ── record_format (app/models/pipeline.py) ──────────────────────────────────
# "v1" is what every pipeline created before 2026-11-02 writes, and keeps writing: the reconciler re-applies each connector's config on every
# tick, so changing it would rewrite running topics and break existing tables. "v2" is what a new pipeline gets: exact decimals, a JDBC row keyed
# by its table's primary key, a CDC topic of plain rows WITH a schema (so a database sink can read it), and a database sink that upserts by that
# key (and deletes, for CDC) when its input carries it.
_VALUE_TO_KEY = "org.apache.kafka.connect.transforms.ValueToKey"
_PK_KEY = "pkkey"   # the platform's own key transform on a v2 JDBC source


def record_format_of(pipeline: dict | None) -> str:
    """Missing means v1: only rows created since migration 0056 carry v2, and a dict built without the field must never switch a running
    pipeline's connector over."""
    return "v2" if (pipeline or {}).get("record_format") == "v2" else "v1"


def primary_key_fields(schema: dict | None) -> list[str]:
    return [f["name"] for f in ((schema or {}).get("schema_definition", {}) or {}).get("fields", []) if f.get("primary_key")]


def _changes_key(config: dict, pk_fields: list[str] | None = None) -> bool:
    """True if a transform other than the platform's own key transform rewrites or replaces the record key (a ValueToKey, or any `$Key`
    variant): the key is then whatever the user made it, not the table's primary key, so a sink must not upsert by it. A ValueToKey on
    exactly the primary-key columns (the form's "standard" ValueToKey row_id) builds the same key, so it is no change."""
    for name in (n for n in str(config.get("transforms") or "").split(",") if n and n != _PK_KEY):
        klass = str(config.get(f"transforms.{name}.type") or "")
        if klass == _VALUE_TO_KEY:
            fields = [f.strip() for f in str(config.get(f"transforms.{name}.fields") or "").split(",") if f.strip()]
            if not pk_fields or sorted(fields) != sorted(pk_fields):
                return True
        elif klass.endswith("$Key"):
            return True
    return False


def _dbz_unwrap_v2():
    """The row instead of Debezium's envelope, for a v2 CDC topic. A delete becomes the last row with `__deleted: "true"`, followed by a
    tombstone (a null value under the row's key), which a JDBC sink with delete.enabled turns into a DELETE."""
    return {"transforms": "unwrap", "transforms.unwrap.type": _UNWRAP_CLASS,
            "transforms.unwrap.delete.handling.mode": "rewrite", "transforms.unwrap.drop.tombstones": "false"}


def _unwrap_first(smt: dict) -> dict:
    """Debezium's ExtractNewRecordState turns the {before, after, source, op} envelope into the row. Every other transform reads the row's
    columns, so an explicit one (the form lists it last among the choices) must run FIRST: after a field transform that finds no such column
    in the envelope, every record would fail and be skipped. The relative order of the other transforms is kept."""
    names = [n for n in str(smt.get("transforms") or "").split(",") if n]
    unwrap = [n for n in names if smt.get(f"transforms.{n}.type") == _UNWRAP_CLASS]
    if not unwrap or names[: len(unwrap)] == unwrap:
        return smt
    return {**smt, "transforms": ",".join(unwrap + [n for n in names if n not in unwrap])}


def _needs_auto_unwrap(user_smt: dict) -> bool:
    """True if the user picked field transforms on a CDC source but didn't add an
    explicit unwrap — we prepend one so those transforms hit the real columns."""
    return bool(user_smt) and _UNWRAP_CLASS not in " ".join(str(v) for v in user_smt.values())


async def build_connector(pipeline: dict, connection: dict, template: dict, schema: dict | None,
                          transform_policy: dict | None = None, topic_policy: dict | None = None):
    """Return (config, produced_topic). produced_topic set for sources — which is
    also exactly when topic-creation/schema-strategy config applies (see
    app/services/topic_policy.py): a sink reads an existing topic, it never
    creates one.

    This is the one place a fetched connection's real secret bundle gets
    hydrated back into a transient connection_config before building the
    real, deployable config (see app/services/secret_store.py) — every
    caller of _build_connector below goes through here first, so nothing
    downstream (including resolve_secret) needs its own hydration step.

    Also the one place errors.tolerance/errors.deadletterqueue.* get set
    for the config actually deployed to Kafka Connect (GAP-04) — previously
    this never set them at all (only the create-time PREVIEW in
    pipeline_config.py hard-coded errors.tolerance=all), so every real
    connector silently ran under Kafka Connect's own default of
    errors.tolerance=none regardless of what that preview claimed. Applied
    here, after _build_connector, so it's uniform across every connector
    class (source and sink alike) without touching each of that function's
    many per-class branches."""
    connection = await hydrate_connection_secrets(connection)
    config, produced_topic = _build_connector(pipeline, connection, template, schema, transform_policy)
    tasks = _tasks_max(pipeline.get("connector_config") or {}, template.get("connector_class"))
    if tasks is not None:
        config["tasks.max"] = str(tasks)
    config.update(error_handling_connect_config(
        error_handling_policy=pipeline.get("error_handling_policy") or default_policy(template),
        direction=template.get("connector_type") or pipeline.get("direction") or "",
        domain=pipeline["domain"], source_system=pipeline["source_system"],
        entity=pipeline["entity"], version=pipeline["version"],
    ))
    if produced_topic:
        resolved = await resolve_topic_config(pipeline["org_id"], pipeline["domain"], topic_policy)
        config.update(topic_creation_connect_config(resolved))
        config.update(schema_strategy_converter_config(resolved))
    else:
        # Sink: every sink branch in _build_connector hardcodes _JSON_SCHEMA
        # (schemas.enable=true), on the platform-wide assumption every topic
        # is schema-enveloped — true for the JDBC and most other sources, but
        # NOT for Mongo's (see MONGO_SOURCE's own docstring on why it can't
        # reliably do otherwise) nor for any Debezium CDC source, which all
        # write plain JSON (_JSON_PLAIN). A sink built against such a topic
        # under that hardcoded assumption fails every record at the
        # KEY_CONVERTER/VALUE_CONVERTER stage — DLQ_AND_CONTINUE silently
        # swallows all of them, so the pipeline shows "running" while writing
        # nothing (live-reproduced, not hypothetical). Kafka Connect gives a
        # sink no way to introspect its own input topic's actual format, so
        # this looks it up the only way available: which pipeline in this org
        # produces that topic_name, and what it actually wrote.
        input_topic = (pipeline.get("connector_config") or {}).get("topics")
        if input_topic and config.get("key.converter") == _JSON_SCHEMA["key.converter"]:
            async with AsyncSessionLocal() as session:
                producer = await PipelineRepository(session).find_by_topic_name(pipeline["org_id"], input_topic)
                if producer is None:
                    producer = await _enforcing_producer_of(session, pipeline["org_id"], input_topic)
            produced = (producer.generated_config or {}) if producer else {}
            if produced.get("connector.class") == MONGO_SOURCE:
                # The Mongo source writes MongoDB extended JSON ({"_id": {"$oid": ...}}). A Mongo sink must read
                # it as text and parse it itself; a JsonConverter turns "$oid" into a plain map key that MongoDB
                # rejects ("unknown operator: $oid"). Other sinks read it as plain JSON.
                config.update(_STRING_CONVERTER if config.get("connector.class") == MONGO_SINK else _JSON_PLAIN)
            elif produced.get("value.converter.schemas.enable") == "false":
                # Not only Mongo: every Debezium (CDC) source writes plain JSON too. A sink left on schemas.enable=true failed every
                # record at the converter and, with errors.tolerance=all, skipped all of them while showing RUNNING (2026-10-28:
                # a JDBC sink read 101,500 CDC records and wrote 0 rows). A sink that can take plain JSON is switched to it, as for
                # Mongo; one that builds rows from each record's schema can't, so its deploy is refused with the reason instead.
                if config.get("connector.class") in SCHEMA_REQUIRED_SINKS:
                    raise ValueError(
                        f"This sink can't read topic '{input_topic}': pipeline '{producer.name}' writes it as plain JSON without a "
                        "schema (as every CDC source does), and this connector needs each record's schema to write rows, so every "
                        "record would be skipped. Read a topic whose records carry a schema, such as one from a JDBC source pipeline."
                    )
                config.update(_JSON_PLAIN)
            elif config.get("connector.class") == JDBC_SINK and record_format_of(pipeline) == "v2" and producer is not None:
                config.update(await _upsert_settings(pipeline["org_id"], producer))
    return config, produced_topic


async def _enforcing_producer_of(session, org_id: str, topic: str):
    """The ENFORCE pipeline whose PREPARED topic `topic` is, or None. Its records are that pipeline's own, exactly as Kafka Connect wrote them
    (the quality worker forwards a passing record unchanged, key included — quality_stream_worker._forward_to_prepared), so a sink reading
    it is configured for that producer's format and key like one reading its data topic. Without this a sink on a prepared topic — the
    only topic it may read from an ENFORCE pipeline — found no producer: it lost the v2 upsert and the format checks above."""
    from app.services.quality_mode import ENFORCE, build_prepared_topic

    for row in await PipelineRepository(session).list_for_org(org_id):
        if row.quality_mode == ENFORCE and build_prepared_topic({
            "org_id": row.org_id, "domain": row.domain, "source_system": row.source_system, "entity": row.entity,
            "mode": row.mode, "version": row.version, "direction": row.direction,
        }) == topic:
            return row
    return None


async def _upsert_settings(org_id: str, producer) -> dict:
    """A v2 JDBC sink upserts by its input's key when that key is the source table's primary key: a record sent twice (a source task
    restarted between saving offsets) then overwrites its own row instead of adding one, and an update replaces the row. That holds for a
    v2 producer that keys its records by the primary key (a JDBC source: its `pkkey` transform; a CDC source: Debezium's own key, which
    also carries deletes as tombstones) of ONE table, with no user transform that changes the key. Anything else ({}): keep inserting."""
    if record_format_of({"record_format": producer.record_format}) != "v2":
        return {}
    produced = producer.generated_config or {}
    tables = (producer.connector_config or {}).get("tables") or []
    if produced.get("key.converter.schemas.enable") != "true" or len(tables) > 1 or "*" in tables:
        return {}
    klass = produced.get("connector.class")
    if klass == JDBC_SOURCE:
        pk = [f for f in str(produced.get(f"transforms.{_PK_KEY}.fields") or "").split(",") if f]
        cdc = False
    elif klass in (DBZ_PG, DBZ_MYSQL, DBZ_MSSQL, DBZ_ORACLE) and producer.schema_version_id:
        from app.services.schema_service import schema_service

        pk = primary_key_fields(await schema_service.get_schema(org_id, producer.schema_version_id))
        cdc = True
    else:
        return {}
    if not pk or _changes_key(produced, pk):
        return {}
    return {"insert.mode": "upsert", "pk.mode": "record_key", "pk.fields": ",".join(pk), **({"delete.enabled": "true"} if cdc else {})}


def _build_connector(pipeline: dict, connection: dict, template: dict, schema: dict | None,
                     transform_policy: dict | None = None):
    """Return (config, produced_topic). produced_topic set for sources. Raises ValueError if unwired."""
    if not connection:
        raise ValueError("Connection profile not found")
    if not template:
        raise ValueError("Connector template not found")

    from app.services.transform_policy import generate_smt_config

    klass = template.get("connector_class")
    cfg = connection.get("connection_config", {}) or {}
    st = (connection.get("source_type") or "").lower()
    entity = pipeline["entity"]
    # Unconditional — every record the source connector reads is written
    # straight to the pipeline's own advertised zone topic, whether or not a
    # quality_policy_id is attached. See _sink_topic's docstring: quality
    # checking never redirects or filters data, it only observes it (see
    # app/services/quality_stream_worker.py).
    topic = pipeline.get("topic_name")  # None for a sink, which produces nothing (GAP-12)
    prefix = f'{pipeline["domain"]}.{pipeline["source_system"]}'
    tables = resolve_tables(pipeline)          # 1+ tables (or [entity]); '*' already expanded
    snapshot_mode = _snapshot_mode(pipeline, klass) if klass in SNAPSHOT_MODES_BY_CLASS else "initial"   # Debezium snapshot.mode
    # User-selected field transforms (MaskField, ReplaceField, …) → real SMT config.
    user_transforms = (transform_policy or {}).get("transforms") or []
    user_smt = _unwrap_first(generate_smt_config(user_transforms)) if user_transforms else {}  # raises ValueError → 422

    # ── JDBC (Postgres / MySQL) ──────────────────────────────────────────────
    if klass == JDBC_SOURCE:
        pk_fields = primary_key_fields(schema)
        pk = pk_fields[0] if pk_fields else None
        v2 = record_format_of(pipeline) == "v2"
        # A `key_field` makes the source publish a keyed topic. Sinks that key by the Kafka
        # record (Redis, document stores) reject a null key, and a JDBC source emits none.
        key_field = (pipeline.get("connector_config") or {}).get("key_field")
        # v2: every record is keyed by the table's primary key ({"row_id": 7}), ahead of the user's transforms, so a sink can upsert by it and a
        # row sent twice (a task restarted after its offsets were last saved) overwrites itself instead of becoming a second row. An explicit
        # key_field is the user's choice and wins.
        pk_key = ({"transforms": _PK_KEY, f"transforms.{_PK_KEY}.type": _VALUE_TO_KEY, f"transforms.{_PK_KEY}.fields": ",".join(pk_fields)}
                  if v2 and pk_fields and not key_field else {})
        c = {"connector.class": klass, "tasks.max": "1", "connection.url": _jdbc_url(cfg, st),
             "connection.user": cfg.get("username"),
             "connection.password": resolve_secret(connection, "password", "password"),
             "table.whitelist": ",".join(_unqualified(t) for t in tables),
             "topic.prefix": prefix + ".", "poll.interval.ms": str(JDBC_POLL_MS),
             # v1 best_fit turns a Postgres NUMERIC(12,2) into a double (sums show float error), yet leaves MySQL's DECIMAL an exact decimal (the
             # driver reports DECIMAL, not NUMERIC). v2 precision_only keeps every NUMERIC/DECIMAL with a scale exact on both: the JDBC sink
             # creates a DECIMAL column, and the quality worker decodes it (quality_stream_worker._decode_logical_types).
             "numeric.mapping": "precision_only" if v2 else "best_fit",
             **_JSON_SCHEMA, **_combine(pk_key, user_smt, _reroute(".*", topic))}
        if key_field:
            # Merge, never overwrite: `c` already carries the reroute (and any user) transforms.
            existing = {k: v for k, v in c.items() if k == "transforms" or k.startswith("transforms.")}
            for k in existing:
                c.pop(k)
            c.update(_with_key(key_field, existing, cast_string=True))
            c["key.converter"] = "org.apache.kafka.connect.storage.StringConverter"
            c.pop("key.converter.schemas.enable", None)
        # JDBC's initial-load control (the snapshot equivalent):
        #   bulk         → re-read the whole table every poll (full snapshot each time)
        #   incrementing → only rows with a new PK value (needs a single table + PK)
        #   auto         → incrementing when a single-table PK is known, else bulk
        load_mode = (pipeline.get("connector_config") or {}).get("load_mode") or "auto"
        if load_mode == "incrementing" and pk:
            c.update({"mode": "incrementing", "incrementing.column.name": pk})
        elif load_mode == "bulk":
            c["mode"] = "bulk"
        else:  # auto
            c.update({"mode": "incrementing", "incrementing.column.name": pk} if (pk and len(tables) == 1) else {"mode": "bulk"})
        c["poll.interval.ms"] = str(_jdbc_poll_interval_ms(pipeline.get("connector_config") or {}, c["mode"]))
        return c, topic
    if klass == JDBC_SINK:
        return {"connector.class": klass, "tasks.max": "1", "connection.url": _jdbc_url(cfg, st),
                "connection.user": cfg.get("username"),
                "connection.password": resolve_secret(connection, "password", "password"),
                "topics": _sink_topic(pipeline), "table.name.format": entity,
                "auto.create": "true", "auto.evolve": "true", "insert.mode": "insert", "pk.mode": "none",
                **_JSON_SCHEMA, **user_smt}, None

    # ── MongoDB (standard connector) ─────────────────────────────────────────
    if klass == MONGO_SOURCE:
        # single collection -> that collection; multiple/'*' -> "" = every collection in the db
        collection = _unqualified(tables[0]) if len(tables) == 1 else ""
        # startup.mode is Mongo's snapshot equivalent: copy_existing = snapshot then stream; latest = stream only
        startup = (pipeline.get("connector_config") or {}).get("startup_mode") or "copy_existing"
        # The Kafka message must equal the Mongo document. So: raw JSON text
        # end to end (StringConverter, no schema envelope — output.format=schema
        # was tried and reverted: it schematizes the change-stream wrapper, not
        # the document, and every field came through null), rendered with
        # DefaultJson — MongoDB's own JSON form (as in mongoexport), which keeps
        # BSON types: {"_id": {"$oid": ...}}, {"installed_at": {"$date": <ms>}}.
        # (RelaxedExtendedJson doesn't exist in mongo-kafka 3.0.1.) SimplifiedJson
        # turned ObjectId/ISODate into bare strings, so the topic no longer
        # matched the collection's types.
        return {"connector.class": klass, "tasks.max": "1", "connection.uri": _mongo_uri(cfg, connection),
                "database": cfg.get("database"), "collection": collection, "startup.mode": startup,
                "publish.full.document.only": "true", "output.format.value": "json", "output.format.key": "json",
                "output.json.formatter": "com.mongodb.kafka.connect.source.json.formatter.DefaultJson",
                "topic.prefix": prefix, **_STRING_CONVERTER, **_combine(user_smt, _reroute(".*", topic))}, topic
    if klass == MONGO_SINK:
        return {"connector.class": klass, "tasks.max": "1", "connection.uri": _mongo_uri(cfg, connection),
                "database": cfg.get("database"), "collection": entity, "topics": _sink_topic(pipeline),
                **_JSON_PLAIN, **user_smt}, None

    # ── Elasticsearch sink ───────────────────────────────────────────────────
    if klass == ES_SINK:
        scheme = "https" if str(cfg.get("scheme", "http")).lower() == "https" else "http"
        url = cfg.get("url") or f"{scheme}://{_host(cfg)}:{_port(cfg, 'elasticsearch')}"
        # dropMongoId: Elasticsearch reserves "_id" as a metadata field and
        # rejects any document that also carries it as a regular field
        # ("Field [_id] is a metadata field and cannot be added inside a
        # document") — every MongoDB document has a literal _id field, so any
        # sink reading from a Mongo-sourced topic hits this unconditionally,
        # even with key.ignore=true (that only controls the ES document ID,
        # not what's in the body). A no-op ReplaceField$Value exclude for a
        # non-Mongo topic that has no _id field at all, so this is safe to
        # apply unconditionally rather than needing to detect the source.
        drop_mongo_id = {
            "transforms": "dropMongoId",
            "transforms.dropMongoId.type": "org.apache.kafka.connect.transforms.ReplaceField$Value",
            "transforms.dropMongoId.exclude": "_id",
        }
        c = {"connector.class": klass, "tasks.max": "1", "connection.url": url,
             "topics": _sink_topic(pipeline), "key.ignore": "true", "schema.ignore": "true",
             "type.name": "_doc", **_JSON_SCHEMA, **_combine(drop_mongo_id, user_smt)}
        if cfg.get("username"):
            c.update({"connection.username": cfg.get("username"),
                      "connection.password": resolve_secret(connection, "password", "password")})
        return c, None

    # ── S3 (needs bucket/region/keys in the connection config) ───────────────
    if klass in (S3_SINK, S3_SOURCE):
        bucket = cfg.get("bucket")
        if not bucket:
            raise ValueError("S3 connection needs a 'bucket' in its config.")
        creds = {}
        if cfg.get("access_key_id"):
            creds = {
                "aws.access.key.id": resolve_secret(connection, "access_key_id", "access_key_id"),
                "aws.secret.access.key": resolve_secret(connection, "secret_access_key", "secret_access_key"),
            }
        base = {"tasks.max": "1", "s3.bucket.name": bucket, "s3.region": cfg.get("region", "us-east-1"),
                "storage.class": "io.confluent.connect.s3.storage.S3Storage", **creds}
        if cfg.get("endpoint"):
            # S3-compatible store (MinIO etc.): point the SDK at it and use path-style addressing.
            base.update({"store.url": cfg["endpoint"], "s3.path.style.access.enabled": "true"})
        if klass == S3_SINK:
            base.update({"connector.class": S3_SINK, "topics": _sink_topic(pipeline),
                         "format.class": "io.confluent.connect.s3.format.json.JsonFormat",
                         "flush.size": "1", "topics.dir": prefix.replace(".", "/"),
                         "partitioner.class": "io.confluent.connect.storage.partitioner.DefaultPartitioner",
                         **_JSON_PLAIN, **user_smt})
            return base, None
        # S3 SOURCE (Confluent) differs from the sink: it needs a license/metadata topic
        # and, in GENERIC mode, a topic.regex.list mapping object keys → the output topic.
        # topic.regex.list routes directly to `topic`, so no RegexRouter reroute is needed.
        obj_regex = f"{cfg['prefix']}.*" if cfg.get("prefix") else ".*"
        if cfg.get("prefix"):
            # The source only scans under `topics.dir` (default "topics"); the sink wrote under its own prefix.
            base["topics.dir"] = cfg["prefix"].strip("/")
        base.update({"connector.class": S3_SOURCE, "format.class": "io.confluent.connect.s3.format.json.JsonFormat",
                     "mode": "GENERIC", "topic.regex.list": f"{topic}:{obj_regex}",
                     "confluent.topic.bootstrap.servers": settings.kafka_bootstrap,
                     "confluent.topic.replication.factor": "1",
                     **_JSON_PLAIN, **user_smt})
        return base, topic

    # ── GCS sink (kafka-connect-storage-common family, same shape as S3) ─────
    if klass == GCS_SINK:
        bucket = cfg.get("bucket")
        if not bucket:
            raise ValueError("GCS connection needs a 'bucket' in its config.")
        creds = {}
        if cfg.get("credentials_json"):
            creds = {"gcs.credentials.json": resolve_secret(connection, "credentials_json", "credentials_json")}
        return {"connector.class": GCS_SINK, "tasks.max": "1", "topics": _sink_topic(pipeline),
                "gcs.bucket.name": bucket, "storage.class": "io.confluent.connect.gcs.storage.GcsStorage",
                "format.class": "io.confluent.connect.gcs.format.json.JsonFormat", "flush.size": "1",
                "topics.dir": prefix.replace(".", "/"),
                "partitioner.class": "io.confluent.connect.storage.partitioner.DefaultPartitioner",
                **creds, **_JSON_PLAIN, **user_smt}, None

    # ── Azure Data Lake Storage Gen2 sink (same storage-common shape) ────────
    if klass == ADLS_GEN2_SINK:
        account = cfg.get("account_name")
        filesystem = cfg.get("filesystem")
        if not account or not filesystem:
            raise ValueError("ADLS Gen2 connection needs 'account_name' and 'filesystem' in its config.")
        auth = {"azure.datalake.gen2.client.id": cfg.get("client_id"),
                "azure.datalake.gen2.client.key": resolve_secret(connection, "client_secret", "client_secret")}
        if cfg.get("tenant_id"):
            auth["azure.datalake.gen2.token.endpoint"] = (
                f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/token"
            )
        return {"connector.class": ADLS_GEN2_SINK, "tasks.max": "1", "topics": _sink_topic(pipeline),
                "azure.datalake.gen2.account.name": account, "azure.datalake.gen2.filesystem": filesystem,
                "storage.class": "io.confluent.connect.azure.datalake.gen2.storage.DataLakeGen2Storage",
                "format.class": "io.confluent.connect.azure.datalake.gen2.format.json.JsonFormat", "flush.size": "1",
                "topics.dir": prefix.replace(".", "/"),
                "partitioner.class": "io.confluent.connect.storage.partitioner.DefaultPartitioner",
                **auth, **_JSON_PLAIN, **user_smt}, None

    # ── Apache Iceberg sink (getindata/kafka-connect-iceberg-sink) ───────────
    # Its config is catalog-shaped, not storage-common: no format.class/flush.size/
    # partitioner — table.write-format + iceberg.* stand in for those. Two things
    # confirmed only by a live deploy against a real (non-Debezium) topic:
    #   1. It derives the Iceberg table schema from each record's own embedded
    #      Connect schema (IcebergChangeEvent.JsonSchema) — schemas.enable=false
    #      throws "event schema has no fields!" on the first record, so this
    #      needs _JSON_SCHEMA, not _JSON_PLAIN.
    #   2. `upsert` defaults to true, and upsert/delta mode reads Debezium CDC
    #      envelope fields (upsert.dedup-column defaults to __source_ts_ms,
    #      upsert.op-column to __op) to decide insert/update/delete — on a plain
    #      (non-CDC) record missing those fields it NullPointerExceptions in
    #      BaseDeltaTaskWriter.write. `upsert: false` (plain append) is the sane
    #      default for a general-purpose sink; a pipeline reading a genuine
    #      Debezium topic can override it back to true via connector_config.
    if klass == ICEBERG_SINK:
        warehouse = cfg.get("warehouse")
        if not warehouse:
            raise ValueError("Iceberg connection needs a 'warehouse' location in its config.")
        upsert = (pipeline.get("connector_config") or {}).get("upsert", False)
        c = {"connector.class": klass, "tasks.max": "1", "topics": _sink_topic(pipeline),
             "iceberg.name": cfg.get("catalog_name") or "default", "iceberg.warehouse": warehouse,
             "iceberg.type": cfg.get("catalog_type") or "hadoop",
             "table.namespace": cfg.get("namespace") or "default",
             "table.auto-create": "true", "table.write-format": "parquet",
             "upsert": "true" if upsert else "false",
             **_JSON_SCHEMA, **user_smt}
        if cfg.get("catalog_uri"):
            c["iceberg.uri"] = cfg["catalog_uri"]
        return c, None

    # ── Debezium CDC sources ─────────────────────────────────────────────────
    if klass in (DBZ_PG, DBZ_MYSQL, DBZ_MSSQL, DBZ_ORACLE):
        db = cfg.get("database")
        # Every Debezium identifier below is derived from connector_name,
        # which is already globally unique and immutable once the pipeline
        # is created — see app/services/debezium_identifiers.py for why this
        # (not the coarser org.domain.source prefix, and not the entity name
        # alone) is what guarantees no two pipelines can collide.
        dbz_prefix = debezium_topic_prefix(pipeline["connector_name"])
        # Reroute every table topic (prefix.schema.table) to the pipeline's canonical
        # topic, but NOT the bare `<prefix>` DDL/schema-change topic (only two segments
        # wouldn't match the three-segment pattern).
        v2 = record_format_of(pipeline) == "v2"
        if v2:
            # The row with its schema, always (not only when the user picked a field transform): that is what a JDBC sink needs to write
            # rows, and what makes the record's key (Debezium's: the primary key) usable for upserts and deletes. An explicit
            # ExtractNewRecordState in the user's chain is theirs and replaces this one.
            unwrap = _dbz_unwrap_v2() if _UNWRAP_CLASS not in " ".join(str(v) for v in user_smt.values()) else {}
        else:
            unwrap = _dbz_unwrap() if _needs_auto_unwrap(user_smt) else {}
        c = {"connector.class": klass, "tasks.max": "1",
             "database.hostname": _host(cfg), "database.port": str(_port(cfg, st)),
             "database.user": cfg.get("username"),
             "database.password": resolve_secret(connection, "password", "password"),
             "topic.prefix": dbz_prefix, "snapshot.mode": snapshot_mode,
             # v1: NUMERIC/DECIMAL as plain doubles (a schemaless topic could not carry an exact decimal readably). v2: exact, with the schema
             # saying so ("precise").
             "decimal.handling.mode": "precise" if v2 else "double", **(_JSON_SCHEMA if v2 else _JSON_PLAIN),
             **_combine(unwrap, user_smt, _reroute(dbz_prefix + _DBZ_TABLE_TOPIC_TAIL.get(klass, _DBZ_DEFAULT_TAIL), topic))}
        if v2:
            # Dates and timestamps as Kafka Connect's own types, so a JDBC sink creates DATE / TIMESTAMP columns (adaptive mode writes plain
            # integers: days, micro- or milliseconds).
            c["time.precision.mode"] = "connect"
        elif _TIMESTAMP_CONVERTER in " ".join(str(v) for v in user_smt.values()):
            # Debezium's default (adaptive) precision writes a TIMESTAMP as epoch MICROseconds, which TimestampConverter reads as
            # milliseconds — 2026-10-11 became year 58746. "connect" emits Kafka Connect's own millisecond Timestamp/Date/Time types,
            # which TimestampConverter understands. Only when the user picked TimestampConverter: it drops sub-millisecond precision,
            # and changing it for every CDC pipeline would change the values every existing one writes.
            c["time.precision.mode"] = "connect"
        history = f"schemahistory.{pipeline['connector_name']}"
        if klass == DBZ_PG:
            c.update({"database.dbname": db, "plugin.name": "pgoutput",
                      "slot.name": debezium_slot_name(pipeline["connector_name"]),
                      "publication.autocreate.mode": "filtered",
                      "publication.name": debezium_publication_name(pipeline["connector_name"]),
                      "table.include.list": ",".join(_qualify(t, "public") for t in tables)})
        elif klass == DBZ_MYSQL:
            c.update({"database.include.list": db,
                      "table.include.list": ",".join(_qualify(t, db) for t in tables),
                      "database.server.id": str(debezium_server_id(pipeline["connector_name"])),
                      "schema.history.internal.kafka.bootstrap.servers": "kafka:29092",
                      "schema.history.internal.kafka.topic": history})
        elif klass == DBZ_MSSQL:
            c.update({"database.names": db,
                      "table.include.list": ",".join(_qualify(t, "dbo") for t in tables),
                      "schema.history.internal.kafka.bootstrap.servers": "kafka:29092",
                      "schema.history.internal.kafka.topic": history, "database.encrypt": "false"})
        elif klass == DBZ_ORACLE:
            owner = (cfg.get("username") or "").upper()
            c.update({"database.dbname": db,
                      "table.include.list": ",".join(_qualify(_unqualified(t).upper(), owner) for t in tables),
                      "schema.history.internal.kafka.bootstrap.servers": "kafka:29092",
                      "schema.history.internal.kafka.topic": history})
        return c, topic

    if klass == DBZ_MONGO:
        db = cfg.get("database")
        dbz_prefix = debezium_topic_prefix(pipeline["connector_name"])
        unwrap = _dbz_unwrap() if _needs_auto_unwrap(user_smt) else {}
        return {"connector.class": DBZ_MONGO, "tasks.max": "1",
                "mongodb.connection.string": _mongo_uri(cfg, connection), "topic.prefix": dbz_prefix,
                "snapshot.mode": snapshot_mode, "database.include.list": db,
                "collection.include.list": ",".join(f"{db}.{_unqualified(t)}" for t in tables),
                **_JSON_PLAIN, **_combine(unwrap, user_smt, _reroute(rf"{dbz_prefix}\.[^.]+\.[^.]+", topic))}, topic

    # ── Local file (reads/writes a file on the Kafka Connect worker) ─────────
    if klass == FILE_SOURCE:
        path = cfg.get("path") or cfg.get("file")
        if not path:
            raise ValueError("File source connection needs a 'path' to a file on the Kafka Connect worker.")
        # each line becomes a String record; topic is set directly (no reroute)
        return {"connector.class": FILE_SOURCE, "tasks.max": "1", "file": path, "topic": topic,
                "key.converter": "org.apache.kafka.connect.storage.StringConverter",
                "value.converter": "org.apache.kafka.connect.storage.StringConverter",
                **_combine(user_smt)}, topic
    if klass == FILE_SINK:
        path = cfg.get("path") or cfg.get("file")
        if not path:
            raise ValueError("File sink connection needs a 'path' to write to on the Kafka Connect worker.")
        return {"connector.class": FILE_SINK, "tasks.max": "1", "file": path, "topics": _sink_topic(pipeline),
                "key.converter": "org.apache.kafka.connect.storage.StringConverter",
                "value.converter": "org.apache.kafka.connect.storage.StringConverter",
                **user_smt}, None

    if klass == FILECHUNK_SOURCE:
        path = cfg.get("path") or cfg.get("file")
        if not path:
            raise ValueError("File chunk source connection needs a 'path' to a file on the Kafka Connect worker.")
        import os
        return {"connector.class": FILECHUNK_SOURCE, "tasks.max": "1",
                "files.dir": os.path.dirname(path) or ".",
                "input.file.pattern": os.path.basename(path),
                "topic": topic,
                # Each chunk becomes one Kafka message, and the plugin's default (2,048,000 bytes)
                # is above Kafka's default 1 MB message and producer request limit, so a file
                # larger than that is chunked but never produced (nothing is logged). Stay under it.
                "binary.chunk.size.bytes": str((pipeline.get("connector_config") or {}).get("chunk_size_bytes") or 900000),
                # The connector cuts a file into .CHUNK files and then has to find them again to publish them, but it
                # only searches for chunk files when this is true (the default is false). Without it the chunks are
                # written, the input is renamed __FINISHED and nothing ever reaches Kafka.
                "input.path.walk.recursively": "true",
                # Single-task mode (no licence key) only supports a one-partition topic: with more, the sink logs
                # "unexpected multi-partition use while in Single-task mode" and drops chunks, so a file never
                # reassembles. Give the pipeline a topic policy with partitions=1. A licensed multi-task setup can
                # raise this with the pipeline option `topic_partitions` (it must equal the topic's partition count).
                "topic.partitions": str((pipeline.get("connector_config") or {}).get("topic_partitions") or 1),
                # Both key and value are raw bytes: the key is the partition key the connector computes so the sink
                # can restore chunk order. With StringConverter the key became the text "[B@1a2b3c" and chunks landed
                # on random partitions.
                **_BYTES_PASSTHROUGH,
                **_combine(user_smt)}, topic

    if klass == FILECHUNK_SINK:
        path = cfg.get("path") or cfg.get("file")
        if not path:
            raise ValueError("File chunk sink connection needs a 'path' to write to on the Kafka Connect worker.")
        import os
        return {"connector.class": FILECHUNK_SINK, "tasks.max": "1",
                "files.dir": os.path.dirname(path) or ".",
                "topics": _sink_topic(pipeline),
                **_BYTES_PASSTHROUGH,
                **user_smt}, None

    t1 = build_t1_connector(
        klass, pipeline=pipeline, cfg=cfg, entity=entity, topic=topic, prefix=prefix,
        tables=tables, sink_topic=(_sink_topic(pipeline) if (pipeline.get("direction") or template.get("connector_type") or "").lower() == "sink" else None),
        user_smt=user_smt, reroute=_reroute, json_schema=_JSON_SCHEMA, json_plain=_JSON_PLAIN,
        bytes_passthrough=_BYTES_PASSTHROUGH, host=_host, port_of=lambda c, d: int(c.get("port") or d),
    )
    if t1 is not None:
        return t1

    raise ValueError(f"Connector class '{klass}' has no config builder yet.")

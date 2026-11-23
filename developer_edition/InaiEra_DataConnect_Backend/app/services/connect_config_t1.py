"""
Config builders for the T1 connectors in `connector_test_plan.xlsx` — the tier
whose backing system runs locally in Docker, so it can be exercised end-to-end
without a cloud account.

`connect_config.build_connector` handles the connectors the platform shipped
with; it delegates here for the T1 additions and raises only if neither knows
the class. Splitting them keeps one 40-branch function from becoming two.

Every config key below was read from the worker itself — for each plugin,
`PUT /connector-plugins/<class>/config/validate` returns the connector's real
ConfigDef, and these builders set names taken from that response rather than
from documentation, which for several of these connectors is wrong or stale
(the ActiveMQ sink registers as `io.confluent.connect.jms.ActiveMqSinkConnector`,
not the `...connect.activemq...` class the Hub page implies).

Connectors run INSIDE the Connect worker, so a connection profile's host must be
a name the worker can resolve — the compose service name (`mysql`, `minio`,
`cassandra`), never `localhost`.
"""

from app.core.config import settings

# ── connector classes (verified against /connector-plugins) ─────────────────
DATAGEN = "io.confluent.kafka.connect.datagen.DatagenConnector"
SPOOLDIR_CSV = "com.github.jcustenborder.kafka.connect.spooldir.SpoolDirCsvSourceConnector"
SYSLOG_SOURCE = "io.confluent.connect.syslog.SyslogSourceConnector"
SNMP_SOURCE = "io.confluent.connect.snmp.SnmpTrapSourceConnector"
DIODE_SOURCE = "io.confluent.connect.diode.source.DataDiodeSourceConnector"
DIODE_SINK = "io.confluent.connect.diode.sink.DataDiodeSinkConnector"
FTPS_SOURCE = "io.confluent.connect.ftps.FtpsSourceConnector"
FTPS_SINK = "io.confluent.connect.ftps.FtpsSinkConnector"
INFLUX_SINK = "io.confluent.influxdb.InfluxDBSinkConnector"
INFLUX_SOURCE = "io.confluent.influxdb.source.InfluxdbSourceConnector"
ACTIVEMQ_SINK = "io.confluent.connect.jms.ActiveMqSinkConnector"
JMS_SINK = "io.confluent.connect.jms.JmsSinkConnector"
JMS_SOURCE = "io.confluent.connect.jms.JmsSourceConnector"
CASSANDRA_SINK = "io.confluent.connect.cassandra.CassandraSinkConnector"
DATASTAX_SINK = "com.datastax.oss.kafka.sink.CassandraSinkConnector"
HBASE_SINK = "io.confluent.connect.hbase.HBaseSinkConnector"
HDFS2_SINK = "io.confluent.connect.hdfs.HdfsSinkConnector"
HDFS3_SINK = "io.confluent.connect.hdfs3.Hdfs3SinkConnector"
HDFS2_SOURCE = "io.confluent.connect.hdfs2.Hdfs2SourceConnector"
HDFS3_SOURCE = "io.confluent.connect.hdfs3.Hdfs3SourceConnector"
PROMETHEUS_SINK = "io.confluent.connect.prometheus.PrometheusMetricsSinkConnector"
CLICKHOUSE_SINK = "com.clickhouse.kafka.connect.ClickHouseSinkConnector"
QUESTDB_SINK = "io.questdb.kafka.QuestDBSinkConnector"
REDIS_SINK = "com.github.jcustenborder.kafka.connect.redis.RedisSinkConnector"
QDRANT_SINK = "io.qdrant.kafka.QdrantSinkConnector"
WEAVIATE_SINK = "io.weaviate.connector.WeaviateSinkConnector"
MILVUS_SINK = "com.milvus.io.kafka.MilvusSinkConnector"
NEO4J_SINK = "org.neo4j.connectors.kafka.sink.Neo4jConnector"
NEO4J_SOURCE = "org.neo4j.connectors.kafka.source.Neo4jConnector"
COUCHBASE_SOURCE = "com.couchbase.connect.kafka.CouchbaseSourceConnector"
COUCHBASE_SINK = "com.couchbase.connect.kafka.CouchbaseSinkConnector"
AEROSPIKE_SINK = "com.aerospike.connect.kafka.inbound.AerospikeSinkConnector"
SINGLESTORE_SINK = "com.singlestore.kafka.SingleStoreSinkConnector"
TIMEPLUS_SINK = "com.timeplus.kafkaconnect.TimeplusSinkConnector"
LITTLEHORSE_SINK = "io.littlehorse.connect.WfRunSinkConnector"
CHUNK_SINK = "com.github.streamsend.file.chunk.sink.ChunkSinkConnector"
CHUNK_SOURCE = "com.github.streamsend.file.chunk.source.ChunkSourceConnector"
SCYLLA_CDC = "com.scylladb.cdc.debezium.connector.ScyllaConnector"

# ── T2 connectors with a backend that runs locally ──────────────────────────
EVENTBRIDGE_SINK = "software.amazon.event.kafkaconnector.EventBridgeSinkConnector"
LIGHTSTREAMER_SINK = "com.lightstreamer.kafka.connect.LightstreamerSinkConnector"
YUGABYTE_SOURCE = "io.debezium.connector.postgresql.YugabyteDBConnector"
OMNISCI_SINK = "io.confluent.connect.omnisci.OmnisciSinkConnector"
DIFFUSION_SINK = "com.diffusiondata.connect.diffusion.sink.DiffusionSinkConnector"
DIFFUSION_SOURCE = "com.diffusiondata.connect.diffusion.source.DiffusionSourceConnector"
VERTICA_SINK = "io.confluent.vertica.VerticaSinkConnector"
SPLUNK_SINK = "com.splunk.kafka.connect.SplunkSinkConnector"
SPLUNK_HTTP_SOURCE = "io.confluent.connect.SplunkHttpSourceConnector"
SPLUNK_S2S_SOURCE = "io.confluent.connect.splunk.s2s.SplunkS2SSourceConnector"
GEMFIRE_SINK = "io.confluent.connect.pivotal.gemfire.PivotalGemfireSinkConnector"
FIREBASE_SINK = "io.confluent.connect.firebase.FirebaseSinkConnector"
FIREBASE_SOURCE = "io.confluent.connect.firebase.FirebaseSourceConnector"
COSMOS_SINK = "com.azure.cosmos.kafka.connect.sink.CosmosDBSinkConnector"
COSMOS_SOURCE = "com.azure.cosmos.kafka.connect.source.CosmosDBSourceConnector"
GRIDGAIN_SINK = "org.gridgain.kafka.sink.GridGainSinkConnector"
GRIDGAIN_SOURCE = "org.gridgain.kafka.source.GridGainSourceConnector"

# ── REST / HTTP polling ──────────────────────────────────────────────────────
HTTP_SOURCE = "com.github.castorm.kafka.connect.http.HttpSourceConnector"

T1_CLASSES = {v for k, v in list(globals().items()) if k.isupper() and isinstance(v, str) and "." in v}

_PORTS = {
    CLICKHOUSE_SINK: 8123, REDIS_SINK: 6379, QDRANT_SINK: 6334, WEAVIATE_SINK: 8080,
    MILVUS_SINK: 19530, NEO4J_SINK: 7687, NEO4J_SOURCE: 7687, CASSANDRA_SINK: 9042,
    DATASTAX_SINK: 9042, SCYLLA_CDC: 9042, INFLUX_SINK: 8086, INFLUX_SOURCE: 8086,
    ACTIVEMQ_SINK: 61616, JMS_SINK: 61616, JMS_SOURCE: 61616, COUCHBASE_SOURCE: 11210,
    COUCHBASE_SINK: 11210, AEROSPIKE_SINK: 3000, SINGLESTORE_SINK: 3306,
    TIMEPLUS_SINK: 8463, SYSLOG_SOURCE: 5140, SNMP_SOURCE: 10161, FTPS_SOURCE: 21,
    FTPS_SINK: 21, HBASE_SINK: 2181, QUESTDB_SINK: 9009, HDFS2_SINK: 9000,
    HDFS3_SINK: 9000, HDFS2_SOURCE: 9000, HDFS3_SOURCE: 9000, DIODE_SINK: 3456,
    DIODE_SOURCE: 3456, LITTLEHORSE_SINK: 2023, GRIDGAIN_SINK: 10800, GRIDGAIN_SOURCE: 10800, COSMOS_SINK: 8081, COSMOS_SOURCE: 8081, FIREBASE_SINK: 9000, FIREBASE_SOURCE: 9000, GEMFIRE_SINK: 10334, SPLUNK_SINK: 8088, VERTICA_SINK: 5433, DIFFUSION_SINK: 8080, DIFFUSION_SOURCE: 8080, OMNISCI_SINK: 6274, SPLUNK_HTTP_SOURCE: 8889, SPLUNK_S2S_SOURCE: 9997,
}


def _lic():
    """Confluent commercial connectors store their licence in a Kafka topic and
    refuse to start without these two keys."""
    return {"confluent.topic.bootstrap.servers": settings.kafka_bootstrap,
            "confluent.topic.replication.factor": "1"}


def _real_secret(value):
    """The platform shows every unset secret field of a connection as the mask `********` once it is
    hydrated, so a plain truthiness test mistakes "no API key" for a key and sends `********` as one."""
    return value if value and value != "********" else None


def _with_key(field, user_smt, cast_string=False):
    """Prepend a ValueToKey+ExtractField pair so the record carries a scalar key.

    Key/value stores (Redis) and anything keyed by document id reject records with
    a null key, and a JDBC/Datagen source emits none. With `cast_string` the key is
    also cast to text — Redis additionally demands a String or Bytes key, and a
    numeric primary key (employee_id) is neither. Returns an SMT fragment with the
    user's own transforms appended, keeping the `transforms` name list ordered.
    """
    names = ["dckey", "dckeyx"]
    out = {
        "transforms.dckey.type": "org.apache.kafka.connect.transforms.ValueToKey",
        "transforms.dckey.fields": field,
        "transforms.dckeyx.type": "org.apache.kafka.connect.transforms.ExtractField$Key",
        "transforms.dckeyx.field": field,
    }
    if cast_string:
        names.append("dckeyc")
        out["transforms.dckeyc.type"] = "org.apache.kafka.connect.transforms.Cast$Key"
        out["transforms.dckeyc.spec"] = "string"
    for k, v in (user_smt or {}).items():
        if k == "transforms":
            names.extend(x for x in str(v).split(",") if x)
        else:
            out[k] = v
    out["transforms"] = ",".join(names)
    return out


# What an HTTP status means to the REST source. castorm's default policy turns every status outside 200..299 and 300..399 into a FAILED task, so one
# 429 (rate limited) or 503 (server briefly down) stops the pipeline until someone restarts it. These ranges are SKIPPED instead — nothing is emitted
# and the request is made again on the next poll — which is a retry.
HTTP_RETRY_STATUS_CODES = "429, 500..599"
# castorm reads the connector's position from Kafka Connect's FLUSHED offsets (written about once a minute). Polling faster than that re-requests the
# same page and publishes every record again, so the default stays above the flush interval.
HTTP_DEFAULT_POLL_SECONDS = 65
_HTTP_FIXED_TIMER = "com.github.castorm.kafka.connect.timer.FixedIntervalTimer"


def _http_source_config(klass, *, cfg, cc, topic, json_plain, user_smt):
    """Config for the REST API source. Connection: `base_url`. Pipeline options (all optional unless noted):
    path (required: the endpoint, or an absolute URL), params, method, headers, body, list_pointer, offset_pointer, initial_offset,
    poll_interval_seconds, retry_status_codes. Any `http.*` option is passed straight through and wins over the friendly one."""
    base = str(cfg.get("base_url") or "").rstrip("/")
    path = str(cc.get("path") or cc.get("url") or "").strip()
    if not path:
        raise ValueError("REST API source needs the pipeline option 'path' (the endpoint to poll, such as /events) or an absolute 'url'.")
    url = path if path.lower().startswith(("http://", "https://")) else (f"{base}/{path.lstrip('/')}" if base else "")
    if not url:
        raise ValueError("REST API source needs a connection 'base_url' (or an absolute 'url' in the pipeline options).")
    interval = cc.get("poll_interval_seconds")
    try:
        interval_ms = int(float(interval) * 1000) if interval is not None else HTTP_DEFAULT_POLL_SECONDS * 1000
    except (TypeError, ValueError) as exc:
        raise ValueError("REST API source option 'poll_interval_seconds' must be a number.") from exc
    if interval_ms <= 0:
        raise ValueError("REST API source option 'poll_interval_seconds' must be greater than zero.")
    offset_pointer = str(cc.get("offset_pointer") or "")
    initial = str(cc.get("initial_offset") or "")
    if "timestamp=" in offset_pointer and "timestamp=" not in initial:
        # With a timestamp offset the first request's offset needs one too, or the filter that drops already-seen records has nothing to compare.
        initial = ", ".join(x for x in (initial, "timestamp=1970-01-01T00:00:00Z") if x)
    c = {
        "connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
        "http.request.url": url, "http.request.method": str(cc.get("method") or "GET").upper(),
        "http.timer": _HTTP_FIXED_TIMER, "http.timer.interval.millis": str(interval_ms),
        "http.response.policy.codes.skip": f"300..399, {cc.get('retry_status_codes') or HTTP_RETRY_STATUS_CODES}",
        **json_plain, **user_smt,
    }
    for friendly, key in (("params", "http.request.params"), ("headers", "http.request.headers"), ("body", "http.request.body"),
                          ("list_pointer", "http.response.list.pointer"), ("offset_pointer", "http.response.record.offset.pointer")):
        if cc.get(friendly):
            c[key] = str(cc[friendly])
    if initial:
        c["http.offset.initial"] = initial
    c.update({k: str(v) for k, v in cc.items() if str(k).startswith("http.")})      # raw castorm options override the friendly ones
    return c


def build_t1_connector(klass, *, pipeline, cfg, entity, topic, prefix, tables,
                       sink_topic, user_smt, reroute, json_schema, json_plain,
                       bytes_passthrough, host, port_of):
    """Return (config, produced_topic) for a T1 connector, or None if `klass`
    is not one of them. produced_topic is set for sources, None for sinks.

    Callers pass the shared helpers from connect_config so both modules build
    hosts, ports, SMT fragments and converters exactly the same way.
    """
    if klass not in T1_CLASSES:
        return None

    cc = pipeline.get("connector_config") or {}
    port = port_of(cfg, _PORTS.get(klass, 0))
    h = host(cfg)
    user, pwd = cfg.get("username"), cfg.get("password")

    # ── no backing system: the fastest end-to-end proof a pipeline works ─────
    if klass == DATAGEN:
        c = {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
             "quickstart": cc.get("quickstart") or "users",
             "max.interval": str(cc.get("max_interval") or 1000),
             **json_schema, **user_smt}
        # Only bound the run if asked: on hitting `iterations` the connector stops
        # itself and Connect surfaces that as a FAILED task, which reads exactly
        # like a broken connector.
        if cc.get("iterations"):
            c["iterations"] = str(cc["iterations"])
        return c, topic

    # ── files on the worker ─────────────────────────────────────────────────
    if klass == SPOOLDIR_CSV:
        d = cfg.get("path") or cfg.get("directory")
        if not d:
            raise ValueError("SpoolDir connection needs a 'path' — a directory on the Kafka Connect worker.")
        return {"connector.class": klass, "tasks.max": "1", "topic": topic,
                "input.path": d, "finished.path": f"{d}/finished", "error.path": f"{d}/error",
                "input.file.pattern": cc.get("file_pattern") or r".*\.csv",
                "cleanup.policy": "MOVE", "halt.on.error": "false",
                "csv.first.row.as.header": "true", "schema.generation.enabled": "true",
                "schema.generation.key.fields": cc.get("key_field") or "",
                **json_schema, **user_smt}, topic

    if klass == CHUNK_SOURCE:
        d = cfg.get("path") or cfg.get("directory")
        if not d:
            raise ValueError("File Chunk source connection needs a 'path' — a directory on the Kafka Connect worker.")
        return {"connector.class": klass, "tasks.max": "1", "topic": topic, "files.dir": d,
                "input.file.pattern": cc.get("file_pattern") or ".*",
                **bytes_passthrough, **user_smt}, topic
    if klass == CHUNK_SINK:
        d = cfg.get("path") or cfg.get("directory")
        if not d:
            raise ValueError("File Chunk sink connection needs a 'path' to write to on the Kafka Connect worker.")
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "files.dir": d, **bytes_passthrough, **user_smt}, None

    if klass in (FTPS_SOURCE, FTPS_SINK):
        creds = {"ftps.host": h, "ftps.port": str(port), "ftps.username": user, "ftps.password": pwd,
                 # Uppercase: the connector rejects "explicit" with
                 # "Invalid value explicit for configuration ftps.security.mode".
                 "ftps.security.mode": (cc.get("security_mode") or "EXPLICIT").upper(),
                 "ftps.ssl.enabled": str(cc.get("ssl_enabled", False)).lower(),
                 # Escape hatch for anything the plugin documents under ftps.* (TLS trust and
                 # keystore settings, CSV/JSON parser options, input/finished paths, ...).
                 **{k: v for k, v in cc.items() if k.startswith("ftps.")}}
        # The plugin needs a trust-store password whenever it is given a trust store (it
        # throws a NullPointerException without one), and the platform keeps credentials
        # off the pipeline, so it comes from the connection's encrypted `secret` field.
        trust_pw = cfg.get("secret")
        if trust_pw and trust_pw != "********":
            # One secret serves the trust store, the key store and its key; the plugin builds an
            # SSL context from both stores (a missing key store location is also an NPE).
            creds.update({"ftps.ssl.truststore.password": trust_pw,
                          "ftps.ssl.keystore.password": trust_pw, "ftps.ssl.key.password": trust_pw})
        if klass == FTPS_SOURCE:
            return {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic, **creds,
                    "ftps.input.path": cfg.get("path") or "/", "ftps.input.file.pattern": cc.get("ftps.input.file.pattern") or cc.get("file_pattern") or ".*",
                    **_lic(), **json_plain, **user_smt}, topic
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic, **creds,
                "ftps.working.dir": cfg.get("path") or "/", "flush.size": str(cc.get("flush_size") or 1),
                "format.class": "io.confluent.connect.ftps.sink.format.json.JsonFormat",
                **_lic(), **json_plain, **user_smt}, None

    # ── HDFS 2 / 3 ──────────────────────────────────────────────────────────
    if klass in (HDFS2_SINK, HDFS3_SINK):
        url = cfg.get("url") or f"hdfs://{h}:{port}"
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "store.url": url, "hdfs.url": url,
             "flush.size": str(cc.get("flush_size") or 1),
             "format.class": ("io.confluent.connect.hdfs.json.JsonFormat" if klass == HDFS2_SINK
                              else "io.confluent.connect.hdfs3.json.JsonFormat"),
             "topics.dir": prefix.replace(".", "/"),
             "partitioner.class": "io.confluent.connect.storage.partitioner.DefaultPartitioner",
             **json_plain, **user_smt}
        # Only the HDFS 3 sink is licence-gated; HDFS 2 rejects the keys as unknown.
        if klass == HDFS3_SINK:
            c.update(_lic())
        return c, None
    if klass in (HDFS2_SOURCE, HDFS3_SOURCE):
        url = cfg.get("url") or f"hdfs://{h}:{port}"
        # The source's format classes live under .format.<type>.* (not .source.format.*
        # as the sinks' namespaces suggest) and its URL key is hdfs.url. Files are read
        # from <topics.dir>, and topic.regex.list maps a file path to the Kafka topic.
        # HDFS 2 reads store.url in its config validation (unset -> NullPointerException in
        # validateStoreUrl) and hdfs.url for the client, so it needs both. HDFS 3 rejects
        # that ("Only store.url or hdfs.url must be provided"), so it gets hdfs.url only.
        urls = {"store.url": url, "hdfs.url": url} if klass == HDFS2_SOURCE else {"hdfs.url": url}
        return {"connector.class": klass, "tasks.max": "1", **urls,
                "format.class": ("io.confluent.connect.hdfs2.format.json.JsonFormat" if klass == HDFS2_SOURCE
                                 else "io.confluent.connect.hdfs3.format.json.JsonFormat"),
                "topics.dir": cc.get("topics_dir") or "topics",
                # The output topic comes from the stored file name (<topic>+<partition>+...),
                # not from topic.regex.list, so route everything to the pipeline's topic.
                **_lic(), **json_plain,
                **{**user_smt, **reroute(".*", topic),
                   "transforms": ",".join([*(x for x in str(user_smt.get("transforms", "")).split(",") if x), "reroute"])}}, topic

    # ── messaging ───────────────────────────────────────────────────────────
    if klass == SYSLOG_SOURCE:
        return {"connector.class": klass, "tasks.max": "1", "topic": topic,
                "syslog.listener": (cc.get("listener") or "UDP").upper(),
                # A listener has no remote host: the connection only has to be reachable for the platform's check,
                # so the port the connector listens on can be set separately with `listen_port`.
                "syslog.listen.address": "0.0.0.0", "syslog.port": str(cc.get("listen_port") or port),
                **_lic(), **json_schema, **user_smt}, topic

    if klass == SNMP_SOURCE:
        return {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
                "snmp.listen.address": "0.0.0.0", "snmp.listen.port": str(cc.get("listen_port") or port),
                "snmp.listen.protocol": (cc.get("protocol") or "UDP").upper(),
                **_lic(), **json_schema, **user_smt}, topic

    if klass in (DIODE_SOURCE, DIODE_SINK):
        # Both halves must share the same password/salt or the receiver cannot
        # decrypt; they are part of the connection profile, not per pipeline.
        # The connector rejects anything shorter than 12 characters
        # ("Password must be atleast 12"), so the fallback has to clear that bar.
        secret = {"diode.encryption.password": cfg.get("password") or "dataconnect-diode",
                  "diode.encryption.salt": cfg.get("salt") or "dataconnect-salt",
                  "diode.port": str(cc.get("diode_port") or port),
                  # The diode carries opaque encrypted bytes, so it pins ALL THREE
                  # converters — headers included — to ByteArrayConverter and refuses
                  # to start on anything else, including the default null header converter.
                  "header.converter": "org.apache.kafka.connect.converters.ByteArrayConverter"}
        if klass == DIODE_SINK:
            return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                    "diode.host": h, **secret, **_lic(), **bytes_passthrough, **user_smt}, None
        # The source writes to <kafka.topic.prefix><original topic name> with nothing between them
        # (e.g. "...-v1org_1001-raw-..."), which breaks the topic naming rule, so route every record
        # to the pipeline's own output topic.
        return {"connector.class": klass, "tasks.max": "1", "kafka.topic.prefix": topic,
                **secret, **_lic(), **bytes_passthrough,
                **{**user_smt, **reroute(".*", topic),
                   "transforms": ",".join([*(x for x in str(user_smt.get("transforms", "")).split(",") if x), "reroute"])}}, topic

    if klass in (ACTIVEMQ_SINK, JMS_SINK, JMS_SOURCE):
        dest = cc.get("destination") or cfg.get("queue") or entity
        dtype = (cc.get("destination_type") or cfg.get("destination_type") or "queue").lower()
        url = cfg.get("url") or f"tcp://{h}:{port}"
        if klass == ACTIVEMQ_SINK:
            c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                 "activemq.url": url, "jms.destination.name": dest, "jms.destination.type": dtype,
                 **_lic(), **json_plain, **user_smt}
            if user:
                c.update({"activemq.username": user, "activemq.password": pwd})
            return c, None
        # Generic JMS: reached through JNDI, so it needs the broker's initial
        # context factory and provider URL, plus that broker's client JAR on the
        # worker classpath (the connector ships none).
        jndi = {"java.naming.factory.initial":
                    cc.get("jndi_factory") or "org.apache.activemq.jndi.ActiveMQInitialContextFactory",
                "java.naming.provider.url": url,
                "jms.destination.name": dest, "jms.destination.type": dtype}
        if user:
            jndi.update({"java.naming.security.principal": user,
                         "java.naming.security.credentials": pwd})
        if klass == JMS_SINK:
            return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                    **jndi, **_lic(), **json_plain, **user_smt}, None
        return {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
                **jndi, **_lic(), **json_plain, **user_smt}, topic

    # ── wide-column: Cassandra / Scylla / HBase ─────────────────────────────
    if klass == CASSANDRA_SINK:
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "cassandra.contact.points": h, "cassandra.port": str(port),
             "cassandra.keyspace": cfg.get("keyspace") or cfg.get("database") or "sample",
             "cassandra.local.datacenter": cfg.get("datacenter") or "datacenter1",
             "cassandra.keyspace.create.enabled": "true",
             "cassandra.table.manage.enabled": "true",
             # The connector defaults this to 3. On a single-node cluster the keyspace
             # is then created asking for 3 replicas that do not exist and every write
             # fails, taking the task down with an "unrecoverable exception".
             "cassandra.keyspace.replication.factor": str(cfg.get("replication_factor") or 1),
             **_lic(), **json_schema, **user_smt}
        if user:
            c.update({"cassandra.username": user, "cassandra.password": pwd,
                      "cassandra.security": "PASSWORD"})
        # The table is named after the record's topic, and a pipeline topic such as
        # org_1001-raw-... is not a valid CQL identifier ("needs double quotes"), so a
        # RegexRouter renames it to the pipeline's entity. The table's primary key is built
        # from the record KEY; a null key aborts the task ("Record with a null key was
        # encountered"), and JDBC-sourced topics carry none, so `key_field` copies that
        # value field(s) into the key with ValueToKey.
        names = [*(x for x in str(user_smt.get("transforms", "")).split(",") if x), "reroute"]
        c.update({k: v for k, v in user_smt.items() if k != "transforms"})
        c.update({k: v for k, v in reroute(".*", entity).items() if k != "transforms"})
        if cc.get("key_field"):
            names = ["dckey", *names]
            c.update({"transforms.dckey.type": "org.apache.kafka.connect.transforms.ValueToKey",
                      "transforms.dckey.fields": cc["key_field"]})
        c["transforms"] = ",".join(names)
        return c, None

    if klass == DATASTAX_SINK:
        ks = cfg.get("keyspace") or cfg.get("database") or "sample"
        # This connector has no auto-create and no implicit field mapping: it needs
        # an explicit `topic.<topic>.<keyspace>.<table>.mapping` of column=field
        # pairs. Without one it starts and then silently writes nothing, so demand
        # it up front with a message that says exactly what to supply.
        mapping = cc.get("mapping")
        if not mapping:
            raise ValueError(
                "DataStax Cassandra sink needs a 'mapping' in the pipeline config — "
                "column=field pairs, e.g. 'employee_id=value.employee_id, first_name=value.first_name'.")
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "contactPoints": h, "port": str(port),
             "loadBalancing.localDc": cfg.get("datacenter") or "datacenter1",
             f"topic.{sink_topic}.{ks}.{entity}.mapping": mapping,
             f"topic.{sink_topic}.{ks}.{entity}.consistencyLevel": "LOCAL_ONE",
             **json_schema, **user_smt}
        if user:
            c.update({"auth.username": user, "auth.password": pwd})
        return c, None

    if klass == HBASE_SINK:
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "hbase.zookeeper.quorum": h,
                "hbase.zookeeper.property.clientPort": str(port),
                # The plugin defaults to the Kerberos layout "/hbase-secure"; an ordinary (unsecured) HBase
                # registers under "/hbase". With the wrong parent the connector never finds the master:
                # it shows RUNNING but never creates a task and logs nothing.
                "zookeeper.znode.parent": cc.get("znode_parent") or "/hbase",
                "auto.create.tables": "true", "auto.create.column.families": "true",
                "table.name.format": entity,
                **_lic(), **json_schema, **user_smt}, None

    if klass == SCYLLA_CDC:
        ks = cfg.get("keyspace") or cfg.get("database") or "sample"
        c = {"connector.class": klass, "tasks.max": "1",
             "scylla.cluster.ip.addresses": f"{h}:{port}",
             "scylla.name": prefix.replace(".", "_"),
             "scylla.table.names": ",".join(
                 t if "." in t else f"{ks}.{t}" for t in tables),
             "topic.prefix": prefix,
             **json_plain, **user_smt}
        if user:
            c.update({"scylla.user": user, "scylla.password": pwd})
        # Scylla emits one topic per table (prefix.keyspace.table) — fold them
        # onto the pipeline's canonical topic the same way Debezium sources do.
        c.update(reroute(rf"{prefix}\.[^.]+\.[^.]+", topic))
        return c, topic

    if klass == YUGABYTE_SOURCE:
        # A Debezium PostgreSQL connector against YugabyteDB's YSQL layer, so it
        # takes the Debezium key names with Yugabyte's own output plugin.
        return {"connector.class": klass, "tasks.max": "1",
                "database.hostname": h, "database.port": str(port),
                "database.user": user, "database.password": pwd,
                "database.dbname": cfg.get("database") or "yugabyte",
                "plugin.name": cc.get("plugin_name") or "yboutput",
                "slot.name": f"dbz_{entity}".lower(),
                "table.include.list": ",".join(t if "." in t else f"public.{t}" for t in tables),
                "topic.prefix": prefix, "snapshot.mode": cc.get("snapshot_mode") or "initial",
                "decimal.handling.mode": "double",
                **json_plain, **user_smt,
                **reroute(rf"{prefix}\.[^.]+\.[^.]+", topic)}, topic

    # ── analytics / time-series / warehouse ────────────────────────────────
    if klass == CLICKHOUSE_SINK:
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "hostname": h, "port": str(port),
                "database": cfg.get("database") or "default",
                "username": user or "default", "password": pwd or "",
                "ssl": "false", "exactlyOnce": "false",
                # The connector writes into the table named after the topic unless
                # told otherwise; the pipeline's entity is the destination table.
                "topic2TableMap": f"{sink_topic}={entity}",
                **json_schema, **user_smt}, None

    if klass == QUESTDB_SINK:
        # QuestDB ingests over the InfluxDB line protocol on 9009, not its pg-wire port.
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "host": f"{h}:{port}", "table": entity,
                "include.key": "false",
                **json_schema, **user_smt}, None

    if klass in (INFLUX_SINK, INFLUX_SOURCE):
        url = cfg.get("url") or f"http://{h}:{port}"
        db = cfg.get("database") or "sample"
        creds = {"influxdb.username": user, "influxdb.password": pwd} if user else {}
        if klass == INFLUX_SINK:
            c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                 "influxdb.url": url, "influxdb.db": db, "measurement.name.format": entity,
                 # Points are identified by measurement + tags + time, and the sink takes tags from a
                 # `tags` field of the record. `schemaless` reads plain JSON such as
                 # {"measurement": "...", "tags": {"sensor": "s1"}, "temperature": 21.5}.
                 **creds, **_lic(), **(json_plain if cc.get("schemaless") else json_schema), **user_smt}
            # The sink writes Connect Date/Timestamp values with Date.toString() and no quotes
            # ("created_at=Thu Sep 17 03:35:39 UTC 2026"), which is invalid line protocol. Fields named
            # in `timestamp_fields` are converted to strings first with Kafka's TimestampConverter.
            ts_fields = cc.get("timestamp_fields") or []
            if isinstance(ts_fields, str):
                ts_fields = [f.strip() for f in ts_fields.split(",") if f.strip()]
            if ts_fields:
                names = [*(x for x in str(user_smt.get("transforms", "")).split(",") if x)]
                for i, f in enumerate(ts_fields):
                    n = f"ts{i}"
                    names.append(n)
                    c.update({f"transforms.{n}.type": "org.apache.kafka.connect.transforms.TimestampConverter$Value",
                              f"transforms.{n}.field": f, f"transforms.{n}.target.type": "string",
                              f"transforms.{n}.format": "yyyy-MM-dd HH:mm:ss"})
                c["transforms"] = ",".join(names)
            return c, None
        return {"connector.class": klass, "tasks.max": "1",
                "influxdb.url": url, "influxdb.db": db,
                "influxdb.measurement.whitelist": ",".join(tables) if tables else "",
                "topic.prefix": prefix + ".",
                **creds, **_lic(), **json_schema, **user_smt,
                **reroute(".*", topic)}, topic

    if klass == SINGLESTORE_SINK:
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "connection.ddlEndpoint": f"{h}:{port}",
                "connection.database": cfg.get("database") or "sample",
                "connection.user": user or "root", "connection.password": pwd or "",
                f"singlestore.tableName.{sink_topic}": entity,
                **json_schema, **user_smt}, None

    if klass == TIMEPLUS_SINK:
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             # Timeplus ingests over its REST port (3218), not the native TCP port.
             "timeplus.sink.address": cfg.get("url") or f"http://{h}:{cfg.get('rest_port') or 3218}",
             "timeplus.sink.stream": entity,
             "timeplus.sink.createStream": "true",
             "timeplus.sink.dataFormat": "json",
             # The sink parses each record value as JSON *text* (org.json), so the
             # value must reach it as a string — a JsonConverter hands it a Map whose
             # toString() ("{a=1}") is not JSON.
             "key.converter": "org.apache.kafka.connect.storage.StringConverter",
             "value.converter": "org.apache.kafka.connect.storage.StringConverter",
             **user_smt}
        if cfg.get("workspace"):
            c["timeplus.sink.workspace"] = cfg["workspace"]
        if _real_secret(cfg.get("api_key")):
            c["timeplus.sink.apikey"] = cfg.get("api_key")
        return c, None

    if klass == PROMETHEUS_SINK:
        # The connector does not push: it serves a scrape endpoint on the worker
        # that Prometheus pulls from, so the listener URL must bind 0.0.0.0 to be
        # reachable from the Prometheus container.
        # This connector reports per-record results through Confluent's reporter
        # framework, which starts its own producer; without an explicit
        # reporter.bootstrap.servers it dies on "bootstrap.servers: Empty list".
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "prometheus.listener.url": cfg.get("url") or f"http://0.0.0.0:{cc.get('listen_port') or cfg.get('port') or 8889}/metrics",
                "behavior.on.error": "log",
                "reporter.bootstrap.servers": settings.kafka_bootstrap,
                "reporter.result.topic.replication.factor": "1",
                "reporter.error.topic.replication.factor": "1",
                **_lic(), **json_schema, **user_smt}, None

    # ── cache / KV ──────────────────────────────────────────────────────────
    if klass == REDIS_SINK:
        # Redis keys the record by the Kafka key, and rejects a null one outright
        # ("The key for the record cannot be null"). Sources like JDBC and Datagen
        # emit no key, so derive one from a field.
        # This connector stores exactly what is on the topic: it demands a String or
        # Bytes key AND value, has no struct handling, and bundles no JSON transform
        # (an SMT from another plugin is invisible to its isolated classloader). So
        # read the key as text and the value as raw bytes, and feed it a topic that
        # already carries a key — a source pipeline with `key_field` set produces one.
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "redis.hosts": f"{h}:{port}",
             "redis.database": str(cfg.get("database") or 0),
             "redis.client.mode": (cc.get("client_mode") or "Standalone"),
             "key.converter": "org.apache.kafka.connect.storage.StringConverter",
             "value.converter": "org.apache.kafka.connect.converters.ByteArrayConverter",
             **user_smt}
        # A connection type that requires a password makes one up for a Redis with none, and
        # AUTH against a password-less Redis is an error; `no_auth` leaves it out.
        if pwd and not cc.get("no_auth"):
            c["redis.password"] = pwd
        return c, None

    if klass == AEROSPIKE_SINK:
        # This plugin's ConfigDef will not validate over the REST API, so these
        # key names come from the connector's own documentation rather than from
        # the worker — treat a rejected deploy here as a config-name mismatch first.
        ns = cfg.get("namespace") or "test"
        key = cc.get("key_field") or "id"
        # One mapping block per topic. The connector takes a whole YAML document
        # (`config`); its schema is topics -> <topic> -> mapping, not flat keys.
        topic_blocks = "".join(
            f"  {t}:\n"
            f"    invalid-record: {cc.get('invalid_record') or 'kill-task'}\n"
            f"    mapping:\n"
            f"      namespace:\n        mode: static\n        value: {ns}\n"
            f"      set:\n        mode: static\n        value: {entity}\n"
            f"      key-field:\n        source: value-field\n        field-name: {key}\n"
            f"      bins:\n        type: multi-bins\n        all-value-fields: true\n"
            for t in str(sink_topic).split(","))
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "config": cc.get("config") or (
                    "---\n"
                    f"aerospike:\n"
                    f"  seeds:\n"
                    f"    - {h}:\n"
                    f"        port: {port}\n"
                    f"topics:\n" + topic_blocks),
                **json_schema, **user_smt}, None

    if klass in (COUCHBASE_SOURCE, COUCHBASE_SINK):
        seed = {"couchbase.seed.nodes": h,
                "couchbase.username": user or "dataconnect",
                "couchbase.password": pwd or "dataconnect",
                "couchbase.bucket": cfg.get("bucket") or cfg.get("database") or "sample"}
        if klass == COUCHBASE_SINK:
            # Keys here are Couchbase document ids (plain strings such as "emp::1"),
            # which a JsonConverter key converter cannot parse; only the value is JSON.
            return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                    **seed, **json_plain,
                    "key.converter": "org.apache.kafka.connect.storage.StringConverter",
                    **user_smt}, None
        return {"connector.class": klass, "tasks.max": "1",
                "couchbase.topic": topic,
                "couchbase.source.handler": "com.couchbase.connect.kafka.handler.source.RawJsonSourceHandler",
                "couchbase.stream.from": cc.get("stream_from") or "SAVED_OFFSET_OR_BEGINNING",
                # The document id is a String key, which ByteArrayConverter rejects
                # ("not compatible with objects of type java.lang.String"); every record
                # then fails the KEY_CONVERTER stage and is dropped under errors.tolerance=all.
                # The value stays the document's raw JSON bytes.
                **seed, **bytes_passthrough,
                "key.converter": "org.apache.kafka.connect.storage.StringConverter",
                **user_smt}, topic

    # ── vector stores ───────────────────────────────────────────────────────
    if klass == QDRANT_SINK:
        # The plugin parses this with java.net.URL, so it must carry a scheme (https
        # turns TLS on). Its shaded gRPC jar also ships without the DNS name-resolver
        # entry in META-INF/services — see docs/CONNECTOR_TEST_SETUP.md — and without that
        # patch every host name fails with "NameResolver 'unix' ... not supported".
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "qdrant.grpc.url": cfg.get("url") or f"http://{h}:{port}",
             "qdrant.collection.name": entity,
             **json_plain, **user_smt}
        if _real_secret(cfg.get("api_key")):
            c["qdrant.api.key"] = cfg.get("api_key")
        return c, None

    if klass == WEAVIATE_SINK:
        http_port = cfg.get("port") or 8080
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "weaviate.connection.url": cfg.get("url") or f"http://{h}:{http_port}",
             "weaviate.grpc.url": cfg.get("grpc_url") or f"{h}:{cfg.get('grpc_port') or 50051}",
             "weaviate.grpc.secured": "false",
             "weaviate.auth.scheme": "NONE",
             # Weaviate class names must start uppercase; the entity does not.
             # A name format (it may contain ${topic}), not a topic:Class pair: the pair was taken whole as the
             # class name, which Weaviate rejects, and the records were dropped without any error.
             "collection.mapping": entity.capitalize(),
             "document.id.strategy": cc.get("id_strategy") or "io.weaviate.connector.idstrategy.NoIdStrategy",
             "vector.strategy": cc.get("vector_strategy") or "io.weaviate.connector.vectorstrategy.NoVectorStrategy",
             # The sink maps each record's properties from its value SCHEMA; a schemaless record produces no
             # object and no error. `with_schema` reads JSON with the embedded schema envelope instead.
             **(json_schema if cc.get("with_schema") else json_plain), **user_smt}
        if _real_secret(cfg.get("api_key")):
            c.update({"weaviate.api.key": cfg.get("api_key"), "weaviate.auth.scheme": "API_KEY"})
        return c, None

    if klass == MILVUS_SINK:
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "public.endpoint": cfg.get("url") or f"http://{h}:{port}",
                "token": _real_secret(cfg.get("token")) or f"{user or 'root'}:{pwd or 'Milvus'}",
                "collection.name": entity,
                "database.name": cfg.get("database") or "default",
                **json_plain, **user_smt}, None

    # ── graph ───────────────────────────────────────────────────────────────
    if klass in (NEO4J_SINK, NEO4J_SOURCE):
        auth = {"neo4j.uri": cfg.get("url") or f"bolt://{h}:{port}",
                "neo4j.authentication.type": "BASIC",
                "neo4j.authentication.basic.username": user or "neo4j",
                "neo4j.authentication.basic.password": pwd or "",
                "neo4j.database": cfg.get("database") or "neo4j"}
        if klass == NEO4J_SINK:
            # Neo4j has no schema to infer a write into, so the pipeline supplies
            # the Cypher; default to a MERGE on the entity label keyed by `id`.
            cypher = cc.get("cypher") or (
                f"MERGE (n:{entity.capitalize()} {{id: event.id}}) SET n += event")
            return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                    # The record value is already bound as `event` by default; setting
                    # neo4j.cypher.bind-value-as=event as well emits two `event` columns
                    # ("Multiple result columns with the same name").
                    f"neo4j.cypher.topic.{sink_topic}": cypher,
                    **auth, **json_plain, **user_smt}, None
        query = cc.get("query") or f"MATCH (n:{entity.capitalize()}) RETURN n"
        streaming = ({"neo4j.query.streaming-property": cc["streaming_property"]}
                     if cc.get("streaming_property") else {})
        return {"connector.class": klass, "tasks.max": "1",
                "neo4j.source-strategy": "QUERY",
                "neo4j.query": query, "neo4j.query.topic": topic, **streaming,
                # EXTENDED (the default) wraps every field in a typed-union struct
                # ({"id": {"type": "I64", "I64": 3, "S": null, ...}}), which no sink can
                # read back; COMPACT emits plain {"id": 3}.
                "neo4j.payload-mode": cc.get("payload_mode") or "COMPACT",
                # NOW (the connector default) starts at the current transaction and
                # would skip every existing node, so a first run reads nothing.
                # EARLIEST replays what is already in the graph. "ALL" is rejected.
                "neo4j.start-from": cc.get("start_from") or "EARLIEST",
                **auth, **json_plain, **user_smt}, topic

    # ── workflow ────────────────────────────────────────────────────────────
    if klass == LITTLEHORSE_SINK:
        wf = cc.get("wf_spec_name") or entity
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "lhc.api.host": h, "lhc.api.port": str(port),
                "wf.spec.name": wf,
                **json_plain, **user_smt}, None

    # ── T2: Amazon EventBridge (LocalStack stands in for AWS) ──────────────
    if klass == EVENTBRIDGE_SINK:
        region = cfg.get("region") or "us-east-1"
        bus = cc.get("event_bus") or cfg.get("event_bus") or "default"
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "aws.eventbridge.connector.id": pipeline["connector_name"],
             "aws.eventbridge.region": region,
             "aws.eventbridge.eventbus.arn": cc.get("eventbus_arn")
                 or f"arn:aws:events:{region}:000000000000:event-bus/{bus}",
             **json_schema, **user_smt}
        # Pointing endpoint.uri at LocalStack is what makes this testable offline;
        # left empty the SDK resolves the real AWS endpoint.
        endpoint = cfg.get("base_url") or cfg.get("endpoint_url")
        if endpoint:
            c["aws.eventbridge.endpoint.uri"] = endpoint
        return c, None

    # ── T2: HEAVY.AI / OmniSci (Thrift port 6274; the plugin bundles its JDBC driver) ──
    if klass == OMNISCI_SINK:
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "connection.host": h, "connection.port": str(port),
             "connection.database": cfg.get("database") or "omnisci",
             "connection.user": user or "admin", "connection.password": pwd or "",
             # The destination table is the pipeline entity; it is created from the record schema when missing.
             "table.name.format": entity, "auto.create": "true",
             **_lic(), **json_schema, **user_smt}
        return c, None

    # ── T2: Diffusion (WebSocket pub/sub server) ─────────────────────────────
    if klass in (DIFFUSION_SINK, DIFFUSION_SOURCE):
        c = {"connector.class": klass, "tasks.max": "1",
             "diffusion.url": cfg.get("url") or f"{cfg.get('scheme') or 'ws'}://{h}:{port}",
             "diffusion.username": user or "admin", "diffusion.password": pwd or "",
             # Diffusion topics carry JSON text; keys are plain strings (they only form the topic path).
             "key.converter": "org.apache.kafka.connect.storage.StringConverter",
             "value.converter": "org.apache.kafka.connect.json.JsonConverter",
             "value.converter.schemas.enable": "false",
             **user_smt}
        if klass == DIFFUSION_SINK:
            # Where each Kafka topic is published; ${topic} and ${key} are substituted by the connector.
            c.update({"topics": sink_topic, "diffusion.destination": cc.get("destination") or "kafka/${topic}"})
            return c, None
        # A Diffusion topic selector; every update of a matching topic becomes one Kafka record.
        c.update({"kafka.topic": topic, "diffusion.selector": cc.get("selector") or "?kafka/.*"})
        return c, topic

    # ── T2: Vertica (the Vertica JDBC driver is NOT bundled with the plugin) ─────
    if klass == VERTICA_SINK:
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "vertica.host": h, "vertica.port": str(port),
             "vertica.database": cfg.get("database") or "",
             "vertica.username": user or "dbadmin", "vertica.password": pwd or "",
             # The destination table is the pipeline entity; it is created from the record schema when missing.
             "table.name.format": entity, "auto.create": "true",
             "pk.mode": cc.get("pk_mode") or "none",
             **json_schema, **user_smt}
        if cc.get("pk_fields"):
            c["pk.fields"] = cc["pk_fields"]
        return c, None

    # ── T2: Splunk (HTTP Event Collector) ───────────────────────────────────
    if klass == SPLUNK_SINK:
        token = _real_secret(cfg.get("token")) or _real_secret(cfg.get("api_key")) or _real_secret(cfg.get("password"))
        if not token:
            raise ValueError("Splunk connection needs the HEC token ('token').")
        scheme = cfg.get("scheme") or "https"
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "splunk.hec.uri": cfg.get("url") or f"{scheme}://{h}:{port}",
             "splunk.hec.token": token,
             "splunk.indexes": cc.get("index") or "main",
             "splunk.sourcetypes": cc.get("sourcetype") or "_json",
             # Splunk's default HEC certificate is self-signed; set the pipeline option `validate_certs` to true
             # once it is replaced. Events are sent to the /event endpoint (JSON), not /raw.
             "splunk.hec.ssl.validate.certs": "true" if cc.get("validate_certs") else "false",
             "splunk.hec.raw": "false",
             # Events are batched for this many seconds before they are sent (default 30).
             "splunk.flush.window": str(cc.get("flush_window") or 5),
             **json_plain, **user_smt}
        return c, None

    # ── REST API source (castorm kafka-connect-http): polls an endpoint, one request per interval ──────────────────────────────
    if klass == HTTP_SOURCE:
        return _http_source_config(klass, cfg=cfg, cc=cc, topic=topic, json_plain=json_plain, user_smt=user_smt), topic


    # The two Splunk sources are listeners: they open a port inside the Connect worker and Splunk (or any HEC client /
    # forwarder) sends to it. The connection only has to point at something reachable (the worker itself); the port the
    # connector listens on is the pipeline option `listen_port` (default 8889 for HEC, 9997 for S2S).
    if klass == SPLUNK_HTTP_SOURCE:
        token = _real_secret(cfg.get("token")) or _real_secret(cfg.get("api_key")) or _real_secret(cfg.get("password"))
        c = {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
             "splunk.port": str(cc.get("listen_port") or 8889),
             "splunk.collector.index.default": cc.get("index") or "main",
             **_lic(), **json_plain, **user_smt}
        if token:
            c["splunk.collector.authentication.tokens"] = token
        # The HEC listener is HTTPS only (it logs "SSL, (ssl, http/1.1)") and cannot answer without a keystore on
        # the worker: without these two settings every request dies in the TLS handshake with no error to the client.
        if cc.get("keystore_path"):
            c["splunk.ssl.key.store.path"] = cc["keystore_path"]
            # The password is a credential: it comes from the connection's `secret`, never from the pipeline options.
            c["splunk.ssl.key.store.password"] = _real_secret(cfg.get("secret")) or "changeit"
        return c, topic

    if klass == SPLUNK_S2S_SOURCE:
        c = {"connector.class": klass, "tasks.max": "1", "kafka.topic": topic,
             "splunk.s2s.port": str(cc.get("listen_port") or 9997),
             **_lic(), **json_plain, **user_smt}
        return c, topic

    # ── T2: VMware Tanzu GemFire / Apache Geode (a client connects through the locator to a region) ──
    if klass == GEMFIRE_SINK:
        c = {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
             "gemfire.locator.host": h, "gemfire.locator.port": str(port),
             # The region must already exist on the cluster; its name is the pipeline entity (or option `region`).
             "gemfire.region": cc.get("region") or entity,
             # `kafka` keys each entry by topic-partition-offset (no record key needed); `record_key` uses the key.
             "key.mode": cc.get("key_mode") or "kafka",
             # Values are written as Connect structs, so the records must carry a schema.
             **_lic(), **json_schema, **user_smt}
        if cc.get("key_fields"):
            c["key.fields"] = cc["key_fields"]
        if cc.get("auth_enabled"):
            c.update({"gemfire.username": user or "", "gemfire.password": pwd or ""})
        return c, None

    # ── T2: Firebase Realtime Database (the Firebase emulator stands in for Google) ──
    if klass in (FIREBASE_SINK, FIREBASE_SOURCE):
        # The database reference is the database URL; an emulator is addressed as http://host:9000?ns=<namespace>.
        ref = cfg.get("url") or cc.get("database_url") or f"https://{h}.firebaseio.com"
        c = {"connector.class": klass, "tasks.max": "1", "gcp.firebase.database.reference": ref,
             **_lic(), **json_plain, **user_smt}
        # Exactly one of the two credential settings: a service-account key file on the worker, or its JSON.
        if cc.get("credentials_json"):
            c["gcp.firebase.credentials.json"] = cc["credentials_json"]
        else:
            c["gcp.firebase.credentials.path"] = cc.get("credentials_path") or cfg.get("credentials_path") or ""
        if klass == FIREBASE_SINK:
            c.update({"topics": sink_topic, "insert.mode": cc.get("insert_mode") or "set"})
            return c, None
        c["gcp.firebase.snapshot"] = "true" if cc.get("snapshot", True) else "false"
        # The source names its topic after the database path; send every record to the pipeline topic.
        names = [*(x for x in str(user_smt.get("transforms", "")).split(",") if x), "reroute"]
        c.update({k: v for k, v in user_smt.items() if k != "transforms"})
        c.update({k: v for k, v in reroute(".*", topic).items() if k != "transforms"})
        c["transforms"] = ",".join(names)
        return c, topic

    # ── T2: Azure Cosmos DB (NoSQL API; the Linux emulator stands in for Azure) ──
    if klass in (COSMOS_SINK, COSMOS_SOURCE):
        key = _real_secret(cfg.get("master_key")) or _real_secret(cfg.get("password")) or _real_secret(cfg.get("api_key"))
        if not key:
            raise ValueError("Cosmos DB connection needs the account key ('master_key' or 'password').")
        container = cc.get("container") or entity
        c = {"connector.class": klass, "tasks.max": "1",
             "connect.cosmos.connection.endpoint": cfg.get("url") or f"https://{h}:{port}",
             "connect.cosmos.master.key": key,
             "connect.cosmos.databasename": cfg.get("database") or "",
             # The emulator only serves the gateway mode; real Azure accepts it too (pipeline option `gateway`: false
             # selects direct mode there).
             "connect.cosmos.connection.gateway.enabled": "false" if cc.get("gateway") is False else "true",
             # The connector reads and writes documents as JSON text, not Connect structs with a schema.
             **json_plain, **user_smt}
        if klass == COSMOS_SINK:
            c.update({"topics": sink_topic, "connect.cosmos.containers.topicmap": f"{sink_topic}#{container}"})
            return c, None
        c["connect.cosmos.containers.topicmap"] = f"{topic}#{container}"
        return c, topic

    # ── T2: GridGain 9 / Apache Ignite 3 (SQL tables over the thin-client port 10800) ──
    if klass in (GRIDGAIN_SINK, GRIDGAIN_SOURCE):
        table = cc.get("table") or entity
        c = {"connector.class": klass, "tasks.max": "1",
             "ignite.addresses": cc.get("addresses") or f"{h}:{port}",
             # Map every topic/table name to the one this pipeline uses; without it the connector would use the
             # platform's topic name (org_1001-raw-...) as the table name.
             # Anchored: a bare ".*" matches the whole name and then the empty string at its end, so the
             # replacement is applied twice ("gg_itemsgg_items").
             "ignite.table.name.regex": "^.*$",
             **json_schema, **user_smt}
        if cc.get("auth_enabled"):
            c["ignite.authenticator.basic.username"] = user or ""
            c["ignite.authenticator.basic.password"] = pwd or ""
        if klass == GRIDGAIN_SINK:
            c.update({"topics": sink_topic, "ignite.table.name.regex.replacement": table})
            # The table's primary key columns are filled from the record KEY (a struct of the key columns); with a null
            # key the connector fails with "Column not found in GridGain for Kafka field 'key'". Topics from JDBC or
            # Datagen carry no key, so `key_field` copies those value field(s) into a struct key with ValueToKey.
            if cc.get("key_field"):
                names = ["dckey", *(x for x in str(c.get("transforms", "")).split(",") if x)]
                c.update({"transforms.dckey.type": "org.apache.kafka.connect.transforms.ValueToKey",
                          "transforms.dckey.fields": cc["key_field"], "transforms": ",".join(names)})
            return c, None
        c.update({"ignite.tables": table, "ignite.table.name.regex.replacement": topic})
        return c, topic

    # ── T2: Lightstreamer ──────────────────────────────────────────────────
    if klass == LIGHTSTREAMER_SINK:
        # The connector needs an explicit field-extraction mapping; there is no
        # "just forward the record" default.
        return {"connector.class": klass, "tasks.max": "1", "topics": sink_topic,
                "lightstreamer.server.proxy_adapter.address":
                    cc.get("proxy_adapter") or f"{h}:{cfg.get('proxy_port') or 6661}",
                "topic.mappings": cc.get("topic_mappings") or f"{sink_topic}:{entity}",
                "record.mappings": cc.get("record_mappings") or "value:#{VALUE}",
                # `#{VALUE.field}` needs a Connect Struct ("A Schema is required" otherwise, and every record is
                # silently skipped), so records must carry their schema: JSON with schema, not schemaless JSON.
                **json_schema, **user_smt}, None

    raise ValueError(f"Connector class '{klass}' is registered as T1 but has no config builder.")

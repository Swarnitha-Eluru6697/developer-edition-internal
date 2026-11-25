# Connector test log

One entry per connector template, added after its pipeline has been run. Each entry records the result, any issue hit, and how it was fixed.

---

## JMS Sink (`tpl_jms_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-jms-sink` 2.2.2 |
| Backend | ActiveMQ classic 6.1.2 (`apache/activemq-classic`), queue `dataconnect.q` |
| Pipeline | `t1-jms_sink-employees` (connector `org_1001--t1--jmssink--employees--batch--v1`) |
| Input topic | `org_1001.raw.db.postgres.products.batch.v1` (58 records) |
| Evidence | Connector and task RUNNING; all 58 records in the ActiveMQ queue (`EnqueueCount` 58); Kafka consumer lag 0 |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The repo backend had no config builder for the T1 connectors, so the template could not be turned into a Connect config. | Ported `connect_config_t1.py` into `app/services/` and hooked it into `connect_config.py`. The hook reads the pipeline's `direction` (the pinned template snapshot has no `connector_type`). |
| 2 | Deploy was blocked: the seed said plugin version 2.2.3 but 2.2.2 is installed, and the deploy check rejects a template newer than the installed plugin. | Set `connector_version` for `tpl_jms_sink` to 2.2.2 in `app/db/seed_templates.py`. |
| 3 | The sink task could not connect to ActiveMQ ("Cannot send, channel has already failed"). The plugin ships no ActiveMQ client, and the copied client was missing its `hawtbuf` dependency. | Copy `activemq-client`, `jakarta.jms-api` and `hawtbuf` from the JMS source plugin into the sink plugin's `lib/`. Saved in the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`). |

---

## Qdrant (`tpl_qdrant_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `qdrant/qdrant-kafka` 1.4.1 |
| Backend | `qdrant/qdrant` container, gRPC 6334 (dashboard at localhost:6333/dashboard), collection `employees` (3-dim, cosine) |
| Pipeline | `t1-qdrant_sink-employees` (connector `org_1001--t1--qdrantsink--employees--batch--v1`) |
| Input topic | `org_1001.raw.t1.qdrant_in.batch.v1` (5 vector records) |
| Evidence | Connector and task RUNNING; 5 points in the collection with the expected payloads (Ann, Bob, Cara, Dev, Eve) |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The task died with "NameResolver 'unix' ... not supported by transport", then "Could not find policy 'pick_first'". The plugin's shaded gRPC jar registers only a Unix-socket name resolver and no default load balancer, so no host name can be resolved. | Patched the plugin jar's `META-INF/services` files to register `DnsNameResolverProvider` and `PickFirstLoadBalancerProvider`. Saved in the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`). |
| 2 | With that fixed, the task failed with "Invalid GRPC URL: qdrant:6334". The plugin parses the address with `java.net.URL`, so a bare `host:port` is rejected. | The builder now sends `http://host:port` (`connect_config_t1.py`). |
| 3 | Records from an ordinary topic (the products data) all went to the DLQ with "'collection_name' value is required". The sink only accepts records shaped as `collection_name`, `id`, `vector`, `payload`. | Not a bug. Test with a topic of vector-shaped JSON records and create the Qdrant collection first (vector size must match). |

---

## Timeplus (`tpl_timeplus_sink`)

**Result:** BLOCKED (backend mismatch, not a connector or platform defect)

| | |
|---|---|
| Plugin | `timeplus/kafka-timeplus-connector-sink` 1.0.5 |
| Planned backend | `timeplus/timeplusd` |
| Evidence | Pipeline deployed and the task showed RUNNING, but nothing was written and no stream was created. A direct probe of `timeplusd` showed the API the plugin calls does not exist there. |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The connection pointed at the native TCP port (8463). The plugin ingests over HTTP. | The builder now sends `http://host:3218` (`connect_config_t1.py`). |
| 2 | The sink task crashed with "Expected a ':' after a key". The plugin parses each record value as JSON text, and the JSON converter hands it a Java Map. | The builder now uses the string converter for Timeplus (`connect_config_t1.py`). |
| 3 | Still nothing written. The plugin posts to `/{workspace}/api/v1beta2/streams/{stream}/ingest` with an `X-API-KEY` header. On `timeplusd` port 3218 that path returns 404 and `/api/v1beta2/streams` has no handler; the 400s are only SQL parse errors on the body. | Not fixable in the platform. It needs the Timeplus Enterprise image (`timeplus/timeplus-enterprise`, web service on port 8000, API key from its console; free 30-day trial). Not run. |

---

## Neo4j Source (`tpl_neo4j_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `neo4j/kafka-connect-neo4j` 5.5.2 |
| Backend | `neo4j:5` container (browser at localhost:7474, user `neo4j`), 5 `Employee` nodes |
| Pipelines | `t1-neo4j_source-employees` (v1, first attempt) and `t1-neo4j_source-employees-v2` (final) |
| Output topics | `org_1001-raw-t1-neo4jsource-employees-batch-v1` and `-v2` (5 records each) |
| Evidence | Connector and task RUNNING; v2 topic holds plain JSON such as `{"id":1,"name":"Ann","dept":"Sales","ctc":55000,"updatedAt":...}` |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy would be blocked: seed said plugin version 5.5.4, installed is 5.5.2. | Set `connector_version` to 5.5.2 for both Neo4j templates in `app/db/seed_templates.py`. |
| 2 | The connector's default payload mode (`EXTENDED`) wrapped every field in a typed-union struct (`{"id":{"type":"I64","I64":3,"S":null,...}}`). That is valid but no sink can read it back; v1 shows this. | The builder now sets `neo4j.payload-mode=COMPACT` (plain `{"id":3}`). Used for v2. |
| 3 | The query strategy reads incrementally and needs a streaming property plus a `$lastCheck` filter in the query. | The builder accepts a `streaming_property` option on the pipeline, next to `query` and `start_from`. Test query: `MATCH (n:Employee) WHERE n.updatedAt > $lastCheck RETURN n.id AS id, ...`, streaming property `updatedAt`. `start-from` stays `EARLIEST` so existing nodes are read (`ALL` is rejected, `NOW` would skip them). |

---

## Neo4j Sink (`tpl_neo4j_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `neo4j/kafka-connect-neo4j` 5.5.2 |
| Backend | `neo4j:5` container, nodes written with label `Employees` |
| Pipelines | `t1-neo4j_sink-employees` (v1, failed: 5 records in its DLQ) and `t1-neo4j_sink-employees-v2` (final) |
| Input topic | `org_1001-raw-t1-neo4jsource-employees-batch-v2` (the Neo4j Source output) |
| Evidence | Connector and task RUNNING; 5 `Employees` nodes in Neo4j with the expected id, name, dept and ctc; v2 DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Every record failed with "Multiple result columns with the same name are not supported". The builder set `neo4j.cypher.bind-value-as=event`, but the connector already binds the value as `event`, so the generated query had two `event` columns. | Removed that setting from the builder (`connect_config_t1.py`). The default Cypher (`MERGE (n:Employees {id: event.id}) SET n += event`) was fine. |
| 2 | Same seed version mismatch as the source (5.5.4 vs 5.5.2). | Fixed together with the source. |

---

## Re-runs with `-` topic names (JMS Sink and Qdrant)

The first runs of JMS Sink and Qdrant read from dotted input topics (`org_1001.raw.db.postgres.products.batch.v1`, and `org_1001.raw.t1.qdrant_in.batch.v1`, a topic created by hand during testing). Topic names must use `-` (they are linked to UCS), so both were re-run as v2 pipelines on `-` topics. The v1 pipelines and topics were kept.

| Connector | v2 pipeline | Input topic | Result |
|---|---|---|---|
| JMS Sink | `t1-jms_sink-employees-v2` | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) | PASS: connector and task RUNNING; all 58 records in ActiveMQ queue `dataconnect.dash.q`. |
| Qdrant | `t1-qdrant_sink-employees-v2` | `org_1001-raw-t1-qdrant-in-batch-v1` (5 vector records) | PASS: connector and task RUNNING; consumer lag 0, DLQ empty. |

Finding from the Qdrant re-run: the sink writes into the collection named by the pipeline's entity (`qdrant.collection.name`, here `employees`) and only requires `collection_name` to be present in each record; it does not route by that field. Both Qdrant pipelines therefore write into the same `employees` collection (same ids, so the count stays 5). To get a separate collection for a test, use a different entity.

Also removed during cleanup: an empty topic `org_1001.raw.t1.neo4jsource.employees.batch.v1` that a console consumer auto-created by mistake (the real Neo4j topics use `-`).

---

## Aerospike Sink (`tpl_aerospike_sink`)

**Result:** PASS after a retest (superseded: this first run was BLOCKED on the community image, see "Aerospike retest" at the end of this file)

| | |
|---|---|
| Plugin | `aerospike/kafka-connect-aerospike-sink` 3.2.9 |
| Backend | `aerospike/aerospike-server` (community) container, namespace `test` |
| Pipelines | `t1-aerospike_sink-employees` (v1, config rejected) and `t1-aerospike_sink-products-v2` (final attempt; v1 of `-products` also kept) |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector RUNNING; the task starts, parses the config and connects to Aerospike, then stops with `mesg-kafka-connector feature not enabled on Aerospike Server`. |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy would be blocked: seed said plugin version 3.2.10, installed is 3.2.9. | Set `connector_version` to 3.2.9 in `app/db/seed_templates.py`. |
| 2 | The connector rejected the config: `Unrecognized field "topic-routing"`. The YAML schema is `topics -> <topic> -> mapping`, not a `topic-routing` block. | The builder now generates `topics: <topic>: invalid-record, mapping: {namespace, set, key-field, bins}` (one block per input topic). Options `key_field` and `invalid_record` can be set on the pipeline. `invalid-record` is mandatory in the connector. |
| 3 | Test data: the products topic has no `id` field, so the key field must be named. | Pipeline option `key_field` (test used `product_id`). |
| 4 | `mesg-kafka-connector feature not enabled on Aerospike Server`. The Aerospike Kafka connector only works against Aerospike Enterprise Edition with a feature key that enables `mesg-kafka-connector`. The community image your plan names does not have it. | Not fixable in the platform. Needs `aerospike/aerospike-server-enterprise` with a feature-key file; Aerospike offers a free 60-day Enterprise trial from its download page (requires a sign-up). Not run. |

---

## Couchbase Source (`tpl_couchbase_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `couchbase/kafka-connect-couchbase` 4.3.7 |
| Backend | `couchbase:community` container (UI at localhost:18091, Administrator / dataconnect). Bucket `sample` with 5 employee documents `emp::1`..`emp::5`, user `dataconnect` |
| Pipelines | `t1-couchbase_source-employees` (v1, records dropped, see issue 2) and `t1-couchbase_source-employees-v2` (final) |
| Output topics | `org_1001-raw-t1-couchbasesource-employees-batch-v1` (not created, nothing delivered) and `-v2` (5 records) |
| Evidence | Connector and task RUNNING; v2 topic holds the 5 documents with the document id as the Kafka key, e.g. `emp::1` / `{"dept":"Sales","id":1,"name":"Ann"}` |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy would be blocked: seed said plugin version 4.3.8, installed is 4.3.7. | Set `connector_version` to 4.3.7 for both Couchbase templates in `app/db/seed_templates.py`. |
| 2 | v1 showed RUNNING but produced nothing. The worker log showed the DCP stream reading the documents, then `KEY_CONVERTER ... ByteArrayConverter` failing on every record: the key is the document id (a String) and `ByteArrayConverter` rejects Strings. Under `errors.tolerance=all` the records are skipped silently. | The builder now uses `StringConverter` for the key and keeps `ByteArrayConverter` for the value (raw JSON bytes) (`connect_config_t1.py`). |
| 3 | The `couchbase:community` container starts uninitialised: no cluster, bucket or user, so the connector had nothing to stream from. | Test-environment setup, not a platform change: `couchbase-cli cluster-init`, then buckets `sample` and `sinkbucket` with `--storage-backend couchstore --bucket-replica 0` (Magma is Enterprise-only, replicas need more nodes), then user `dataconnect` with the `admin` role. |
| 4 | Host port 8091 is used by UCS Gravitino. | Published the Couchbase UI on 18091 and the query service on 18093. Connectors use the container ports over the Docker network. |

---

## Couchbase Sink (`tpl_couchbase_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `couchbase/kafka-connect-couchbase` 4.3.7 |
| Backend | Same Couchbase container; writes into bucket `sinkbucket` (a different bucket from the source, otherwise the source would stream the sink's own writes back in a loop) |
| Pipelines | `t1-couchbase_sink-employees` (v1, failed: 5 records in its DLQ) and `t1-couchbase_sink-employees-v2` (final) |
| Input topic | `org_1001-raw-t1-couchbasesource-employees-batch-v2` (the Couchbase Source output) |
| Evidence | Connector and task RUNNING; 5 documents in `sinkbucket` (Ann, Bob, Cara, Dev, Eve); v2 DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Same seed version mismatch as the source. | Fixed together with the source. |
| 2 | All records failed with "Converting byte[] to Kafka Connect data failed due to serialization error". The sink used the JSON converter for the key, but the key is a plain document id such as `emp::1`. | The builder now uses `StringConverter` for the sink key; the value stays JSON (`connect_config_t1.py`). |

---

## ClickHouse Sink (`tpl_clickhouse_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `clickhouse/clickhouse-kafka-connect` v1.5.0 |
| Backend | `clickhouse/clickhouse-server` (latest, 26.9) container, HTTP 8123 (play UI at localhost:8123/play), database `sample`, user `dataconnect` |
| Pipelines | `t1-clickhouse_sink-products` (v1) and `-v2` (both failed at startup, see issue 2); `t1-clickhouse_sink-products-v3` (final) |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; 58 rows in `sample.products`; v3 DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy would be blocked: seed said plugin version 1.7.0, installed is 1.5.0. | Set `connector_version` to 1.5.0 in `app/db/seed_templates.py`. |
| 2 | The task started and then failed with "Connection to ClickHouse is not active" (earlier runs saw the connector with no working task). The ping succeeded, but the next `SELECT VERSION()` failed with `Magic is not correct - expect [-126] but got [-112]`: the plugin's client only decodes LZ4 blocks and ClickHouse 26.x defaults `network_compression_method` to ZSTD. The plugin's `clickhouseSettings` option does not reach this startup check, so it cannot be fixed from the connector config. | Backend setting: a profile file `users.d/compression.xml` in the ClickHouse container sets `network_compression_method` to `LZ4`. (An older ClickHouse image tag would also work.) |
| 3 | The sink does not create tables. | Test setup: created `sample.products` with columns matching the record schema (Int64, Float64, Nullable types, Date32 for `launch_date`, DateTime64(3) for the timestamps). The pipeline's entity (`products`) is the table name. |

---

## HDFS 2 Source (`tpl_hdfs2_source`)

**Result:** PASS (on a Confluent Platform 7.6 Connect worker; see issue 3)

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-hdfs2-source` 3.1.9 (latest on Confluent Hub; the installed 3.1.6 is the same problem) |
| Backend | `apache/hadoop:3` namenode + datanode (amd64, emulated), UI at localhost:9870. Test file `/topics/employees/partition=0/employees+0+0000000000+0000000004.json` (5 JSON lines) |
| Worker | `connect-file` container: `confluentinc/cp-kafka-connect:7.6.0`, the platform's `connect-file-pool` (backend started with `DATACONNECT_CONNECT_POOLS={"connect-file-pool":"http://connect-file:8083"}`) |
| Pipelines | `t1-hdfs2_source-employees`, `-v2`, `-v3` (failed attempts on the 7.9 worker), `-v4` (worked but wrote to the wrong topic, see issue 4), `-v5` (final) |
| Output topic | `org_1001-raw-t1-hdfs2source-employees-batch-v5` (5 records, e.g. `{"name":"Ann","id":1,"dept":"Sales"}`) |
| Evidence | Connector and task RUNNING on the file pool; all 5 HDFS records on the pipeline topic |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The old builder used a format class this connector does not have (`...hdfs2.source.format.json.JsonFormat`). | The source's classes live under `...hdfs2.format.<type>.*`: use `io.confluent.connect.hdfs2.format.json.JsonFormat` (read from the plugin jar). |
| 2 | Deploy failed with a NullPointerException in `validateStoreUrl`. Its config validation reads `store.url`, while the HDFS client reads `hdfs.url`. | The builder sends both keys for HDFS 2. |
| 3 | The connector started but never created a task (`Failed to reconfigure connector's tasks`: `NoSuchMethodError: org.apache.kafka.common.utils.Utils.join(Collection, String)`). Both 3.1.6 and 3.1.9 call a Kafka method removed in Kafka 3.9, which the running Confluent Platform 7.9 worker has. Separately, 3.1.9 needed the licensing jars (`NoClassDefFoundError io/confluent/license/InvalidLicenseException`). | Run the connector on the platform's `connect-file-pool` as a Confluent Platform 7.6.0 worker (the base image of the repo's `kafka-connect.Dockerfile`) with only the HDFS source plugins. Licensing jars copied from the syslog plugin (the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`)). Seed version stays 3.1.9 (it matches Confluent Hub's latest). |
| 4 | v4 ran but wrote the records to a topic named `employees`, taken from the stored file name (`<topic>+<partition>+...`), not to the pipeline topic; `topic.regex.list` does not set the output topic. | The builder routes the output to the pipeline's `topic_name` with a RegexRouter transform, the same as the platform does for JDBC sources. |
| 5 | A new Connect worker was OOM-killed because the Docker VM was out of memory (8 GB total). | Temporarily stopped the Neo4j, ClickHouse and Couchbase containers (data kept). Give Docker Desktop more memory, or run fewer backends at once. |

---

## HDFS 3 Source (`tpl_hdfs3_source`)

**Result:** PASS (on the same CP 7.6 `connect-file` worker)

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-hdfs3-source` 3.1.9, with the licensing jars added |
| Backend | Same Hadoop containers and test file as HDFS 2 Source |
| Pipelines | `t1-hdfs3_source-employees` (v1, failed on the URL setting) and `-v2` (final) |
| Output topic | `org_1001-raw-t1-hdfs3source-employees-batch-v2` (5 records) |
| Evidence | Connector and task RUNNING; all 5 records on the pipeline topic |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Same root causes as HDFS 2 Source (Kafka 3.9 `Utils.join`, licensing jars, output topic routing). | Same fixes: CP 7.6 file-pool worker, the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`), RegexRouter in the builder. |
| 2 | v1 failed with `Only store.url or hdfs.url must be provided`. HDFS 3 rejects the pair that HDFS 2 requires. | The builder sends `hdfs.url` only for HDFS 3 (`store.url` + `hdfs.url` for HDFS 2). |

---

## Cassandra Sink (`tpl_cassandra_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-cassandra` 2.0.19 |
| Backend | `cassandra:4.1` container (4.1.12), single node, datacenter `datacenter1` (`docker exec -it cassandra cqlsh`) |
| Pipelines | `t1-cassandra_sink-products` (v1) and `-v2` (failed, see issues 3 and 4); `t1-cassandra_sink-products-v3` (final) |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; keyspace `sample` and table `sample.products` created by the connector (replication factor 1); 58 rows; v3 DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy would be blocked: seed said plugin version 2.0.20, installed is 2.0.19. | Set `connector_version` to 2.0.19 in `app/db/seed_templates.py`. |
| 2 | On a single-node cluster the connector's default keyspace replication factor of 3 makes every write fail. | Already in the builder: `cassandra.keyspace.replication.factor=1`. Confirmed the keyspace is created with `SimpleStrategy` and replication factor 1. |
| 3 | v1 failed with "Record with a null key was encountered". The table's primary key is built from the record key, and the JDBC-sourced products topic has null keys. | New pipeline option `key_field` (test used `product_id`): the builder adds a ValueToKey transform that copies it into the key. |
| 4 | v2 failed with "Invalid CQL form [org_1001-raw-...]: needs double quotes". The connector names the table after the topic, and a pipeline topic is not a valid CQL identifier. The plugin has no table-name setting. | The builder adds a RegexRouter transform that renames the topic to the pipeline's entity (`products`), so the table is `sample.products`. |

Note: unlike ClickHouse, this sink creates its own keyspace and table (`cassandra.keyspace.create.enabled` and `cassandra.table.manage.enabled`, both on by default), so no table setup was needed.

---

## Scylla CDC Source (`tpl_scylla_cdc_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `scylladb/scylla-cdc-source-connector` 2.0.6 |
| Backend | `scylladb/scylla` container (3.0.8, `--smp 1 --memory 750M --developer-mode 1`), keyspace `sample`, table `sample.customers` with `cdc = {'enabled': true}` (`docker exec -it scylla cqlsh`) |
| Pipeline | `t1-scylla_cdc_source-customers-cdc` (connector `org_1001--t1--scyllacdcsource--customers--cdc--v1`) |
| Output topic | `org_1001-raw-t1-scyllacdcsource-customers-cdc-v1` |
| Evidence | Connector and task RUNNING. The 3 existing rows arrived as creates (`op: c`); after the connector started, a live insert (`op: c`), update (`op: u`, tier gold to platinum) and delete (`op: d`, plus a tombstone) each arrived as a Debezium-style event. 7 records. |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The test keyspace could not be created. The earlier diagnosis (Scylla accepts `cqlsh` before DDL, so the seed needed retries) was wrong. The real error is `SimpleStrategy doesn't support tablet replication`: recent Scylla creates keyspaces with tablets by default, which reject `SimpleStrategy` and do not support CDC. | Create the keyspace with `NetworkTopologyStrategy` and `AND tablets = {'enabled': false}`. Updated in the wave's `t1-seed/scylla/seed.cql`. Retrying is not needed. |
| 2 | Pipeline creation was rejected with "This connector template does not support mode 'batch' (supports ['cdc', 'snapshot'])". | Test setup only: create the pipeline with mode `cdc`. |

---

## File Chunk Source (`tpl_filechunk_source`)

**Result:** PASS after a retest (superseded: this first run was NOT VERIFIED, see "File Chunk Source retest" at the end of this file for the real cause and the fix)

| | |
|---|---|
| Plugin | `streamsendio/file-chunk-source` 2.9 |
| Backend | None (files on the shared folder `/tmp/file-connector`, host folder `infra/file-connector-data`) |
| Pipelines | `t1-filechunk_source-chunks` (v1), `-v2`, `-v3` (v3 runs alone in its own folder `chunk-src3`) |
| Evidence | Connector and task RUNNING; a 5 MB `report.bin` is split into 6 chunk files (5 x 900,000 + 620,000 = 5,120,000 bytes) and renamed `report.bin__FINISHED`. The output topic stays empty (0 records). |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy returned a 500 because the backend still routed `connect-file-pool` to the stopped HDFS test worker. | Reset the backend to its default routing (no `DATACONNECT_CONNECT_POOLS`). |
| 2 | Deploy was blocked by "Connection verified: Path not found on server". The backend checks a file connection's path from its own container, so the file must be in the folder shared by the backend and the Connect worker. | Put test files under `/tmp/file-connector/...` (host `infra/file-connector-data`), not in the worker's private filesystem. |
| 3 | The plugin's default chunk size (2,048,000 bytes) is above Kafka's default 1 MB message limit, so a file larger than that can never be produced. | The builder now sets `binary.chunk.size.bytes` to 900000 (pipeline option `chunk_size_bytes` overrides it). |
| 4 | Two pipelines watching the same folder raced for the same file and each other's chunk files (`NoSuchFileException ... .CHUNK.PROCESSING`). | One pipeline per folder. The platform keeps restarting a failed pipeline's connector, so a failed pipeline on the same folder keeps interfering. |
| 5 | Recreating a connector with the same name fails with `Error registering metrics MBean` (known plugin quirk: the metrics MBean is not unregistered). | Restart the Connect worker, or use a new pipeline name. |
| 6 | The worker logs `Decryption error: Input length must be multiple of 16...` hundreds of times a second. It comes from the optional `license.key` check. The vendor's docs say there is no licence restriction for single-task use, so this is noise (but it floods the log and hides other messages). | None needed. |
| 7 | **Unresolved:** with all of the above in place the chunks are written next to the input file but nothing is produced to Kafka, and no error is logged that I could read through the log flood. Tried without effect: creating the topic first, a 900 KB chunk size, a single connector in its own folder, producer overrides (`compression.type=none`, `acks=all`, `max.in.flight=1`). | Next step if wanted: run the vendor's own Docker uploader, or read the plugin's debug log on a worker without the other test connectors. |

---

## FTPS Source (`tpl_ftps_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-ftps` 1.0.14 (preview) |
| Backend | `delfer/alpine-ftp-server` (vsftpd) with explicit FTPS (forced TLS), self-signed certificate, user `dataconnect`, home `/ftp/dataconnect` containing `employees.csv` (3 rows). Not `atmoz/sftp`: that is an SSH/SFTP server and cannot speak FTP over TLS |
| Pipelines | `t1-ftps_source-employees` (v1), `-v2`, `-v3` (config rejected or NullPointerException, see issues), `-v4` and `-v5` (hung listing `/`; connectors paused), `-v6` (final) |
| Output topic | `org_1001-raw-t1-ftpssource-employees-batch-v6` (3 records, e.g. `{"id":"1","name":"Ann","dept":"Sales"}`) |
| Evidence | Connector and task RUNNING; the 3 CSV rows are on the topic |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | `ftps.security.mode` is case-sensitive (`explicit` is rejected). | Already in the builder: sent in uppercase (`EXPLICIT`). |
| 2 | The planned backend (`atmoz/sftp`) is an SSH/SFTP server; the connector had nothing it could speak to. | Use a real FTP server with TLS: `delfer/alpine-ftp-server` with `TLS_CERT` / `TLS_KEY`. |
| 3 | Deploy failed with a NullPointerException inside the plugin's config validation. The plugin builds its SSL context from files: it needs `ftps.ssl.truststore.location` (the PEM option is ignored), and also a client key store (`ftps.ssl.keystore.location`) plus the passwords for the trust store, key store and key. | Create a JKS trust store (server certificate) and a client key store in the worker (`keytool`), and pass their locations as `ftps.*` pipeline options. The builder now passes through any `ftps.*` pipeline option. |
| 4 | The platform rejects passwords in a pipeline's config. | The password for the stores comes from the connection's encrypted `secret` field; the builder applies it to the trust store, key store and key passwords. |
| 5 | vsftpd requires TLS session reuse on the data channel by default, which Java FTPS clients do not do (`No SSL session reuse on data channel`). | Test-server setting: `require_ssl_reuse=NO` in `vsftpd.conf`. |
| 6 | The task hung forever reading the listing. This vsftpd is not chrooted, so the connection path `/` is the container's whole filesystem (`/proc`, `/sys`, ...). | Set the connection's path to the user's home, `/ftp/dataconnect`. Use the real input folder, never `/`. |

---

## FTPS Sink (`tpl_ftps_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-ftps` 1.0.14 (preview) |
| Backend | Same FTPS server, output folder `/ftp/dataconnect/out` |
| Pipeline | `t1-ftps_sink-employees` |
| Input topic | `org_1001-raw-t1-ftpssource-employees-batch-v6` (the FTPS Source output, 3 records) |
| Evidence | Connector and task RUNNING; 3 JSON files written under `/ftp/dataconnect/out/<topic>/partition=3/` (`...+3+0000000000.json` etc.); DLQ empty |

### Issues and fixes

Same TLS settings as the source (issues 3 to 6 above). The sink's working directory comes from the connection's path, so it must also be a real folder (`/ftp/dataconnect/out`).

---

## FTPS Source and Sink: re-run on `ftp` connections

The first FTPS pipelines (v1 to v6 source, v1 sink) used `jdbc`-type connections, so the Connections page showed 0 under `ftp`. Both were re-run on connections of the proper `ftp` type (`t1-ftp-source` with path `/ftp/dataconnect`, `t1-ftp-sink` with path `/ftp/dataconnect/out`). The `ftp` type needs only host, port, username, password and remote path; the trust-store password goes in the connection's `secret` field as before.

| Template | Pipeline | Result |
|---|---|---|
| FTPS Source | `t1-ftps_source-employees-v8` | PASS: 3 CSV rows on `org_1001-raw-t1-ftpssource-employees-batch-v8` |
| FTPS Sink | `t1-ftps_sink-employees-v3` (reads the v8 topic) | PASS: 3 JSON files under `/ftp/dataconnect/out/org_1001-raw-t1-ftpssource-employees-batch-v8/` |

Extra fix found on the way: the builder's default input pattern (`.*`) overrode a pipeline's own `ftps.input.file.pattern`. With the sink's `out/` folder inside the input folder, `.*` matched the folder and every poll failed (`Error during processing`), so v7 produced nothing. The builder now lets the pipeline's `ftps.input.file.pattern` win; the test used `.*\.csv`.

---

# Core wave re-test on your existing connections

Run on the connections you already had (`postgres` = your `sql_practice` database, `mysql_customers`, `mongo_devices`, all through `host.docker.internal`) plus the Redis container of the core stack. Sinks write only NEW objects (a table `t1_products_copy`, a collection `t1_devices_copy`, Redis keys); nothing existing was changed. Pipelines are named `t1-<template>-<entity>`.

| Template | Pipeline | Result | Evidence |
|---|---|---|---|
| Debezium PostgreSQL CDC | `t1-debezium_postgres-products-cdc` | PASS (snapshot) | 58 events on `org_1001-raw-t1-debeziumpostgres-products-cdc-v1`. Only the snapshot was checked; a live insert/update/delete was not made in your database. |
| JDBC Sink (Postgres) | `t1-jdbc_sink-t1_products_copy` | PASS | 58 rows in `sql_practice.t1_products_copy`; DLQ empty |
| Redis Sink | `t1-redis_sink-products` | PASS | 58 keys (`product_id` -> record JSON) in the core Redis |
| MongoDB Source | `t1-mongo_source-devices` | PASS | 79 documents on `org_1001-raw-t1-mongosource-devices-batch-v1` |
| MongoDB Sink | `t1-mongo_sink-t1_devices_copy-v2` (v1 failed) | PASS | 79 documents in `smart_home_db.t1_devices_copy`; DLQ empty |
| Debezium MongoDB CDC | `t1-debezium_mongodb-devices-cdc` | PASS (snapshot) | 79 events on `org_1001-raw-t1-debeziummongodb-devices-cdc-v1` |
| JDBC Source (MySQL) | `t1-jdbc_source-customers` | PASS | 50 rows on `org_1001-raw-t1-jdbcsource-customers-batch-v1` |
| Debezium MySQL CDC | `t1-debezium_mysql-customers-cdc` | BLOCKED (database privileges) | see below |

Also created: `t1-jdbc_source-products` (a keyed JDBC source on your Postgres, topic `org_1001-raw-t1-jdbcsource-products-batch-v1`) as the Redis sink's input.

### Issues and fixes

| # | Where | Issue | Fix |
|---|---|---|---|
| 1 | Redis Sink | Seed said 0.0.13; installed is 0.0.12. | Seed set to 0.0.12. |
| 2 | Redis Sink | The deploy check rejected the plugin: it reports its version as `0.0.0.0` (the jar carries no version), which counted as older than the pin. | `deploy_preflight.py` now skips the version comparison when the installed version is all zeros (test added). |
| 3 | Redis Sink | Redis rejects records with a null key, and a JDBC source emits none. | Ported the `key_field` option to the repo's JDBC source (ValueToKey + string key), as in your earlier harness. Use it on the source pipeline that feeds a Redis sink. |
| 4 | Redis Sink | The `ftps`-style connection type requires a password, and AUTH against a password-less Redis is an error. | Redis builder option `no_auth: true` leaves the password out. |
| 5 | MongoDB Source / Sink | The templates are qualified against MongoDB connector 3.1.0 (Confluent Hub's latest), but 3.0.1 was installed, so deploy was rejected. | Installed 3.1.0 on the worker and updated the pin in `infra/kafka-connect.Dockerfile`. |
| 6 | MongoDB Sink | All records failed with `unknown operator: $oid`: the Mongo source writes MongoDB extended JSON, and the sink parsed it with the JSON converter into a plain map. | A Mongo sink reading a Mongo-sourced topic now uses the string converter, so the connector parses extended JSON itself (`connect_config.py`). |
| 7 | Debezium MongoDB CDC | Deploy was rejected at the schema check with `SOURCE_AUTH_FAILED`: the connectors authenticate against `authSource=admin`, but the discovery code used the database name. | Both use `auth_source` from the connection if set, else `admin` (`connect_config.py`, `connection_discovery.py`). |
| 8 | Debezium MySQL CDC | The task starts but never snapshots: `Access denied; you need (at least one of) the RELOAD or FLUSH_TABLES privilege(s)`. The `dataconnect` MySQL user lacks the privileges Debezium needs. | Not changed (it is a grant on your own database). Needs `GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT, LOCK TABLES ON *.* TO 'dataconnect'@'%'; FLUSH PRIVILEGES;` run as a MySQL admin. The connector is paused until then. |

Note: DECIMAL columns from the JDBC source appear in the schema-enveloped JSON as base64 bytes (for example `account_balance` shows `E5nf`), which is how Kafka Connect represents its Decimal type.

---

## Update: Debezium MySQL CDC (`tpl_debezium_mysql`) is now PASS

| | |
|---|---|
| Plugin | `debezium-connector-mysql` **3.0.8.Final** (the image had 2.5.4.Final) |
| Backend | Your MySQL `dataconnect` database (MySQL 8.4.11 via `host.docker.internal`), table `customers` |
| Pipelines | `t1-debezium_mysql-customers-cdc` (v1) and `-v2` (on the updated template) |
| Evidence | Connector and task RUNNING; 50 snapshot events on `org_1001-raw-t1-debeziummysql-customers-cdc-v1` and `-v2`. A live insert/update/delete was not made in your database. |

### Issues and fixes (replaces issue 8 above)

| # | Issue | Fix |
|---|---|---|
| 1 | The task could not snapshot: `Access denied; you need ... RELOAD or FLUSH_TABLES`. | You granted `SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT, LOCK TABLES` to `dataconnect`. That was enough. |
| 2 | Next error: `You have an error in your SQL syntax ... near 'MASTER STATUS'`. MySQL 8.4 removed `SHOW MASTER STATUS`, and every Debezium 2.x release still runs it (checked in the jar of 2.7.4 as well). The template lists MySQL 8.4 as supported, but Debezium only supports it from 3.0. | Installed Debezium MySQL 3.0.8.Final. The seed pin for `tpl_debezium_mysql` is now 3.0.8.Final (the deploy check requires the same major version as the pin), and `infra/kafka-connect.Dockerfile` installs the MySQL connector from a separate `DBZ_MYSQL=3.0.8.Final` (the other Debezium connectors stay on 2.5.4.Final). |

Note: the pre-update v1 pipeline was created against the old 2.5.4 template revision, so the deploy check reports a major-version difference for it on later reconcile ticks; the connector itself keeps running. Use the v2 pipeline.

---

# Analytics wave (QuestDB, InfluxDB)

## QuestDB Sink (`tpl_questdb_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `questdb/kafka-questdb-connector` 0.12 |
| Backend | `questdb/questdb` container; console at localhost:9020, line protocol on 9009 inside the Docker network |
| Pipeline | `t1-questdb_sink-products` |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; table `products` created by the connector with 58 rows (`select count() from products`); DLQ empty |

No issues: the builder already used the line-protocol port (9009), and QuestDB creates the table itself.

---

## InfluxDB Sink (`tpl_influxdb_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-influxdb` (sink reports its version as `unknown`; the deploy check skips it) |
| Backend | `influxdb:1.8` container, database `sample`, auth off, HTTP on localhost:8086 (1.8 on purpose: the connector predates the 2.x API) |
| Pipelines | `t1-influxdb_sink-products` (v1, failed), `-v2` (6 points), `t1-influxdb_sink-readings-v3` (final) |
| Input topic | `org_1001-raw-t1-influx-in-batch-v1` (sensor readings: `{"measurement":"readings","tags":{"sensor":"s1","room":"kitchen"},"temperature":21.5,"humidity":40}`) |
| Evidence | Connector and task RUNNING; 8 points in measurement `readings` with their tags (`SELECT * FROM readings`) |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | With the products topic the task failed with `unable to parse 'products ... created_at=Thu Sep 17 03:35:39 UTC 2026 ...': invalid boolean`. The sink writes Connect Date/Timestamp values unquoted with `Date.toString()`, which is invalid line protocol. | New pipeline option `timestamp_fields`: those fields are converted to strings with Kafka's TimestampConverter SMT before the sink (`connect_config_t1.py`). |
| 2 | With that fixed, 58 products produced only 6 points. InfluxDB identifies a point by measurement + tags + timestamp, and the sink takes tags from a `tags` field of the record; products rows have none and many share a timestamp, so they overwrite each other. | The sink needs time-series style records. New option `schemaless: true` (plain JSON with `measurement` and `tags`); tested with sensor readings. Two readings of the same sensor in the same millisecond still collapse to one point (normal InfluxDB behaviour); readings a second apart are all kept. |

---

## InfluxDB Source (`tpl_influxdb_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-influxdb` 1.2.12 (source half is deprecated upstream) |
| Backend | Same InfluxDB container, measurement `readings` |
| Pipeline | `t1-influxdb_source-readings` |
| Output topic | `org_1001-raw-t1-influxdbsource-readings-batch-v1` |
| Evidence | Connector and task RUNNING; 9 records for the 8 points, e.g. `{'measurement': 'readings', 'tags': {'room': 'bedroom', 'sensor': 's2'}, 'time': '2026-10-05T08:33:41.159Z', 'humidity': 45.0, 'temperature': 19.2}`. One point (the newest) is delivered twice, at the edge of the source's polling window; delivery is at-least-once. |

No issues; the builder's settings were accepted as they are.

---

# Messaging wave (ActiveMQ Sink, JMS Source)

## ActiveMQ Sink (`tpl_activemq_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-activemq-sink` **2.2.3** (Confluent Hub's latest; the image had 2.2.2) |
| Backend | `apache/activemq-classic:6.1.2`, console at localhost:8161 (admin/admin), queue `dataconnect.amq.q` |
| Pipeline | `t1-activemq_sink-products` |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; all 58 messages in the queue (`EnqueueCount` 58); DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The seed pins 2.2.3 (Hub's latest) but 2.2.2 was installed, so the deploy check would reject it. | Installed 2.2.3 on the worker (same direction as the HDFS source fix; the seed was left as it is). |
| 2 | The new build, like others, ships `connect-licensing-extensions` without `confluent-licensing` (`NoClassDefFoundError io/confluent/license/InvalidLicenseException` at start). | Copied the licensing jars from the syslog plugin into the plugin's `lib/` (the same step as in the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`)). |

---

## JMS Source (`tpl_jms_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-jms` **13.0.12** (Hub's latest; the image had 13.0.11). It already bundles the licensing jars and the ActiveMQ client. |
| Backend | Same ActiveMQ broker; reads queue `dataconnect.amq.q`, the queue the ActiveMQ Sink filled, so the test is a full loop (Kafka to ActiveMQ to Kafka) |
| Pipeline | `t1-jms_source-products-stream` (mode `stream`) |
| Output topic | `org_1001-raw-t1-jmssource-products-stream-v1` |
| Evidence | Connector and task RUNNING; 58 records on the topic; the queue shows 58 dequeued and 0 left |

### Issues and fixes

The seed pin (13.0.12) was the same reason as above; installing 13.0.12 fixed it. No other issues.

---

# HDFS sinks (HDFS 2 Sink, HDFS 3 Sink)

Backend for both: `apache/hadoop:3` namenode + datanode (amd64, emulated), NameNode UI at localhost:9870 (`docker exec namenode hdfs dfs -ls -R /`). Runs on the main Connect worker (sinks need no pool worker). Input topic for both: `org_1001-raw-dash-postgres-products-batch-v1` (58 records). Files are written under `/org_1001/t1/<hdfs2sink|hdfs3sink>/<topic>/partition=0/<topic>+<partition>+<startOffset>+<endOffset>.json`; each record is the JSON the topic holds, one per line.

## HDFS 2 Sink (`tpl_hdfs2_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-hdfs` 10.2.17 (matches the seed and Hub's latest) |
| Pipeline | `t1-hdfs2_sink-products`, `flush_size` 20 |
| Evidence | Connector and task RUNNING; two committed files of 20 records each (offsets 0-19 and 20-39). The last 18 records are buffered until the next flush (a sink commits a file only every `flush.size` records, so 58 is not a multiple of 20); DLQ empty |

## HDFS 3 Sink (`tpl_hdfs3_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-hdfs3` **3.0.22** (Hub's latest; the image had 3.0.19) |
| Pipeline | `t1-hdfs3_sink-products`, `flush_size` 29 |
| Evidence | Connector and task RUNNING; two files (offsets 0-28 and 29-57) holding all 58 records |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The HDFS 3 seed pin (3.0.22, Hub's latest) was newer than the installed 3.0.19, so the deploy check would reject it. | Installed 3.0.22 (seed unchanged). |
| 2 | The new build ships `connect-licensing-extensions` without `confluent-licensing`. | Copied the licensing jars from the syslog plugin (same step as the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`)). |
| 3 | After the Hadoop containers were stopped and started again, the datanode exited with `Incompatible clusterIDs`: the namenode re-formats its storage on start, so the old datanode no longer matches. | Test-environment note: recreate the datanode container whenever the namenode has been restarted (HDFS data is not kept). Not a platform issue. |

---

## Elasticsearch Sink (`tpl_elasticsearch_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-elasticsearch` 15.1.3 (seed pin 15.1.2, newer is accepted) |
| Backend | Docker `docker.elastic.co/elasticsearch/elasticsearch:8.10.2` (image already on the machine), single node, security off, published on host port 9200, so the existing `elasticsearch_employee` connection (`host.docker.internal:9200`) reaches it unchanged. Not your native Elasticsearch (see issue 1) |
| Pipeline | `t1-elasticsearch_sink-t1_products_copy` |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; index `org_1001-raw-dash-postgres-products-batch-v1` holds 58 documents (`curl localhost:9200/<index>/_count`); DLQ empty |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy was blocked: "Connection verified ... Cannot reach host.docker.internal:9200". Nothing was listening: your native Homebrew Elasticsearch (`elasticsearch-full` 7.17.4) is in an error state. | Not touched (a host service). Ran the Docker 8.10.2 image on port 9200 instead. |
| 2 | The platform keeps the result of the last connection test and blocks deploys while it says failed. | After the backend was up, ran the connection's "Test connection" again (`POST /connections/{id}/test-connection`), then deployed. |
| 3 | The index is named after the topic (`org_1001-raw-dash-postgres-products-batch-v1`), not after the pipeline's entity. | Not a bug: the Elasticsearch sink's default. (The platform's sink builder does not map topic to index.) |

---

# Vector and wide-column sinks (DataStax, Weaviate)

## DataStax Cassandra Sink (`tpl_datastax_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `datastax/kafka-connect-cassandra-sink` 1.4.0 |
| Backend | `cassandra:4.1` container (`docker exec -it cassandra cqlsh`), keyspace `sample` |
| Pipeline | `t1-datastax_sink-products_ds` |
| Input topic | `org_1001-raw-dash-postgres-products-batch-v1` (58 records) |
| Evidence | Connector and task RUNNING; 58 rows in `sample.products_ds`; DLQ empty |

Setup notes (not defects): this connector creates neither tables nor a default mapping. The table `sample.products_ds` was created by hand, and the pipeline needs a `mapping` option of `column=value.field` pairs (the builder refuses to deploy without one and says so).

---

## Weaviate Sink (`tpl_weaviate_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `Weaviate/kafka-connect-weaviate` 0.1.2 |
| Backend | `semitechnologies/weaviate:1.25.4`, anonymous access, REST on localhost:8087 (gRPC 50051 inside the Docker network), collection `Articles` (no vectorizer) |
| Pipelines | `t1-weaviate_sink-employees` (v1, v2, v3: nothing written), `t1-weaviate_sink-articles` v1 and v2 (nothing written), `-v3` (final) |
| Input topic | `org_1001-raw-t1-weaviate-in-batch-v2` (5 article records with an embedded JSON schema, plus one extra trace record) |
| Evidence | Connector and task RUNNING; 6 objects in collection `Articles` with their properties (`curl localhost:8087/v1/objects?class=Articles`) |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The task died with `extract auth: oidc auth is not configured`. The connection has no API key, but the platform shows every unset secret field as the mask `********`, and the builder treated that as a real key (Weaviate switched to API-key auth; Qdrant, Timeplus and Milvus had the same latent problem). | `_real_secret()` in `connect_config_t1.py`: the builder ignores the mask for `api_key` and the Milvus `token`. |
| 2 | Records were consumed with no error, but no object appeared. Weaviate's debug log showed the connector trying to create a class named `Org_1001-raw-t1-weaviate-in-batch-v2:Articles`: `collection.mapping` is a collection name format (it may contain `${topic}`), not a `topic:Class` pair, and Weaviate rejected the name without any error reaching Kafka Connect. | The builder now sends `collection.mapping` as just the capitalised entity (`Articles`). |
| 3 | The connector never creates collections (its README: "Weaviate collections must be created in advance"). `id` is also a reserved property name, so the employee records from the Neo4j topic cannot be mapped as they are. | Created `Articles` (title, body, category, rating) by hand and used article-style records. |
| 4 | Input format. The connector maps properties from the record's value schema, so a `with_schema` option (JSON with the embedded schema envelope) was added and used. Not proven necessary: the final fix (issue 2) was tested only with schema-enveloped records. | Option kept in `connect_config_t1.py`. |

---

## YugabyteDB Source (`tpl_yugabyte_source`)

**Result:** PASS

| | |
|---|---|
| Plugin | `yugabyte/yugabytedb-source-connector` `dz.2.5.2.yb.2025.1.2` (a Debezium PostgreSQL connector for YugabyteDB, logical replication with the `yboutput` plugin) |
| Backend | `yugabytedb/yugabyte:2025.1.2.0-b110` (the server version this connector was built for), single node, YSQL on localhost:5433 (`docker exec -it yugabyte bin/ysqlsh -h yugabyte`), table `employees` |
| Pipelines | `t1-yugabyte_source-employees-cdc` (v1, v2: failed on the `latest` server, see issue 1), `t1-yugabyte_source-yb_employees-cdc-v3` (final) |
| Output topic | `org_1001-raw-t1-yugabytesource-yb_employees-cdc-v3` |
| Evidence | Connector and task RUNNING; 3 snapshot events (`op: r`), then live changes arrived as an insert (`c`, id 4), an update (`u`, id 2 dept Engineering to Platform) and a delete (`d`, id 3), plus a tombstone: 7 records |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | On the `latest` image (YugabyteDB 2026.1) the snapshot failed with `procedure set_yb_read_time(unknown) does not exist`, and streaming with `pg_current_wal_lsn() is not yet supported`. The connector build targets YugabyteDB 2025.1; the 2026.1 server no longer matches. | Run the matching server image `yugabytedb/yugabyte:2025.1.2.0-b110` (arm64). Use the same major/minor server as the connector build. |
| 2 | Connect validation then failed with `No node available in primary cluster to connect to`. The connector's smart JDBC driver reads the node list from `yb_servers()`, which showed the container ID hostname, unusable for the driver. | Start the node advertising a resolvable name: `bin/yugabyted start --advertise_address=yugabyte` with container hostname `yugabyte`. |
| 3 | Host port 7000 (the YugabyteDB master UI) is used by macOS AirPlay on this Mac. | Published only 5433 (YSQL). |
| 4 | Two pipelines for the same entity share the replication slot name `dbz_<entity>`, so v1 and v2 competed with each other. | v3 uses a different entity (`yb_employees`) and therefore its own slot. v1 and v2 are paused. |
| 5 | In an update event only the changed columns carry values (`name` shows null); a delete event carries only the key. That is YugabyteDB's default replica identity, not a platform issue. | Not changed. |

---

# No-backend wave (Datagen, SpoolDir, Syslog, SNMP)

All four use plugin versions equal to the seed pins and to Confluent Hub's latest (Datagen 0.7.2, SpoolDir 2.0.71, Syslog 1.5.17, SNMP 1.3.6). A "no backend" connector still needs a connection that passes the platform's reachability check, so the test connections point at an endpoint that always answers (the Kafka broker `kafka:29092`, or the Connect worker `kafka-connect:8083`); the real settings go in the pipeline's connector config.

## Datagen Source (`tpl_datagen_source`)

**Result:** PASS

| | |
|---|---|
| Pipeline | `t1-datagen_source-users-v2` (v1 stayed a draft: its connection pointed at host `none`) |
| Output topic | `org_1001-raw-t1-datagensource-users-stream-v2` |
| Evidence | Connector and task RUNNING; user records such as `{'registertime': 1502606134243, 'userid': 'User_9', 'regionid': 'Region_4', 'gender': 'FEMALE'}` (14 within the first minute, one about every 5 seconds with `max_interval` 5000) |

Notes: the template supports only mode `stream`. The connector generates data forever; with its plugin removed from the worker it stops producing. I did not set `iterations`, because a run that finishes surfaces as a FAILED task. An accidental empty topic `org_1001-raw-t1-datagensource-users-batch-v1` (from a console consumer on a name that did not exist yet) was removed.

## SpoolDir Source (`tpl_spooldir_source`)

**Result:** PASS

| | |
|---|---|
| Pipeline | `t1-spooldir_source-employees` (mode `batch` only) |
| Setup | Folder `/tmp/file-connector/spool-in` (the folder shared by the backend and the worker) with `finished/` and `error/` subfolders and `employees.csv` (5 rows) |
| Output topic | `org_1001-raw-t1-spooldirsource-employees-batch-v1` |
| Evidence | Connector and task RUNNING; 5 records (e.g. `{'employee_id': '2', 'first_name': 'Bob', 'department': 'Engineering', 'ctc': '72000'}`); the file was moved to `spool-in/finished/employees.csv` |

Notes: the builder moves processed files to `<path>/finished` and failures to `<path>/error`, so both folders must exist. Values arrive as strings (the connector's schema generation does not infer types). The plugin reports its version as `0.0.0.0`; the deploy check skips it (fix made earlier for Redis).

## Syslog Source (`tpl_syslog_source`)

**Result:** PASS

| | |
|---|---|
| Pipeline | `t1-syslog_source-syslog-stream` (UDP listener on 5140 inside the worker) |
| Output topic | `org_1001-raw-t1-syslogsource-syslog-stream-v1` |
| Evidence | Connector and task RUNNING; 3 messages sent over UDP arrived parsed, e.g. host `host1`, message `Failed password for root from 10.0.0.5`, facility 4, level 2 |

### Issue and fix

| # | Issue | Fix |
|---|---|---|
| 1 | A listener has no remote host, but the platform's connection check needs a reachable host and port, and the builder used the connection's port as the listen port. | New pipeline option `listen_port` (`connect_config_t1.py`): the connection can point at any reachable endpoint while the connector listens on `listen_port`. |

## SNMP Trap Source (`tpl_snmp_source`)

**Result:** PASS

| | |
|---|---|
| Pipeline | `t1-snmp_source-traps-stream` (UDP listener on 10161 via `listen_port`) |
| Output topic | `org_1001-raw-t1-snmpsource-traps-stream-v1` |
| Evidence | Connector and task RUNNING; 3 SNMPv2c traps (community `public`, enterprise OID `1.3.6.1.4.1.8072.2.3`) arrived with sysUpTime and their variables (`...2.3.2.1` = 42, 77, 5 and `...2.3.2.2` = warning, critical, info) |

Test setup note: the worker has no SNMP or UDP tool, and bash's `printf > /dev/udp/...` splits a packet into several datagrams (the SNMP library then rejects them as malformed BER). The traps were hand-built and sent as single datagrams with a small Java program run inside the worker. Same `listen_port` fix as Syslog.

## Local File Sink (`tpl_file_sink`)

**Result:** PASS

| | |
|---|---|
| Pipeline | `t1-file_sink-users` (connection `t1-local-file-sink`, type `file`, path `/tmp/file-connector/sink-out/users.txt`) |
| Input topic | `org_1001-raw-t1-datagensource-users-stream-v2` (the 20 Datagen records) |
| Evidence | Connector and task RUNNING; the 20 distinct records are in `infra/file-connector-data/sink-out/users.txt` (one JSON record per line); consumer group lag 0 on all partitions |

### Issues

| # | Issue | Fix / note |
|---|---|---|
| 1 | The first deploy was rejected: "Path not found on server". The connection test checks that the target path already exists, and a sink's output file does not exist yet. | Created the (empty) file first. The folder is the one shared by the backend and the worker, so both see it. |
| 2 | The file holds 40 lines: each of the 20 records appears twice. The task was restarted by a worker rebalance (caused by other connectors being added) before it committed its offsets, so it re-read the topic from the start. | Not fixed. The file sink is at-least-once and does not de-duplicate. The topic itself still holds exactly 20 records. |

## File Chunk Sink (`tpl_filechunk_sink`)

**Result:** PASS for the sink itself. It was fed hand-built chunk messages, because the File Chunk Source still does not publish to Kafka (see that entry).

| | |
|---|---|
| Plugin | `streamsendio/file-chunk-sink` 2.9 (no licence key needed in single-task mode) |
| Pipeline | `t1-filechunk_sink-chunks-v2` (connection `t1-filechunk-sink`, type `file`, path `/tmp/file-connector/chunk-out/`); `-v1` is the first attempt (task FAILED, see issue 2) |
| Input topic | `org_1001-raw-t1-chunkfeed-report-batch-v2` (2 chunk messages: 900,000 + 124,000 bytes) |
| Evidence | Connector and task RUNNING; `infra/file-connector-data/chunk-out/merged/report.bin` is 1,024,000 bytes with MD5 `6af239d22f02ddc97e13cf9becce3470`, identical to the original file |

### How the test input was made

The chunks were not produced by the source plugin. A small producer script published the two chunks of a 1 MB test file with the key and Kafka headers the sink reads: key = chunk file name (`00001_1_P01_2_report.bin_3_00001_4_00002_5_<md5>_6_<size>_7_<ts>_8_.CHUNK`), value = chunk bytes, headers `file.name`, `uploader.chunk.size.bytes`, `file.relative.path`, `produce.task.name`, `produce.hostname`, `chunk.file.last.modified`, `uploader.partitions`, `uploader.version`, `uploader.multitask`. The header names were read out of the plugin's classes.

### Issues

| # | Issue | Fix / note |
|---|---|---|
| 1 | Topic name rejected: a sink may only read `org_1001-<zone>-<domain>-<source_system>-<entity>-<mode>-<version>`. | Used `org_1001-raw-t1-chunkfeed-report-batch-v2`. (An empty stray topic `org_1001-raw-t1-filechunksink-input-v1` from the first wrong name is left in Kafka.) |
| 2 | The `-v1` task died with `StringIndexOutOfBoundsException` in `parseUploadedChunkFile`: the `file.name` header must hold the full chunk file name, not the original file name. | Corrected the header and ran a new pipeline (`-v2`) on a new topic. `-v1` and its topic stay as they are. |
| 3 | The sink creates `builds/`, `locked/` and `merged/` under `files.dir`; the finished file is in `merged/`. | Expected behaviour, not an error. |

### Source follow-up (File Chunk Source stays NOT VERIFIED)

Re-tested on a quiet worker with standalone debug connectors (since removed): `topic.partitions=1`, the vendor sample licence key (this needs `tasks.max=2`, which is the licensed multi-task mode), and a 3 KB file in 1,000-byte chunks. In every case the chunk files were written and the input renamed `__FINISHED`, but the topic stayed empty. The licence is therefore not the cause (the plugin only uses it for multi-task mode). The worker log is flooded with `Decryption error: Input length must be multiple of 16` and shows nothing else for the connector. Side effect: when the source plugin was reloaded, its cleanup deleted the old `.CHUNK` files in `chunk-src3`; the original `report.bin__FINISHED` files are intact.

## File Chunk Source and File Chunk Sink: re-check and root cause

**File Chunk Sink: PASS (re-confirmed).** A second, different file (2,500,000 random bytes, 3 chunks of 900,000 / 900,000 / 700,000) was published to `org_1001-raw-t1-chunkfeed-report-batch-v2`. The sink (pipeline `-v2`, connector and task RUNNING) rebuilt `chunk-out/merged/report2.bin` with MD5 `160fb8afaa54321c632f7bab3aa11db0`, identical to the original. The earlier `report.bin` (1 MB) also matches.

**File Chunk Source: FAIL at the time. The licence explanation below was WRONG; it is corrected in "File Chunk Source retest" at the end of this file.**

| Evidence | Result |
|---|---|
| Source with TRACE logging on (`/admin/loggers/com.github.streamsend`) | Every poll logs `read() - DEBUG polling for next file`, then `Ignoring exception while calling read() inside poll()`; the exception is swallowed, so poll returns nothing and the worker has no record to send. |
| Count over a 20 s window (3 pipeline connectors plus debug ones) | 40,120 `Decryption error: Input length must be multiple of 16...`, 40,116 `Ignoring exception while calling read()`, 40,116 `polling for next file` (1 : 1 : 1). Each `read()` call hits the licence decryption error and then throws. |
| Plugin internals | `LicenseValidator` decrypts `license.key` with AES/ECB using a hard-coded key and expects `<id>\|<expiry date>`. `read()`, the task and the file queue all call `isValidLicense`. With no key set, the decryption throws. |
| The only key available | The sample key in the plugin's own `etc/uploader.json` decrypts to an expiry date of **2025-06-30**, so it is expired (and it also switches on multi-task mode, which then needs `tasks.max=2`). With it, chunks were still written and nothing produced. |
| Ruled out | Bind mount: same behaviour on the container's own disk (`/tmp/chunk-local`). Topic partitions (`topic.partitions=1`), chunk size (1,000 B to 900 KB), a quiet single connector, producer settings, Kafka message size. |

**Exact reason:** the source's `read()` fails the plugin's licence check on every poll (`Decryption error: Input length must be multiple of 16`), so no record is ever returned to Kafka Connect. The chunk files are cut and renamed before that check, which is why the folder looks like a success while the topic stays empty. The vendor says single-task use needs no licence, but this 2.9 build behaves as if one is required.

**To fix:** get a valid, unexpired `license.key` from StreamSend (the builder can pass it as a pipeline option) or the vendor's current build. A key was not forged or worked around.

Housekeeping: debug connectors `dbg-chunk-src*`, the debug topic `dbg-chunk-test` and the `chunk-dbg*` folders are test leftovers; the platform pipelines, topics and `chunk-out/` are the kept proof.

## Prometheus Metrics Sink (`tpl_prometheus_sink`)

**Result:** PASS

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-prometheus-metrics` 2.0.18 (Hub latest; commercial, 30-day trial licence) |
| Backend | None: the connector serves the scrape endpoint itself, inside the worker, on port 8889 (not mapped to the host) |
| Pipeline | `t1-prometheus_sink-cpu` (mode `stream`; connection `t1-prometheus` points at `kafka-connect:8083` only to pass the reachability check; the listen port comes from the pipeline option `listen_port`) |
| Input topic | `org_1001-raw-t1-metricsfeed-cpu-stream-v1` (6 messages: 3 rejected malformed ones, then 3 correct ones) |
| Evidence | Connector and task RUNNING; `curl localhost:8889/metrics` inside the worker returns `# TYPE dataconnect_cpu_usage gauge` with `dataconnect_cpu_usage{host="web1",env="t1"} 0.42`, `web2 0.77`, `web3 0.15` |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | Deploy blocked: installed 2.0.17, template pinned 2.0.18. | Installed Hub's 2.0.18 (old copy kept in the stash). |
| 2 | 2.0.18 died at start with `NoClassDefFoundError: io/confluent/license/InvalidLicenseException` (it ships `connect-licensing-extensions` but not `confluent-licensing`). | Copied the licensing jars from the syslog plugin, the same fix the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`) applies to other plugins. |
| 3 | The connection check needs a reachable host:port, but the listener's port is not open until the connector runs. | New pipeline option `listen_port` (`connect_config_t1.py`), same idea as Syslog/SNMP. |
| 4 | Metrics did not appear: `Values key cannot be null` and "dimensions field is missing". The plugin needs `values` and `dimensions` as **structs** (not maps) and the label field is named `dimensions`, not `labels`. | Published records in the right shape: `{name, type:"gauge", timestamp, dimensions:{host,env}, values:{value}}` with a JSON schema. The 3 malformed messages stay in the topic and were skipped by the plugin (`behavior.on.error=log`). |

Note: Prometheus itself is not part of this stack; the endpoint is only reachable inside the worker. To scrape it from a Prometheus container, map port 8889 on `kafka-connect` and add the target.

## Data Diode Sink (`tpl_datadiode_sink`)

**Result:** PASS. The encrypted UDP datagrams arrive, and the full round trip with Data Diode Source (next entry) delivers the records decrypted.

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-data-diode` 1.2.9 (commercial, 30-day trial licence; ships its licensing jars) |
| Backend | A small Python UDP listener in the backend container (`/tmp/udp_listen2.py`, port 3456; log `/tmp/udp2.log`) standing in for the receiving side |
| Pipeline | `t1-datadiode_sink-events` (mode `stream`; connection `t1-datadiode-sink-b`, host `infra-backend-1`, password and salt in the connection profile) |
| Input topic | `org_1001-raw-t1-diodefeed-events-stream-v1` (JSON event lines, 14 so far) |
| Evidence | Connector and task RUNNING; each batch of records leaves as one UDP datagram from the worker (172.18.0.7) to port 3456. The datagram starts with the plugin's header `noop-compressor.aes256_cbc_pkcs5padding` followed by AES-256 ciphertext; size grows with the batch (2 records 234 bytes, 3 records 330, 4 records 410). The payload is not readable, as intended. |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The platform's connection check needs a reachable TCP host:port, but the diode uses UDP and builds its target port from the connection's port. | New pipeline option `diode_port` (`connect_config_t1.py`): the connection can point at any reachable host and port (here the backend's port 8000, the first try with 8001 was refused inside the Docker network), while the sink sends to `diode_port`. |
| 2 | The same batch of 3 records was sent twice (11:17:21 and 11:17:49): the task restarted before committing offsets and re-read the topic. | Not fixed; the diode is at-least-once and a receiver must tolerate duplicates. |
| 3 | The sink never shows a committed offset in `kafka-consumer-groups` (CURRENT-OFFSET `-`). | Behaviour of the connector, not an error; records are still delivered. |

The round trip was proven later with pipelines `t1-datadiode_sink-events-v2` and `-v3` (see Data Diode Source).

## Data Diode Source (`tpl_datadiode_source`)

**Result:** PASS (round trip with Data Diode Sink)

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-data-diode` 1.2.9 (same plugin as the sink) |
| Pipelines | `t1-datadiode_source-events-v2` (final; UDP 3457) and `-v1` (UDP 3456, first run), paired with sinks `t1-datadiode_sink-events-v3` (to 3457) and `-v2` (to 3456); both sides use the same password and salt from the connection profile |
| Output topic | `org_1001-raw-t1-datadiodesource-events-stream-v2` |
| Evidence | Both connectors RUNNING; the 14 JSON event records written to `org_1001-raw-t1-diodefeed-events-stream-v1` were encrypted by the sink, sent over UDP to the worker, decrypted by the source and appear on the output topic (e.g. `{"event":"login","user":"u1"}`) |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The source writes to `<kafka.topic.prefix><original topic name>` with nothing between, giving `org_1001-raw-t1-datadiodesource-events-stream-v1org_1001-raw-t1-diodefeed-events-stream-v1`, which breaks the topic naming rule (must stay UCS-compatible). | The builder now adds a `RegexRouter` (`.*` to the pipeline topic), as for the HDFS sources. The `-v1` run's oddly named topic stays in Kafka as it was created before the fix. |
| 2 | A second source on the same worker failed with `Cannot open DatagramSocket ... 0.0.0.0:3456` (port already held by `-v1`). | Each diode source needs its own UDP port: set pipeline option `diode_port` (`-v2` uses 3457) and redeploy (the pipeline was updated with PATCH and redeployed). |
| 3 | Same connection-check limitation as the sink (UDP). | `diode_port` option; the connection can point at any reachable host and port. |

## S3 Sink and S3 Source (`tpl_s3_sink`, `tpl_s3_source`) on MinIO

**Result:** PASS (both, tested as a pair against MinIO instead of AWS)

| | |
|---|---|
| Plugins | `confluentinc/kafka-connect-s3` 12.1.10 and `kafka-connect-s3-source` 3.1.6 (already on the worker; Hub's latest are 12.1.13 and 3.1.9) |
| Backend | MinIO (`bitnamilegacy/minio`, container `t1-minio`, on the Docker network at `t1-minio:9000`; host ports 9100 for the API and 9101 for the web console, login `dataconnect` / `dataconnect-secret`), bucket `dataconnect-t1`. Docker Hub's `minio/minio` is no longer pullable and quay.io returned 401, so the Bitnami legacy image was used. |
| Sink pipeline | `t1-s3_sink-events` (connection `t1-minio`, type `s3`, with a new `endpoint` field); reads `org_1001-raw-t1-diodefeed-events-stream-v1` (14 JSON records) |
| Sink evidence | 14 objects under `dataconnect-t1/org_1001/t1/s3sink/org_1001-raw-t1-diodefeed-events-stream-v1/partition=0/...+0+00000000NN.json` (one record per object because the builder sets `flush.size=1`), e.g. `{"event":"login","user":"u1"}` |
| Source pipeline | `t1-s3_source-events-v2` (connection `t1-minio-source`, prefix `org_1001/t1/s3sink/`); `-v1` is the first attempt, which found no files (issue 2) |
| Source output topic | `org_1001-raw-t1-s3source-events-batch-v2`: 14 records, no duplicates, content identical to the sink's input |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | The platform could only talk to AWS S3: the connection check probed `<bucket>.s3.<region>.amazonaws.com:443` and the builder had no way to set a custom endpoint. | New optional connection field `endpoint` (`connection_adapters.py`, `connect_config.py`): the check probes that host and port, and the connectors get `store.url` plus `s3.path.style.access.enabled=true`. Needed for MinIO and any S3-compatible store. |
| 2 | S3 Source `-v1` was RUNNING but found "0 files" and produced nothing. The source scans only under `topics.dir` (default `topics`), while the sink writes under its own prefix. | The builder now sets `topics.dir` from the connection's `prefix`. `-v2` read all 14 objects. |
| 3 | Deploy would be blocked by the version check: the seed pinned 10.5.13 (sink) and 2.5.9 (source), the worker has 12.1.10 and 3.1.6 (different major). | Seed pins changed to 12.1.10 and 3.1.6. Note `infra/kafka-connect.Dockerfile` still installs `s3:10.5.19` and `s3-source:2.6.3`; it should be updated to match if the image is rebuilt. |

## Debezium SQL Server CDC (`tpl_debezium_sqlserver`)

**Result:** PASS

| | |
|---|---|
| Plugin | `debezium-connector-sqlserver` 2.5.4.Final (matches the seed pin; already on the worker) |
| Backend | SQL Server 2022 (`mcr.microsoft.com/mssql/server:2022-latest`, container `t1-mssql`, host port 1433, login `sa` / `Dataconnect#1234`, SQL Server Agent enabled). Database `t1db`, table `dbo.customers` (3 rows), CDC enabled with `sp_cdc_enable_db` and `sp_cdc_enable_table` (capture job `cdc.t1db_capture` started). The image was already on the machine, so nothing was pulled. |
| Pipelines | `t1-debezium_sqlserver-customers` (v1, first run) and `-v2` (final), connection `t1-mssql` (type `sqlserver`) |
| Output topic | `org_1001-raw-t1-debeziumsqlserver-customers-cdc-v2` |
| Evidence | Connector and task RUNNING. v2 topic: 3 snapshot reads (`op=r`), then live changes: insert `c` (id 5 Esha), update `u` (Ann: Pune to Hyderabad, with before and after), delete `d` (id 4) and its tombstone. Total 7 messages. |

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | v1 ran, but its output topic stayed empty; all events went to `org_1001--t1--debeziumsqlserver--customers--cdc--v1.t1db.dbo.customers`. SQL Server table topics have four parts (`<prefix>.<database>.<schema>.<table>`), the others three, and the platform's reroute pattern only matched three. | The builder now uses a four-part pattern for SQL Server (`_DBZ_TABLE_TOPIC_TAIL` in `connect_config.py`). v2 routes correctly. v1 and its unrouted topic stay as they were created. |
| 2 | Slip while making the fix: an f-string with a backslash is a syntax error on Python 3.11, which crash-looped the backend for a few minutes. | Rewrote it without the f-string; the backend restarted normally and the v2 deploy completed. |

Note: SQL Server needs the Agent running and CDC enabled per database and per table. Debezium needs a login that can read the CDC tables (here `sa`).

## LittleHorse Sink (`tpl_littlehorse_sink`)

**Result:** PASS after a LittleHorse-only naming fix (first run was BLOCKED: the platform's connector names contain an underscore, which this plugin rejects).

| | |
|---|---|
| Plugin | `littlehorse/lh-kafka-connect` 0.14.1 (`io.littlehorse.connect.WfRunSinkConnector`) |
| Backend | LittleHorse server (`ghcr.io/littlehorse-enterprises/littlehorse/lh-server`, 1.12 GB on disk, container `t1-lh`, gRPC port 2023 on the Docker network and host), running on the stack's own Kafka (`LHS_CLUSTER_ID=t1-lh`; it created its own `t1-lh-*` topics there). A workflow `t1-events` (variables `event` and `user`, both public) was registered with the LittleHorse Python client. |
| Platform pipelines | `t1-littlehorse_sink-events` (v1) and `-v2`: both stay `failed`, deploy error "The source did not respond within the configured timeout" |
| Input topic | `org_1001-raw-t1-lhfeed-events-stream-v1` (3 keyed JSON records: key `"run-N"`, value `{"event":...,"user":...}`) |
| Direct test | Connector `t1-littlehorse-direct` (same config, hyphen-only name, created straight on Kafka Connect), RUNNING; LittleHorse holds 3 WfRuns of `t1-events`, all COMPLETED, with variables from the records (`login/u1`, `purchase/u2`, `logout/u3`), ids like `t1-littlehorse-direct-org-1001-raw-t1-lhfeed-events-stream-v1-0-0` |

### Exact reason for the platform failure

The plugin's validator (`LHSinkConnector.validate`) rejects any connector name that is not lowercase letters, digits and hyphens: `Invalid value org_1001--t1--littlehorsesink--events--stream--v1 for configuration name: Connector name only supports lowercase alphanumeric characters and hyphens`. The platform names every connector `<org id>--<domain>--...` and the org id is `org_1001`, so it always contains an underscore. The plugin throws an uncaught exception from the REST call, Kafka Connect then holds the request until the backend's 15-second read timeout, and the backend reports that as "The source did not respond within the configured timeout", which is misleading.

### Other findings

| # | Finding |
|---|---|
| 1 | Connection types `jdbc` and `ftp` both worked for the connection check (reachability only). |
| 2 | WfRun ids are built from the connector name, topic, partition and offset, not from the record key. |
| 3 | The platform pipelines `-v1`/`-v2`, the direct connector and the LittleHorse WfRuns are left as proof. |

### Options to unblock

1. Let a template declare a connector-name style, and for LittleHorse build the name with `-` in place of `_` (for example `org-1001--t1--...`). This touches the connector-name format that the runtime parses (`topic_naming.parse_connector_name`), so it needs your decision.
2. Ask LittleHorse to relax the name check.

### Fix and re-test (LittleHorse-only connector name)

`topic_naming.connector_name_for_class()` (used by `pipeline_config.generate_pipeline_config`) now writes `_` as `-` in the connector name for the LittleHorse classes only (`HYPHEN_ONLY_CONNECTOR_CLASSES`); every other connector keeps the standard `org_1001--...` name and the naming format is unchanged. Unit test: `tests/test_connector_name_override.py`.

| | |
|---|---|
| Pipeline | `t1-littlehorse_sink-events-v3` (connection `t1-littlehorse`) |
| Connector | `org-1001--t1--littlehorsesink--events--stream--v3`, connector and task RUNNING |
| Evidence | LittleHorse holds three more WfRuns of `t1-events`, all COMPLETED, ids `org-1001--t1--littlehorsesink--events--stream--v3-org-1001-raw-t1-lhfeed-events-stream-v1-0-N`, variables `login/u1`, `purchase/u2`, `logout/u3` |

`-v1` and `-v2` (underscore names) stay `failed` as proof of the original problem. Pipelines created before this change keep whatever connector name they were given.

## Milvus Sink (`tpl_milvus_sink`)

**Result:** PASS (the earlier FAIL was an environment limit). The pipeline shows `running` and all 3 vectors are in Milvus; deploys were slow because of a reconciler backlog, see issues 2 and 3.

| | |
|---|---|
| Plugin | `zilliz/kafka-connect-milvus` 1.0.1 (REST based, Milvus HTTP endpoint) |
| Backend | Full stack on the Docker network after stopping the containers this test did not need (UCS stack, Mongo, MySQL, Schema Registry, LittleHorse): `milvusdb/milvus:v2.4.15` standalone (`t1-milvus`, ports 19530 and 9091), `quay.io/coreos/etcd:v3.5.18` (`t1-etcd`), and the existing MinIO container `t1-minio` as Milvus's object store. About 2 GB RAM in total. |
| Setup | Collection `docvectors` created with the Python client: `id` INT64 primary key (no auto id), `vector` FLOAT_VECTOR(4), `title` VARCHAR(100), AUTOINDEX/COSINE index. The sink does not create collections. |
| Pipeline | `t1-milvus_sink-docvectors` (connection `t1-milvus`, host `t1-milvus`, port 19530, `root` / `Milvus`); the collection name is the pipeline entity |
| Input topic | `org_1001-raw-t1-vecfeed-docvectors-stream-v1` (3 JSON records `{"id":1,"vector":[0.1,0.2,0.3,0.4],"title":"alpha"}` ...) |
| Evidence | Connector and task RUNNING, consumer lag 0. Querying the collection returns all 3 entities with their vectors (`alpha`, `beta`, `gamma`; floats stored as float32). |

### Issues

| # | Issue | Fix / note |
|---|---|---|
| 1 | Nothing in the template or builder needed changing: `public.endpoint`, `token`, `collection.name` and the JSON converters were right. | none |
| 2 | Every platform deploy returned "The source did not respond within the configured timeout" although the connector did get created and ran. The real cause is not Milvus: the runtime reconciler self-heals every FAILED connector with `POST /connectors/<name>/restart` each tick, and about 45 test connectors are deliberately FAILED because their plugins are stashed. Each restart holds Kafka Connect's REST thread for about 47 s (the backend gives up after 15 s, but Connect still works through the backlog), so any other call, including a deploy, times out. | Added `DATACONNECT_RUNTIME_RECONCILE_INTERVAL_SECONDS` to `infra/docker-compose.yml` (variable `RECONCILE_INTERVAL_SECONDS`, default 15 as before) and ran the backend with 1800. The backlog still has to drain (or the worker be restarted); a reconciler that skipped restarts for connectors whose plugin is not installed would remove the problem, but that is a code change not made here. |
| 3 | After those failed deploys the platform showed the pipeline as `failed` while Connect reported it RUNNING. | The reconciler corrected it by itself after its backlog drained: the pipeline now shows `running`. |
| 4 | Recreating the backend container (for issue 2) removed the Python packages pip-installed into it for tests (`pymilvus`, `littlehorse-client`); reinstall them if those scripts are needed again. | none |

## HBase Sink (`tpl_hbase_sink`)

**Result:** PASS (the earlier FAIL was blamed on the environment, but the real causes were configuration)

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-hbase` 2.0.45 (Hub latest; matches the seed pin). The 2.0.44 copy that was installed is kept in the stash. |
| Backend | `harisekhon/hbase` (HBase 2.1, amd64 image, run under emulation on Apple Silicon, 766 MB on disk) as container `t1-hbase` with `--hostname t1-hbase` on the Docker network, ZooKeeper on 2181 (host port 2182), master UI on host port 16010 |
| Pipeline | `t1-hbase_sink-customers-v4` (final; v1 to v3 were failed attempts), connection `t1-hbase` (host `t1-hbase`, port 2181) |
| Input topic | `org_1001-raw-t1-hbasefeed-customers-stream-v2` (3 keyed JSON records with schema) |
| Evidence | Connector and task RUNNING. `scan 'customers'` in the HBase shell shows 3 rows, `row-1` to `row-3`, with `info:id`, `info:name`, `info:city` (e.g. `row-1 info:name = Ann`, `info:city = Pune`; `id` is stored as an 8-byte long) |

### Why it failed before, and what fixed it

| # | Issue | Fix |
|---|---|---|
| 1 | "ZooKeeper advertises an address the connector cannot reach" (the old note). The container's default hostname is a random id, which ZooKeeper registers. | Started the container with `--hostname t1-hbase` on the shared network; ZooKeeper now registers `t1-hbase,16020,...` and the Connect worker reaches ports 2181, 16000 and 16020. |
| 2 | The connector showed RUNNING but never created a task and logged nothing. The plugin's `zookeeper.znode.parent` defaults to `/hbase-secure` (Kerberos layout) while an unsecured HBase registers under `/hbase`, so the master is never found. | The builder now sets `zookeeper.znode.parent` to `/hbase` (pipeline option `znode_parent` overrides). |
| 3 | The task failed with "Error with creating column family with name org_1001-raw-...-v1" for flat records. The plugin puts top-level primitive fields in a column family named after the topic, and HBase family names allow only letters, digits and underscores, so the platform's hyphenated topic names are rejected. The real server error is hidden because the plugin lacks the `htrace` class on the client side (`NoClassDefFoundError org/apache/htrace/core/Tracer$Builder`). | Not fixable in the builder: the plugin has no setting for the family name (a guessed `column.family.format` was tried and removed). Records must be nested, one struct per column family, e.g. `{"info": {"id":1,"name":"Ann","city":"Pune"}}`, which gives family `info` with qualifiers `id`, `name`, `city`. |
| 4 | The 2.0.45 install shipped without `confluent-licensing`. | Copied the licensing jars from the syslog plugin (the the plugin-fixes script (`docs/CONNECTOR_TEST_SETUP.md`) approach). |
| 5 | Deploys timed out after each backend or worker restart while Connect worked through its restart backlog (see the Milvus entry). | Waited; no code change. |

Limits: needs nested records (issue 3), so a flat table such as the JDBC-sourced products topic would need a transform to wrap the fields in a struct first. Row key comes from the Kafka record key (a null key is not usable).

## SingleStore Sink (`tpl_singlestore_sink`)

**Result:** PASS after a retest (superseded: this entry claimed a licence key was needed, which was wrong; see "SingleStore retest" at the end of this file)

| | |
|---|---|
| Plugin | `singlestore/singlestore-kafka-connector` 1.2.12 (stashed, not installed for this test) |
| Backend options | `ghcr.io/singlestore-labs/singlestoredb-dev` (about 1.2 GB compressed, amd64 only) or `singlestore/cluster-in-a-box` (about 1.1 GB compressed, amd64) |
| Why blocked | 1. Both images refuse to start without `SINGLESTORE_LICENSE` (a free key from the SingleStore portal; the account has to be created by the user). 2. They are amd64 images running under emulation on this Apple Silicon host, and start a full database engine; the Docker VM has 7.7 GB in total. |
| Builder | Already present (`connect_config_t1.py`, `connection.ddlEndpoint`, `connection.database`, `connection.user/password`, `singlestore.tableName.<topic>`); not exercised. |

To unblock: obtain a free licence key, then run the dev image with it (`SINGLESTORE_LICENSE`, port 3306), create the target database, and deploy the sink as for the other SQL sinks.

## File Chunk Source retest (corrects the earlier licence diagnosis) and File Chunk pair

**Result:** PASS. File Chunk Source and File Chunk Sink now run as a real pair through the platform and a 2.5 MB file comes out identical (MD5 `0105775bc88908c5e4d798b9e500b7b1` in and out).

The Marketplace page (version 2.9, modified Apr 22 2025) is the version tested, and a fresh download from Hub has the same jar (same MD5), so the plugin file was never the issue. The vendor README says: "There is no license restriction for single-task usage". The licence was **not** the cause.

| | |
|---|---|
| Pipelines | source `t1-filechunk_source-chunks-v7` (topic policy `single-partition`, 1 partition) and sink `t1-filechunk_sink-chunks-v6`; earlier `-v1` to `-v6` runs are the failed attempts listed below |
| Source output topic | `org_1001-raw-t1-filechunksource-chunks-batch-v7` (1 partition, 3 chunk messages: 900,000 + 900,000 + 700,000 bytes) |
| Evidence | Source and sink RUNNING; `infra/file-connector-data/chunk-out/merged/989488e1e501/report8.bin` is 2,500,000 bytes with MD5 equal to the original `chunk-src8/report8.bin__FINISHED` |

### How the real causes were found

The 2.9 plugin was run in Kafka Connect standalone mode (as the vendor documents) from a snapshot of the worker image, with `-Xlog:exceptions` and TRACE logging, and its bytecode was read. Findings:
- The `NullPointerException` in `read()` and the "Decryption error" lines are idle-poll noise: `read()` logs `file.getName()` before checking that a file exists, and the licence check fails on the default key `none`. They are thrown when no file is waiting and are harmless.
- With a valid-format key (the expired sample key) the same silence occurred, so the licence is not what stops publishing.
- After cutting a file into `.CHUNK` files the connector's file queue never looked for them again, because that search is only done when `input.path.walk.recursively=true` (default false).

### Issues and fixes (all in the builder, `connect_config.py`)

| # | Issue | Fix |
|---|---|---|
| 1 | Chunks written but never published (the earlier mystery). The queue only searches for finished chunk files when `input.path.walk.recursively` is `true`; the vendor's sample config sets it, the default does not. | Builder sets `input.path.walk.recursively=true`. This alone made the standalone test publish all chunks. |
| 2 | The message key is the raw partition key (bytes). With `key.converter=StringConverter` it became the text `[B@69a3b876`, so chunks landed on random partitions. | Source and sink both use `ByteArrayConverter` for key and value (`_BYTES_PASSTHROUGH`). |
| 3 | Single-task mode (no licence key) supports only a one-partition topic: with more, the sink logs "unexpected multi-partition use while in Single-task mode", keeps only part of the chunks and the file never reassembles (tests `-v4` to `-v6` stalled at 900,000 or 1,800,000 bytes of 2,500,000). The platform creates topics with 6 partitions by default. | Builder sets `topic.partitions=1` (pipeline option `topic_partitions` raises it for a licensed multi-task setup), and the source pipeline must use a topic policy with `partitions=1` (policy `single-partition` was created; `topic_policy_id` on the pipeline). |
| 4 | The sink writes under `merged/<produce.hostname>/` (here `989488e1e501`) because the source sets `file.relative.path`. | Expected behaviour, not an error. |

Notes: the first sink runs (`-v1` to `-v3` on the earlier test topics) and their half-built files under `chunk-out/builds` are left as proof of the failures. The source leaves `<file>__FINISHED` in the input folder and removes its `.CHUNK` files. The multi-partition and multi-task modes were not tested (they need a licence key).

## Timeplus retest 1 (superseded by "Timeplus retest 2" below)

**Result:** BLOCKED. The right backend now runs, but the plugin needs an API key that can only be created after signing up for a Timeplus trial licence in the web console.

| | |
|---|---|
| Plugin | `timeplus/kafka-timeplus-connector-sink` 1.0.5 (the Marketplace page shows the same version, modified Oct 10 2024) |
| Backend | `timeplus/timeplus-enterprise:latest` (Timeplus Enterprise 3.4.1, native arm64, 648 MB compressed, 2.89 GB on disk), container `t1-timeplus`, web service on port 8000, native TCP 8463, REST 3218. It has the `/{workspace}/api/v1beta2/streams/{stream}/ingest` API (workspace `local`) that the plugin calls and the free `timeplusd` image lacked. Container stopped, not deleted. |
| What worked | The REST API answers with basic auth (`proton` / `timeplus@t+`): `GET /local/api/v1beta2/streams` returns 200, and the user list shows `proton` and `local_api_user`. The builder already supports a connection `url`, `workspace` and `api_key`, so it would be `http://t1-timeplus:8000`, workspace `local`. |
| Why blocked | A fresh install redirects every page to an onboarding screen. Issuing the free 30-day trial licence (`POST /local/api/v1beta2/licenses/trial`) asks for a user email and registers it with Timeplus, and the plugin authenticates with an API key that is created in the web console under Profile, API Keys. Neither could be done without the user's account, so no records were sent. |

To unblock: open http://localhost:8000, complete onboarding, create an API key, then deploy the sink with that key.

## Final status of the three blocked templates

| Template | Blocker |
|---|---|
| Timeplus Sink | Was listed here; now PASS through an auth proxy, see "Timeplus retest 2" |
| Aerospike Sink | Was listed here; now PASS on Aerospike Enterprise with its built-in evaluation key, see "Aerospike retest" |
| SingleStore Sink | Was listed here; now PASS, no licence key needed, see "SingleStore retest" |

## Timeplus retest 2 (PASS through an API-key-to-basic-auth proxy)

**Result:** PASS for the connector's data path (stream creation and ingest). The plugin cannot log in to self-managed Timeplus Enterprise 3.4.1 directly; it needs Timeplus Cloud or the proxy used here.

| | |
|---|---|
| Plugin | `timeplus/kafka-timeplus-connector-sink` 1.0.5 (same as the Marketplace page: version 1.0.5, modified Oct 10 2024) |
| Backend | Timeplus Enterprise 3.4.1 (`t1-timeplus`, console http://localhost:8000, trial licence issued from the console on 2026-10-06, valid 30 days, user `Lithish`; the `proton` admin / `timeplus@t+` also works). Workspace id `default`. |
| Test helper | `t1-timeplus-proxy`, a small Python proxy (port 8010 on the Docker network) that accepts the plugin's `X-API-KEY` header (test key `t1-test-api-key-0123456789`), swaps it for basic auth, and removes the trailing slash from paths. Not part of the platform. |
| Pipeline | `t1-timeplus_sink-events3-v3` (final), connection `t1-timeplus-proxy` (`url` `http://t1-timeplus-proxy:8010`, `workspace` `default`, `api_key` the test key); `-v2` was the first run |
| Input topic | `org_1001-raw-t1-tpfeed-events3-stream-v1` (5 JSON records `{"id":1,"user":"u1","action":"act1"}` ...) |
| Evidence | Connector and task RUNNING. The plugin listed streams, inferred the schema, created stream `events3` (HTTP 201) and ingested (200). `SELECT id,user,action FROM table(events3)` returns all 5 rows, columns `action` string, `id` float64 (numbers are inferred as float64), `user` string. |

### Why it could not run directly, and the exact findings

| # | Finding |
|---|---|
| 1 | Timeplus Enterprise 3.x authenticates its REST API with basic auth only. The plugin sends only an `X-API-KEY` header (the "API key for the workspace, 60 chars long" in its README, an API key as issued by Timeplus Cloud). Calling the ingest API with any `X-API-KEY` returns 401, and the console has no API Keys screen: the user menu offers only "Switch to dark" and "Logout" (the key screen exists in the console's code but is not shown in this edition). So no valid key can be created here. |
| 2 | The plugin creates a stream with `POST /{workspace}/api/v1beta2/streams/` (trailing slash). Enterprise answers 307, the plugin does not follow redirects, and the stream is never created. The test proxy strips the slash. Timeplus Cloud accepts the slash. |
| 3 | When the stream does not exist the ingest call returns 404, but the plugin treats the records as written: records sent before the stream existed (ids 1 to 4 on `events`) were silently lost. Another reason to check the stream before relying on it. |
| 4 | Earlier builder fixes still apply (REST address, string converters); the connection now carries `url`, `workspace` and `api_key`. |

For a real run, point the connection at Timeplus Cloud (`https://us.timeplus.cloud`, a workspace id and an API key from its console); no change to the platform is needed.

## Aerospike retest (PASS on Aerospike Enterprise, no account or key needed)

**Result:** PASS. The earlier block ("`mesg-kafka-connector` feature not enabled") came from the community image. Aerospike Enterprise ships with a default single-node evaluation feature key (since server 6.1), and that key is enough for the connector.

| | |
|---|---|
| Plugin | `aerospike/kafka-connect-aerospike-sink` from Hub 3.2.10 (the Marketplace page shows 3.2.10, modified Sep 3 2026). The 3.2.10 jar still reports its version as 3.2.9, so the seed pin stays 3.2.9 (the platform's version check compares the reported version). |
| Backend | `aerospike/aerospike-server-enterprise` (server 8.2.0.0, native arm64, 145 MB compressed), container `t1-aerospike-ee` (host port 3010), default config: single node, namespace `test`, default feature key `/etc/aerospike/features.conf` |
| Pipeline | `t1-aerospike_sink-items-v5` (final; `-v3` rejected by the version check, `-v4` failed on date fields), connection `t1-aerospike-ee` (host `t1-aerospike-ee`, port 3000, namespace `test`) |
| Input topic | `org_1001-raw-t1-asfeed-items-batch-v1` (5 JSON-with-schema records, key = `item_id`) |
| Evidence | Connector and task RUNNING. Set `test.items` holds 5 records, read back with the Python client: `{'item_id': 1, 'name': 'Keyboard', 'price': 49.5, 'in_stock': 1}` ... `{'item_id': 5, 'name': 'Dock', 'price': 99.0, 'in_stock': 1}` (booleans are stored as 0/1). |

### Issues and findings

| # | Issue | Fix / note |
|---|---|---|
| 1 | Deploy blocked: the Hub 3.2.10 jar reports version 3.2.9, older than the 3.2.10 pin I set while installing it. | Seed pin set back to 3.2.9. |
| 2 | `-v4` (the 58-record products topic) failed with `ClassCastException: java.util.Date cannot be cast to java.lang.Integer` in the record parser: the plugin cannot convert Connect date/timestamp fields. | Use records with plain types (strings, numbers, booleans), or convert dates first (for example with a TimestampConverter transform). Not a platform change. |
| 3 | Everything was slow for a while: the File Chunk connectors, whose plugins I had left installed after their test, spin in a hot loop and flood the worker log. | Stashed the chunk plugins and restarted the worker. Lesson: stash a plugin as soon as its test is done. |
| 4 | The first deploys after each restart timed out (reconciler backlog), as in the Milvus entry. | Waited. |

Notes: the connector's earlier builder fixes (YAML `config` with `topics -> <topic> -> mapping`, `invalid-record` mandatory, `key_field`) were all that was needed; no further builder change. The default evaluation key is for single-node developer use, not production.

## SingleStore retest (PASS, no licence key needed)

**Result:** PASS. The earlier BLOCKED entry was wrong about the licence: the SingleStore dev image runs without `SINGLESTORE_LICENSE` (the vendor README says it creates a cluster "without the need for a license", on machines with up to 8 cores and 64 GB).

| | |
|---|---|
| Plugin | `singlestore/singlestore-kafka-connector` 1.2.12 (Marketplace page: 1.2.12, Apache 2.0; matches the seed pin) |
| Backend | `ghcr.io/singlestore-labs/singlestoredb-dev` (1.2 GB compressed, 3.92 GB on disk, amd64 only, run with `--platform linux/amd64` under emulation on Apple Silicon), container `t1-singlestore`, root password `dataconnect`, host ports 3316 (SQL) and 8085 (Studio UI); about 0.9 GB RAM while idle. It started in a few minutes. Database `sample`, table `sample.parts (id BIGINT PRIMARY KEY, name, qty INT, price DOUBLE)` created by hand. |
| Pipeline | `t1-singlestore_sink-parts-v2`, connection `t1-singlestore` (host `t1-singlestore`, port 3306, database `sample`, user `root`) |
| Input topic | `org_1001-raw-t1-ssfeed-parts-stream-v1` (JSON with schema; 5 records, then 1 more sent while running) |
| Evidence | Connector and task RUNNING. `SELECT * FROM sample.parts` shows 6 rows (`1 Bolt 100 0.25` ... `5 Spring 75 1.2`, and the live record `6 Pulley 12 7.75`). |

### Findings

| # | Finding |
|---|---|
| 1 | No builder change was needed (`connection.ddlEndpoint`, `connection.database`, user and password, `singlestore.tableName.<topic>` = entity). |
| 2 | The sink does not create the table (like ClickHouse); create it first with matching column names. |
| 3 | The server logs a repeating warning "Used cluster capacity (2 units) has exceeded the maximum cluster capacity of your license (1 units)" because the unlicensed single-container cluster runs an aggregator and a leaf; it did not block writes. |
| 4 | The consumer group shows no committed offset (CURRENT-OFFSET `-`) although all rows arrive, as with the Data Diode sink. |
| 5 | The first deploy after the worker restart timed out (reconciler backlog) but the connector was created and ran. |

## LittleHorse: underscore re-check (2026-10-06)

Hub still lists `littlehorse/lh-kafka-connect` 0.14.1 (July 2025) as the latest, the version tested. Validating the same config directly against the worker:

| Connector name | Result |
|---|---|
| `org_1001--t1--littlehorsesink--events--stream--v9` (underscore) | HTTP 500: `Invalid value ... for configuration name: Connector name only supports lowercase alphanumeric characters and hyphens` |
| `org-1001--t1--littlehorsesink--events--stream--v9` (hyphen) | accepted, 0 errors |

So the plugin still rejects underscores and the LittleHorse-only name override (`connector_name_for_class`) is still needed. The deployed pipeline `t1-littlehorse_sink-events-v3` (connector `org-1001--t1--littlehorsesink--events--stream--v3`) is the working one.

## T2: Amazon EventBridge Sink (`tpl_eventbridge_sink`) on LocalStack

**Result:** PASS against LocalStack 3.8.1 (no AWS account). The earlier "Netty async client cannot acquire a connection" failure did not reproduce.

| | |
|---|---|
| Plugin | `aws/kafka-eventbridge-sink` v1.5.1 (Hub latest, June 2025; matches the seed pin; it reports its version as `v1.5.1`) |
| Backend | `localstack/localstack:3.8` (3.8.1, 631 MB compressed, arm64), container `t1-localstack`, port 4566, `SERVICES=events,sqs`. Created by hand: event bus `t1-bus`, SQS queue `t1-events-q`, rule `t1-rule` (pattern `detail-type exists`) on the bus with the queue as target, so the delivered events can be read back. |
| Worker setup | The connector uses the AWS SDK default credential chain. The worker had no AWS credentials, so test ones (`test` / `test`, region `us-east-1`) were written to `~/.aws/credentials` and `~/.aws/config` of the worker's `appuser`. |
| Pipeline | `t1-eventbridge_sink-orders-v2`, connection `t1-localstack` (`endpoint_url` `http://t1-localstack:4566`, `region` `us-east-1`, `event_bus` `t1-bus`) |
| Input topic | `org_1001-raw-t1-ebfeed-orders-stream-v1` |
| Evidence | Connector and task RUNNING; LocalStack logged `events.PutEvents => 200`; the 3 events were read from the SQS target queue with `detail-type` `kafka-connect-org_1001-raw-t1-ebfeed-orders-stream-v1`, `source` `kafka-connect.org_1001--t1--eventbridgesink--orders--stream--v2` and `detail.value` `{'order_id': 4, 'customer': 'Dan', 'amount': 42.0}` (also 5 Eve, 6 Fay), plus topic, partition, offset, timestamp and key in the detail. |

### Findings

| # | Finding |
|---|---|
| 1 | Nothing in the builder had to change: `aws.eventbridge.region`, `...eventbus.arn` (account `000000000000`) and `...endpoint.uri` from the connection's `endpoint_url` work. |
| 2 | Credentials are not set by the template. Without any credentials in the worker the AWS SDK fails; for LocalStack any dummy keys are enough (`~/.aws/credentials`, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the worker). This is the most likely reason for the earlier connection error; the earlier setup was not re-created to prove it. |
| 3 | The first 3 test records had plain-text keys. The platform's JSON key converter expects a JSON-with-schema key (or no key), so they were rejected into the dead-letter topic `org_1001-quarantine-t1-eventbridgesink-orders-connector-v2`. Keyless records were delivered. |
| 4 | Real AWS was not tested (the IAM user in the earlier note has no EventBridge permissions); LocalStack is the test target as planned. |

## T2: GridGain Sink (`tpl_gridgain_sink`) and GridGain Source (`tpl_gridgain_source`)

**Result:** Sink PASS (against Apache Ignite 3.1.0). Source BLOCKED: it needs a real GridGain 9 server, which needs a licence file.

| | |
|---|---|
| Plugin | `gridgain/gridgain-kafka-connect` 9.1.27 (Hub latest; the stashed copy was 9.1.26). It is the GridGain **9** connector: Ignite 3 thin client on port 10800, SQL tables (not the older `gridgain/community` 8.x caches). |
| Backend (sink) | `apacheignite/ignite:3.1.0` (arm64), container `t1-ignite3`, ports 10800 (client) and 10300 (REST). Cluster initialised with `POST /management/v1/cluster/init` (no licence needed). Table `gg_items (id INT PRIMARY KEY, name VARCHAR, qty INT, price DOUBLE)` created with the bundled `ignite3cli`. |
| Sink pipeline | `t1-gridgain_sink-gg_items-v4` (final; `-v2`, `-v3` failed, see issues), connection `t1-ignite3` (host `t1-ignite3`, port 10800), option `key_field` = `id` |
| Input topic | `org_1001-raw-t1-ggfeed-gg_items-stream-v1` (4 JSON-with-schema records) |
| Evidence | Connector and task RUNNING; `SELECT * FROM gg_items ORDER BY id` returns `1 Hammer 10 12.5`, `2 Saw 4 22.0`, `3 Drill 2 89.9`, `4 Level 15 7.25`. |

### Builder (new in `connect_config_t1.py`; the templates had none)

`ignite.addresses` = `host:10800`, `topics` or `ignite.tables`, the table-name regex `^.*$` mapped to the pipeline entity (or the pipeline option `table`), optional basic auth with the pipeline option `auth_enabled`, and for the sink the option `key_field`.

### Issues and fixes

| # | Issue | Fix |
|---|---|---|
| 1 | `gridgain/gridgain9` cannot be initialised without a licence: `POST /cluster/init` returns "License must not be empty". The vendor docs say a `license.json` is mounted for `cluster init`; none is bundled in the image. | Used Apache Ignite 3.1.0 for the sink (standard Ignite 3 APIs, no licence). |
| 2 | v2 failed: `Ignite table 'gg_itemsgg_items' is not found`. The table-name regex `.*` matches the whole name and then the empty string at its end, so the replacement is applied twice. | The builder uses the anchored `^.*$`. |
| 3 | v3: records dropped with `Column not found in GridGain for Kafka field 'key'`. The primary key columns are filled from the record key; a null key fails. | New option `key_field` adds a `ValueToKey` transform (struct key of those fields). |
| 4 | **Source fails** on Apache Ignite: `Continuous query error ... IGN-CLIENT-2 Unexpected operation code: 1001`. The source uses a GridGain-specific continuous-query call that Apache Ignite does not implement. Pipeline `t1-gridgain_source-gg_items-v2` stays failed. | Needs a real GridGain 9 server, so a GridGain licence file (the vendor offers a trial; registration needed). Not run. |

Notes: with `flush.mode=KAFKA` rows appear after the worker's offset flush (up to about a minute). The first deploys after each restart timed out (reconciler backlog) but the connectors were created.

### GridGain: does the connector work with GridGain 8? (checked 2026-10-06)

No. The template's plugin (`gridgain/gridgain-kafka-connect` 9.1.27) is the GridGain **9** connector: its client libraries are the Ignite 3 thin client (`ignite-client-9.1.27`), and the connector classes are listed in the GridGain 9 SDK. Pointed at a GridGain 8 Community server (`gridgain/community`, port 10800, no licence needed), the source task fails at connect time with `IgniteClientConnectionException: IGN-CLIENT-1 Handshake error [endpoint=t1-gg8/...:10800]` (pipeline `t1-gridgain_source-gg_items-v3`). So a GridGain 8 trial licence cannot be used to test this template; the Source still needs a GridGain **9** server and its licence file (the GridGain 8.9.x trial download page only offers 8.x). The 8.x generation has a different, deprecated Confluent connector (`gridgain/ignite-connector`) that is not this template.

## T2: Lightstreamer Sink (`tpl_lightstreamer_sink`)

**Result:** PASS with a custom adapter set on the stock Lightstreamer server (no account or licence needed).

| | |
|---|---|
| Plugin | `lightstreamer/kafka-connect-lightstreamer` 1.2.10 (Hub latest; matches the seed pin) |
| Backend | `lightstreamer:latest` (Lightstreamer Server 7.4.8, 219 MB arm64), container `t1-ls`, web port 8080. Needs the adapter set in the adapters.xml in `docs/CONNECTOR_TEST_SETUP.md` copied to `/lightstreamer/adapters/kafka_connect/adapters.xml` (the stock image has none): the built-in `LiteralBasedProvider` metadata adapter plus a `ROBUST_PROXY_FOR_REMOTE_ADAPTER` data adapter `KafkaConnector` listening on port 6661, which the connector connects to. |
| Pipeline | `t1-lightstreamer_sink-quotes-v5` (final), connection `t1-lightstreamer-web` (host `t1-ls`, port 8080 for the reachability check, `proxy_port` 6661), pipeline options `topic_mappings` `<topic>:quotes` (the item name) and `record_mappings` `id:#{VALUE.id},name:#{VALUE.name},price:#{VALUE.price}` |
| Input topic | `org_1001-raw-t1-lsfeed-quotes-stream-v1` (JSON-with-schema records `{"id":1,"name":"ACME","price":...}`) |
| Evidence | Connector and task RUNNING; the Lightstreamer log shows `Accepted connection (1) on the Request/Reply socket on port 6661`. A small Lightstreamer text-protocol client (session on adapter set `KAFKA_CONNECT`, subscription to item `quotes`, MERGE mode, fields `id name price`) received `SUBOK,1,1,3` and then `U,1,1,1\|ACME\|99.9`, the last of three sent prices (101.5, 102.25, 99.9; MERGE conflates). |

### Why it failed before, and the issues

| # | Issue | Fix |
|---|---|---|
| 1 | The stock server has no adapter listening on 6661, so there was nothing to connect to. | Added the adapter set above (the real setup the earlier note called for). |
| 2 | **The earlier `java.net.SocketException: Socket closed` was caused by the platform's connection check.** It opens and closes a TCP connection to the connection's port (6661). The plain `PROXY_FOR_REMOTE_ADAPTER` treats that as a fatal load error ("Error while loading the Adapters") and the whole server shuts down. | Use the `ROBUST_PROXY_FOR_REMOTE_ADAPTER` data adapter (the server then starts at once, keeps running, and ignores stray connections), and point the connection at the web port 8080 with `proxy_port` 6661. |
| 3 | The proxy accepts one connector connection at a time. A second (or a paused) connector task keeps the slot and the next one fails with `Socket closed`. | One Lightstreamer sink per adapter set; restart the server (or the connector) to free the slot. |
| 4 | Every record was skipped with `Error while extracting record: A Schema is required`: the builder used schemaless JSON, but `#{VALUE.field}` needs a Connect Struct. | The builder now uses JSON-with-schema converters (`json_schema`) for Lightstreamer. |
| 5 | Config format: `topic.mappings` is `<topic>:<item name>` (or `item-template.<name>`) and `record.mappings` is `<field>:#{VALUE.<name>}`; the builder default (`value:#{VALUE}`) is only a placeholder. | Pass the `topic_mappings` and `record_mappings` pipeline options. |

Notes: v2 to v4 are failed or paused earlier attempts (v4 is paused and holds a task slot). The first deploys after each restart timed out (reconciler backlog).

## T2: Azure Cosmos DB Sink and Source (`tpl_cosmos_sink`, `tpl_cosmos_source`) on the Cosmos DB emulator

**Result:** PASS for both (no Azure account or subscription: Microsoft's free Linux emulator).

| | |
|---|---|
| Plugin | `microsoftcorporation/kafka-connect-cosmos` 1.18.0 (Hub latest, Nov 2025; matches the seed pin; Marketplace link in the plan) |
| Backend | `mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-latest` (native arm64, 2.17 GB on disk), container `t1-cosmos`: gateway on 8081 (HTTPS), health on host port 8082, Data Explorer on http://localhost:1234. Database `T1DB`, containers `items` and `feed` (partition key `/category`) created with `cosmoshell.sh` in the container. Account key: the emulator's well-known key. |
| Certificate setup | The Java SDK needs HTTPS, and the emulator's built-in certificate is only valid for `localhost`. A certificate for `t1-cosmos` was generated with openssl (`CN=t1-cosmos`, SAN `t1-cosmos`, `localhost`), given to the emulator with `-v cosmos.pfx:/mycert.pfx -e CERT_PATH=/mycert.pfx -e CERT_SECRET=...`, and imported into the Connect worker's Java trust store (`keytool -cacerts -importcert`, then a worker restart). |
| Sink pipeline | `t1-cosmos_sink-items-v2`, connection `t1-cosmos` (`url` `https://t1-cosmos:8081`, `database` `T1DB`, `master_key` the account key); input topic `org_1001-raw-t1-cosfeed-items-stream-v1` (3 plain JSON records with `id` and `category`) |
| Sink evidence | Connector and task RUNNING; `SELECT c.id, c.category, c.name, c.price FROM c` in container `items` returns Hammer, Saw (price 22) and Rake. |
| Source pipeline | `t1-cosmos_source-items-v2` (same connection), output topic `org_1001-raw-t1-cosmossource-items-stream-v2` |
| Source evidence | Connector and task RUNNING; the 3 documents arrive as JSON with Cosmos metadata (`_rid`, `_etag`, `_ts`, `_lsn`, `_attachments`). A document `Level` inserted while it ran appeared as a 4th record (change feed followed). |

### Builder (new in `connect_config_t1.py`; none existed)

Both templates: `connect.cosmos.connection.endpoint` (connection `url`, default `https://host:port`), `connect.cosmos.master.key` (connection `master_key` or `password`), `connect.cosmos.databasename`, `connect.cosmos.containers.topicmap` = `<topic>#<container>` (the pipeline entity, or option `container`), plain-JSON converters, and `connect.cosmos.connection.gateway.enabled=true` (the emulator only serves gateway mode; pipeline option `gateway: false` selects direct mode on real Azure).

### Findings

| # | Finding |
|---|---|
| 1 | The sink writes the record as the document; it needs an `id` field in the value (default strategy `ProvidedInValueStrategy`) and the container's partition key field (`category`). |
| 2 | Real Azure Cosmos DB was not tested; only the emulator (preview, gateway mode, no stored procedures). |
| 3 | The first deploys after the worker restart timed out (reconciler backlog) but the connectors were created. |

## T2: Firebase Realtime DB Sink and Source (`tpl_firebase_sink`, `tpl_firebase_source`)

**Result:** BLOCKED. The connector cannot be pointed at the Firebase emulator from this worker; it needs either real Google credentials or the emulator environment variable set on the Connect worker's JVM.

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-firebase` 1.2.21 (Hub latest, Sep 2026; matches the seed pin; uses Firebase Admin SDK 9.2.0). Needed the Confluent licensing jars copied in (as for other 2.x Confluent plugins). |
| Backend | Firebase emulator suite (`andreysenov/firebase-tools`, 641 MB, arm64) running only the Realtime Database emulator, container `t1-firebase`, port 9000, project `demo-t1`, open rules; namespace `demo-t1-default-rtdb`. REST on `localhost:9000` works (`PUT/GET /probe.json?ns=demo-t1-default-rtdb`). |
| Builder | New in `connect_config_t1.py` (none existed): `gcp.firebase.database.reference` = connection `url`, `gcp.firebase.credentials.path` (or `credentials_json`), `insert.mode` (default `set`) for the sink, `gcp.firebase.snapshot` and a RegexRouter to the pipeline topic for the source, plus the Confluent licence settings. |
| Pipelines | `t1-firebase_sink-users-v2`, `-v3`, `-v4` (all without data written). The Source was not run. |

### What was found

| # | Finding |
|---|---|
| 1 | The connector's own validation rejects a plain HTTP database address: `URI scheme must be one of the following. 'https'` for `http://t1-firebase:9000?ns=...`. The emulator only speaks HTTP. A TLS front was added (`caddy`, container `t1-firebase-tls`, certificate for `t1-firebase-tls` imported into the worker's Java trust store); the address `https://t1-firebase-tls:9443?ns=demo-t1-default-rtdb` then passes validation. |
| 2 | With that address the sink task starts (RUNNING) but nothing is written: it ends with `Unable to SET records to firebase` after the 10 s acknowledgement wait, and the TLS front's access log shows **no request at all**. The Admin SDK never opens a connection, because it first has to obtain a Google access token for the service account in `gcp.firebase.credentials.path`; a placeholder key cannot get one. |
| 3 | The Firebase Admin SDK skips that authentication only when the process environment variable `FIREBASE_DATABASE_EMULATOR_HOST` is set on the JVM. It cannot be set on the already running Connect worker; it needs the worker container to be recreated with the variable (and its plugins reinstalled), or a dedicated second worker. The other route is a real Firebase project with a service-account key (needs a Google account), which was not used. |
| 4 | Plugin quirk: restarting a Firebase task in the same worker fails with `IllegalStateException: FirebaseApp name ... already exists`, so each retry needs a new connector name or a worker restart. |
| 5 | The first deploys after each restart timed out (reconciler backlog) but the connectors were created. |

To unblock: (a) give the Connect worker `FIREBASE_DATABASE_EMULATOR_HOST=t1-firebase:9000` (compose `environment:` entry for `kafka-connect`, then recreate it) and keep the TLS front, or (b) use a real Firebase project and its service-account JSON.

## T2: VMware Tanzu GemFire Sink (`tpl_gemfire_sink`) on Apache Geode

**Result:** PASS (no account or licence). The earlier "BLOCKED: amd64-only" note did not apply: the amd64 image runs fine under emulation.

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-pivotal-gemfire` 1.0.27 (Hub latest, Sep 2026; matches the seed pin; bundles the Geode 1.15.1 client). Needed the Confluent licensing jars copied in. |
| Backend | `apachegeode/geode:latest` (Geode 1.15.1, Java 8, amd64 only, 208 MB compressed, run with `--platform linux/amd64` under emulation), container `t1-geode`, locator 10334 and cache server 40404. Started with gfsh: locator with `--hostname-for-clients=t1-geode`, a server `--max-heap=768m`, and region `orders` (PARTITION). |
| Pipeline | `t1-gemfire_sink-orders-v2`, connection `t1-geode` (host `t1-geode`, port 10334, the locator) |
| Input topic | `org_1001-raw-t1-gffeed-orders-stream-v1` (3 JSON-with-schema records) |
| Evidence | Connector and task RUNNING; `gfsh query "select * from /orders"` returns 3 rows: `{"order_id":1,"customer":"Ann","amount":120.5}`, `{"order_id":2,"customer":"Bob","amount":75.0}`, `{"order_id":3,"customer":"Cara","amount":310.25}`. |

### Builder (new in `connect_config_t1.py`; none existed). Only a Sink template exists for GemFire.

`gemfire.locator.host` / `gemfire.locator.port` from the connection, `gemfire.region` = the pipeline entity (or option `region`), `key.mode` default `kafka` (key = topic-partition-offset; option `key_mode: record_key` with `key_fields`), optional auth with `auth_enabled`, JSON-with-schema converters (values are written as structs), and the Confluent licence settings.

### Findings

| # | Finding |
|---|---|
| 1 | The region must exist on the cluster first (`create region --name=orders`); the sink does not create it. |
| 2 | gfsh `start locator --bind-address=0.0.0.0` fails (`not a valid address on any local network interface`); omit it and give `--hostname-for-clients` so the worker can reach the server by name. |
| 3 | The first deploy after the worker restart timed out (reconciler backlog) but the connector was created. |

## T2: Splunk Sink, Splunk HTTP Source and Splunk S2S Source on Splunk Enterprise

**Result:** PASS for all three (no account or key; Splunk Enterprise's built-in trial licence).

| | |
|---|---|
| Backend | `splunk/splunk:latest` (Splunk Enterprise, amd64 only, 1.9 GB compressed, 6.79 GB on disk, run under emulation), container `t1-splunk`: UI http://localhost:8003 (`admin` / `Dataconnect#123`), HEC on 8088 with token `t1-hec-token-0000-0000-000000000001`, management API on 8089. It became healthy in a few minutes and uses about 1.5 GB RAM. Started with `SPLUNK_START_ARGS=--accept-license`, `SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com`, `SPLUNK_PASSWORD`, `SPLUNK_HEC_TOKEN`. |
| Plugins | `splunk/kafka-connect-splunk` (sink; Hub 2.2.7 but it reports itself as **2.2.6**, so the seed pin was set to 2.2.6), `confluentinc/kafka-connect-splunk-source` 2.0.2 (HEC source) and `kafka-connect-splunk-s2s` 2.3.12 (S2S source). Both Confluent sources needed their licensing jars copied in. |

### Sink (`tpl_splunk_sink`)
Pipeline `t1-splunk_sink-events-v3`, connection `t1-splunk` (host `t1-splunk`, port 8088, `token`), input topic `org_1001-raw-t1-spfeed-events-stream-v1` (3 JSON records). Splunk search `index=main` returns the 3 events (e.g. `{"action":"login","id":1,"user":"ann"}`, sourcetype `_json`) plus one `Splunk HEC Configuration Check` event the connector sends at start. Builder options: `index` (default `main`), `sourcetype`, `validate_certs` (default off, HEC uses a self-signed certificate), `flush_window` (default 5 s instead of 30 s).

### HTTP Source (`tpl_splunk_http_source`)
Pipeline `t1-splunk_http_source-events-v3`, connection `t1-splunk-httpsrc-tls` (host `kafka-connect`, port 8083 only for the reachability check; `token` = the HEC token clients must send; `secret` = keystore password), options `listen_port` 8890 and `keystore_path` `/tmp/splunk-http.jks` (a keystore created on the worker with `keytool`). Three HEC events POSTed with `curl -k https://localhost:8890/services/collector/event -H "Authorization: Splunk <token>"` arrived on `org_1001-raw-t1-splunkhttpsource-events-stream-v3`, e.g. `{"time":1791282676229,"host":"...","sourcetype":"t1-test","index":"main","event":"{\"user\":\"ann\",\"action\":\"login\",\"id\":1}"}`.

### S2S Source (`tpl_splunk_s2s_source`)
Pipeline `t1-splunk_s2s_source-events-v2`, connection `t1-splunk-s2s`, `listen_port` 9997. Splunk was told to forward to the connector (`splunk add forward-server kafka-connect:9997`); the 3 events sent through its HEC (`"msg":"forward-test"`, sourcetype `t1-s2s`) arrived on `org_1001-raw-t1-splunks2ssource-events-stream-v2` with S2S metadata (`host`, `source`, `index`, `time`, `_tcpoutTime`). Splunk forwards everything it indexes, so thousands of its internal log events arrived too (over 8,800 at the time it was stopped); the forward target was then removed.

### Builders (new in `connect_config_t1.py`; none existed)
Sink: `splunk.hec.uri`, `splunk.hec.token`, index and sourcetype, certificate validation off by default, `/event` endpoint. Sources: `kafka.topic` = the pipeline topic, `splunk.port` / `splunk.s2s.port` = pipeline option `listen_port` (these listen **inside the Connect worker**), the HEC token from the connection, JSON converters.

### Findings
| # | Finding |
|---|---|
| 1 | The HEC Source's listener is **HTTPS only** ("SSL, (ssl, http/1.1)") and answers nothing (empty reply, `urllib` error) unless a keystore is configured (`splunk.ssl.key.store.path` and password). Without one every request dies in the TLS handshake. |
| 2 | The platform refuses credentials in a pipeline's `connector_config`, so the keystore password is taken from the connection's `secret`, and only the keystore path is a pipeline option. |
| 3 | The two source pipelines' connection must point at something reachable (the worker itself, 8083); the real ports are `listen_port`. Two sources cannot share a port in the same worker. |
| 4 | The sink's seed version had to follow the plugin's self-reported 2.2.6 (same stale-version quirk as the Aerospike 3.2.10 jar). |
| 5 | The first deploys after each worker restart timed out (reconciler backlog) but the connectors were created. |

## T2: Vertica Sink (`tpl_vertica_sink`)

**Result:** PASS. The earlier "BLOCKED: `vertica/vertica-ce` is no longer on Docker Hub" is true for that name, but other images work.

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-vertica` 1.3.3 (Hub latest, Oct 2025; matches the seed pin). It does **not** bundle a JDBC driver: `vertica-jdbc-10.0.1-0.jar` (Maven Central, `com.vertica.jdbc:vertica-jdbc`) was added to the plugin's `lib/`. |
| Image search | Not available: `vertica/vertica-ce`, `opentext/vertica-ce`, `vertica/vertica-k8s`. Available: `opentext/vertica-k8s` (Vertica 26.2, multi-arch with the `-multiarch` tags, but built for the Kubernetes operator in Eon mode, not run standalone) and `jbfavre/vertica` (community image, Vertica 9.2.0-7 Community Edition, amd64 only, 799 MB compressed). |
| Backend | `jbfavre/vertica:latest` under emulation (`--platform linux/amd64`), container `t1-vertica`, port 5433, database `docker`, user `dbadmin`. It creates the database and starts on its own (a few minutes). A password was set with `ALTER USER dbadmin IDENTIFIED BY 'dataconnect'` because the connector requires one. |
| Pipeline | `t1-vertica_sink-sales-v2`, connection `t1-vertica` (host `t1-vertica`, port 5433, database `docker`, `dbadmin`) |
| Input topic | `org_1001-raw-t1-vtfeed-sales-stream-v1` (4 JSON-with-schema records) |
| Evidence | Connector and task RUNNING; the connector created table `sales` itself (`auto.create`), and `select * from sales order by sale_id` returns 4 rows: `1 north 120.5`, `2 south 75`, `3 east 310.25`, `4 west 42`. |

### Builder (new in `connect_config_t1.py`; none existed)
`vertica.host`, `vertica.port`, `vertica.database` (connection `database`), `vertica.username` / `vertica.password`, `table.name.format` = the pipeline entity, `auto.create=true`, `pk.mode` (option `pk_mode`, `pk_fields`), JSON-with-schema converters.

### Findings
| # | Finding |
|---|---|
| 1 | The JDBC driver must be supplied separately; the oldest driver on Maven Central (10.0.1-0) worked against a 9.2 server. |
| 2 | The task list in the Connect status was empty for a while after creation, but the data arrived; the first deploy after a worker restart timed out as usual. |
| 3 | Only Vertica 9.2 (2019) was tested; a current Vertica needs the operator-style `opentext/vertica-k8s` image and an Eon-mode database. |

## T2: Diffusion Sink and Source (`tpl_diffusion_sink`, `tpl_diffusion_source`)

**Result:** PASS for both, as a Kafka to Diffusion to Kafka round trip. The old note ("`pushtechnology/diffusion` is no longer published") is right about the name, but the server image moved.

| | |
|---|---|
| Plugin | `DiffusionData/Diffusion-Connector` 1.0.0 package. It reports its version as **0.0.1**, so both seed pins were set to 0.0.1 (the platform compares the reported version). |
| Backend | `pushtechnology/docker-diffusion:latest` (Diffusion server 6.12, amd64 only, 274 MB compressed, run with `--platform linux/amd64`), container `t1-diffusion`, WebSocket and management console on host port 8086 (http://localhost:8086, `admin` / `password`), with a trial licence built in. Started in under a minute. |
| Sink pipeline | `t1-diffusion_sink-prices-v3`, connection `t1-diffusion` (host `t1-diffusion`, port 8080, `admin` / `password`), option `destination` default `kafka/${topic}`; input topic `org_1001-raw-t1-dffeed-prices-stream-v1` (keyed JSON records AAPL, MSFT, GOOG) |
| Source pipeline | `t1-diffusion_source-prices-v2`, same connection, option `selector` `?kafka/.*`, output topic `org_1001-raw-t1-diffusionsource-prices-stream-v2` |
| Evidence | Both connectors and tasks RUNNING. The sink publishes to the Diffusion topic `kafka/org_1001-raw-t1-dffeed-prices-stream-v1`, which holds the latest value; the source subscribed to `?kafka/.*` and delivered key `kafka/org_1001-raw-t1-dffeed-prices-stream-v1`, value `{"symbol":"GOOG","price":175.1}` (the topic's current value) and, after a new AAPL record was produced, `{"symbol":"AAPL","price":191.75}`. |

### Builder (new in `connect_config_t1.py`; none existed)
`diffusion.url` (connection `url`, default `ws://host:port`), `diffusion.username` / `diffusion.password`, string keys and JSON values (no schema), sink `diffusion.destination` (default `kafka/${topic}`, option `destination`), source `kafka.topic` and `diffusion.selector` (option `selector`, default `?kafka/.*`).

### Findings
| # | Finding |
|---|---|
| 1 | With the default destination every record of a topic goes to the same Diffusion topic, so only the latest value is kept (Diffusion topics are state, not a log). Use `destination` = `kafka/${topic}/${key}` to keep one Diffusion topic per record key. |
| 2 | The Diffusion Source delivers the topic's current value when it first subscribes, then every update. |
| 3 | The platform's version check needed the plugin's self-reported 0.0.1 (same stale-version pattern as the Aerospike, Splunk and Vertica packages). |
| 4 | The first deploys after each worker restart timed out (reconciler backlog) but the connectors were created. |

## T2: HEAVY.AI (OmniSci) Sink (`tpl_omnisci_sink`)

**Result:** PASS. The earlier "BLOCKED: only large CUDA images" was incomplete: a small CPU-only open-source image exists.

| | |
|---|---|
| Plugin | `confluentinc/kafka-connect-omnisci` 1.0.14 (Hub latest, Sep 2026; matches the seed pin). It bundles its own `omnisci-jdbc` 5.4.0 driver and needs the Confluent licensing jars copied in. |
| Image search | The Docker Hub namespace `heavyai` is empty or gone, but `omnisci/core-os-cpu` (CPU only, open source, amd64, v5.10.2, 319 MB compressed) is still published, together with `omnisci/omnisci-ee-cpu`. The CUDA builds are not needed. |
| Backend | `omnisci/core-os-cpu:v5.10.2` (run with `--platform linux/amd64` under emulation), container `t1-omnisci`, Thrift port 6274, Immerse web UI port 6273; default login `admin` / `HyperInteractive`, database `omnisci`. It started within seconds; `omnisql` is in the container (`/omnisci/bin/omnisql -p HyperInteractive`). |
| Pipeline | `t1-omnisci_sink-events-v2`, connection `t1-omnisci` (host `t1-omnisci`, port 6274, database `omnisci`, `admin` / `HyperInteractive`) |
| Input topic | `org_1001-raw-t1-omfeed-events-stream-v1` (4 JSON-with-schema records) |
| Evidence | Connector and task RUNNING; the connector created table `events` (`auto.create`) and `select * from events order by event_id` returns `1 sensor-a 21.5`, `2 sensor-b 19.75`, `3 sensor-a 22`, `4 sensor-c 18.25`. |

### Builder (new in `connect_config_t1.py`; none existed)
`connection.host`, `connection.port` (6274), `connection.database` (default `omnisci`), `connection.user` / `connection.password`, `table.name.format` = the pipeline entity, `auto.create=true`, JSON-with-schema converters, and the Confluent licence settings.

### Findings
| # | Finding |
|---|---|
| 1 | OmniSci was renamed HEAVY.AI in 2022 and the old `omnisci` images stopped at v5.10.2 (Feb 2022); the plugin's driver is the 5.4.0 line and works against it. Newer HEAVY.AI releases were not tested (no public image found). |
| 2 | The first deploy after the worker restart timed out (reconciler backlog) but the connector was created; the task list was empty for a short time while the data was already arriving. |

## T2: Imply, StarTree, Kinetica, Onibex ClickHouse and Onibex Snowflake: why the Hub install says "not available" (checked 2026-10-06 against the Confluent Hub API)

| Entry | Hub record | Verdict |
|---|---|---|
| `imply/imply-native-integration` ("Imply Polaris Native Integration") | `kafka_connect_api: false`, no archive, no version, no release date, no source URL. The description says the integration is "embedded in the Imply UX" and is based on the Kafka Client API (Imply Polaris is a SaaS built on Apache Druid, trial by request). | **Not a Kafka Connect connector.** There is no plugin to install, so "Component is not available for installation yet" is accurate and permanent. It cannot be tested in this platform; ingestion is configured inside Imply Polaris (needs an Imply account). |
| `startree/startree-native-integration` ("StarTree Native Integration") | Same: `kafka_connect_api: false`, no archive, "embedded in the StarTree UX", point StarTree Data Manager at Confluent Cloud. | Not a Kafka Connect connector. Not testable here (needs a StarTree Cloud account). |
| `kinetica/kinetica-connector` 6.2.1 | `kafka_connect_api: true`, source and sink, but no downloadable archive on Hub; `source_url` is GitHub `kineticadb/kinetica-connector-kafka` (open source). | A real connector whose Hub artifact is missing; it could be built or taken from the GitHub releases. The backend would be a Kinetica database (a GPU-oriented product; image and licence not checked). Not attempted. |
| `onibex/clickhouse-sink-connector` 1.1.1 and `onibex/snowflake-sink-connector` 2.3.4 | `kafka_connect_api: true`, sink; no archive on Hub; vendor docs at help.onibex.com. | Real commercial connectors distributed by the vendor, not through Hub; need an Onibex download or trial. Not attempted. |

## T2: the SaaS-only connectors, checked for a local stand-in (2026-10-06)

All ten plugins were loaded on the worker and their settings listed for an endpoint override. Plugins are stashed again; nothing was deployed.

| Connector | Endpoint setting found | Could it be tested without an account? |
|---|---|---|
| Datadog Logs | `datadog.url` | Yes, partly: a small stand-in HTTP server can receive the log batches and show the request format. Not the real Datadog service. |
| Ably | `client.rest.host` | Partly, same idea (Ably's REST publish API would have to be imitated). |
| Vectara | `api.url` | Partly, same idea (document indexing endpoint). |
| Salesforce (SObject sink, CDC and other sources) | `salesforce.instance` | Hardly: needs imitated OAuth login, REST and (for the sources) the Bayeux streaming API. |
| Azure Data Explorer (Kusto) | `kusto.ingestion.url`, `kusto.query.url` (both required) | No: ingestion goes through Azure queues and blobs. |
| Snowflake Streaming | `snowflake.url.name`, OAuth endpoint | No: needs Snowflake's own protocol. |
| New Relic (Events, Logs, Metrics) | only `nr.region` (US or EU), no URL | No without DNS and TLS tricks. |
| PagerDuty | none (fixed Events API URL) | No. |
| Google Cloud Functions | `region` (the function URL is built from region, project and function name) | No. |
| Google BigQuery (wepay) | none found | No. |

Free accounts that may be enough to test the real services (to be confirmed on each vendor's sign-up page): Ably (free plan), New Relic (free tier), Salesforce (Developer Edition), Azure Data Explorer (free cluster), Vectara (free trial), Datadog, PagerDuty and Snowflake (time-limited trials).

---

# Release matrix: Debezium MySQL CDC (snapshot-to-stream transition, offset recovery)

| | |
|---|---|
| Result | **PASS** (14 of 14 checks) |
| Backend | The `mysql` test unit (MySQL 8.4.11, ROW binlog, CDC grants), database `dataconnect`, table `rm_customers` |
| Plugin | `debezium-connector-mysql` 3.0.8.Final, the template's pin |
| Pipeline | `rm-mysql-cdc-<run>`, org `org_release_matrix`, connector `org_release_matrix--rm--mysqlcdc--rm_customers--cdc--v<run>`; kept with its topic as proof |
| Checks | Snapshot emitted all 5 rows once (flags `first`, `true`, `last`). Insert, update (before and after) and delete (plus tombstone) were captured with `snapshot=false`. No second snapshot at the snapshot-to-stream switch. Pause, change, resume: down-time changes captured once. Connect worker stopped, rows changed, worker started: offset recovered, no duplicates, no re-snapshot, streaming continued. |

Issue found: **the shipped image cannot load this plugin.** `infra/kafka-connect.Dockerfile` installs Debezium MySQL 3.0.8, which is compiled for Java 17 (class file 61), but `confluentinc/cp-kafka-connect:7.6.0` runs Java 11. The worker logs `UnsupportedClassVersionError ... only recognizes class file versions up to 55` and `MySqlConnector` is missing from `/connector-plugins`, so the template shows `PLUGIN_NOT_INSTALLED`. For this test Zulu JDK 17 was unpacked into the running container (`/opt/jdk17`, `/etc/alternatives/java` pointed at it) and the worker restarted; afterwards both were reverted and the MySQL unit stopped. A permanent fix needs a Java 17 base image, or Debezium 2.x with MySQL 8.0 (2.x runs `SHOW MASTER STATUS`, which MySQL 8.4 removed).

Test note: events from different partitions are not in global order; check ordering per row key or by binlog position and row index (`source.pos`, `source.row`).

---

# Release matrix: REST API Source (pagination, cursors, 429 and retry)

| | |
|---|---|
| Result | **FIXED and PASS** (first run: blocked; see the follow-up below) |
| Platform | A pipeline from `tpl_rest_source` cannot be deployed: readiness fails with `Connector class 'com.github.castorm.kafka.connect.http.HttpSourceConnector' has no config builder yet.` (`app/services/connect_config.py`). The template pins plugin version 2.0.1, which castorm never released (latest 0.8.11); the installed jar reports version `0.0.0.0`, so the version check would not pass either. |
| Connector tests | Plugin `castorm/kafka-connect-http` 0.8.11 (8.8 MB, installed in the container only), against a local stub server that paginates, hands out cursors and returns 429 and 503. The connectors were created directly through the Connect REST API, not through the platform. |
| Pipeline kept as proof | `rm-rest-cursor-<run>` (draft, org `org_release_matrix`), the REST connection and the `org_release_matrix-raw-rm-rest-*` topics |

| Check | Result |
|---|---|
| Cursor pagination (2 pages) | Pass: every record once, the request carries the last record id (0, 5, 10), later records are picked up |
| Page-number pagination | Pass: pages walked in order, a page added later is picked up |
| Duplicates while polling | Poll interval (`http.timer.interval.millis`) must be longer than the Connect offset flush (about 60 s). The connector reads its position from flushed offsets, so a faster poll re-fetches the same page (each record 3 times at 2 s and 15 s intervals). |
| Restart and worker restart | At-least-once: records after the last flushed offset are delivered again (ids 6-10 twice). Duplicates carry the identical record key, so a consumer can de-duplicate. |
| 429 with the default policy | Fail: the task goes FAILED (`Policy failed for response code: 429`) and stays failed until restarted. `Retry-After` is ignored. |
| 503 with the default policy | Fail: same, the task goes FAILED. |
| 429 with `http.response.policy.codes.skip=300..399, 429` | Pass: retried on every poll and data delivered. `Retry-After` is still ignored. |

Test pitfalls found: the initial offset must contain `key` and an ISO `timestamp` (`2020-01-01T00:00:00Z`) when the offset pointer has them. Castorm stamps each Kafka record with the record's own timestamp, so test data with old timestamps is deleted by topic retention at once.

Reverted afterwards: the plugin directory was removed, Connect restarted (castorm no longer loaded), the stub stopped and the direct connectors deleted. Topics and the draft pipeline were kept.

## Follow-up: REST source fixed and re-tested through the platform (14 of 14 checks)

| Change | Where |
|---|---|
| Added the missing config builder for the REST API source (pipeline options `path`/`url`, `params`, `method`, `headers`, `body`, `list_pointer`, `offset_pointer`, `initial_offset`, `poll_interval_seconds`, `retry_status_codes`; any `http.*` option passes through and wins) | `app/services/connect_config_t1.py` (`HTTP_SOURCE`, `_http_source_config`) |
| **429 and 503 no longer fail the task:** `http.response.policy.codes.skip` defaults to `300..399, 429, 500..599`, so the response is skipped and the request repeats on the next poll (`retry_status_codes` changes it) | same |
| Default poll interval 65 s (above the Connect offset flush) so pages are not re-published; a timestamp offset also gets an initial ISO timestamp | same |
| Template pin corrected from 2.0.1 (never released) to 0.8.11 | `app/db/seed_templates.py` |
| 17 unit tests, five mutations each caught | `tests/test_connect_config_http_source.py` |

Live run through the platform (pipelines `rm-rest-p429-<run>`, `-p503-`, `-pcur-`, org `org_release_matrix`): deploy readiness passes; the generated connector carries the retry policy; against the stub server the first two requests got 429 (and 503) and the task stayed RUNNING, never FAILED, then data arrived (ids 1-8 and 1-6) with nothing duplicated; cursor pagination delivered ids 1-10 once each.

Still true: `Retry-After` is ignored (the retry is the next poll), and the castorm plugin is not in the Connect image, so the catalog shows the template as unavailable until it is installed. Reverted afterwards: plugin removed, Connect restarted, stub stopped; the three pipelines were paused and kept, with their topics, as proof.

## Transformations: TimestampConverter settings and CDC timestamps (2026-10-16)

| Setup | Database `tf_src_134770` (tables `people_jdbc`, `people_cdc`), pipelines `tf-jdbc-*-134770` and `tf-cdc-*-134770` (org `org_1001`) |
|---|---|

What a TimestampConverter wrote to Kafka for `2026-10-11 08:30:01` (JDBC Source):

| Target type | Result |
|---|---|
| string, format `yyyy-MM-dd HH:mm:ss` | `"2026-10-11 08:30:01"`, the only human-readable one |
| string, no format (the form showed the format only as a placeholder) | Deploy reported success, then the task FAILED: `TimestampConverter requires format option to be specified when using string timestamps` |
| unix / Timestamp / Date / Time | Numbers: `1791707401000`, the same (already a Timestamp), `20737` days, `30601000` ms since midnight |

On Debezium CDC a string target gave year `58746`: Debezium's default `time.precision.mode` (adaptive) writes a TIMESTAMP as
epoch microseconds, which TimestampConverter reads as milliseconds. With `time.precision.mode=connect` (checked with a temporary
connector created directly on Connect, removed afterwards with its slot, publication and topic) it wrote `"2026-10-11 08:30:01"`.

| Fix | Where |
|---|---|
| Format defaults to `yyyy-MM-dd HH:mm:ss` and is required while the target type is string (`required_when`) | `app/db/seed_templates.py` |
| Saving a transform policy with a missing required setting is a 422 naming it (`validate_transform_settings`) | `app/services/transform_policy.py`, `app/api/routes/transforms.py` |
| The deploy preflight checks the same settings in the generated config (check `smt_settings_valid`), so an older policy is blocked on the "Ready to deploy" screen | `app/services/deploy_preflight.py` |
| A connector or task Kafka Connect reports FAILED has its reason (first trace line) saved as the pipeline's runtime error, and kept through the self-heal restarts until the connector and every task really run | `app/services/runtime_reconciler.py` (`connect_failure_reason`, `connect_healthy`) |
| A CDC pipeline whose transforms include TimestampConverter gets `time.precision.mode=connect` (sub-millisecond precision is dropped; other CDC pipelines are unchanged) | `app/services/connect_config.py` |
| The form refuses Create while a required transform setting is blank, shows the error under that box, and shows placeholders as `e.g. …` | frontend `src/pages/Pipelines.tsx`, `src/templateAdapters.ts` |

The Timestamp field is required, not pre-filled: a guessed column name the table doesn't have makes every record fail at that
transform, and a source's default "skip bad records" policy then drops them all without a failed task.

Tests: `tests/test_transform_settings.py` (34; each backend fix disabled in turn was caught), frontend
`tests/e2e/pipelines.spec.ts` (2) and `tests/unit/typed-adapters.spec.ts` (3).

Still open from the same test run: HeaderFrom cannot deploy (the backend asks for the abstract `HeaderFrom` class instead of
`HeaderFrom$Value`), the form sends Debezium's ExtractNewRecordState last so every field transform runs on the CDC envelope and
every record is dropped, and a user RegexRouter has no effect because the platform's own reroute runs after it.

## Several tasks per connector: `tasks_max` (2026-10-17)

Every config builder sets `tasks.max=1`, so the Monitoring task table could only ever show one task. A pipeline can now set the
option `tasks_max` (in `connector_config`), but only where the connector really divides its work across tasks:

| Connector | Tasks it can use | `tasks_max` |
|---|---|---|
| JDBC source | one per table at most | 1 to 16 |
| JDBC sink | one per topic partition at most | 1 to 16 |
| Every other connector (Debezium CDC, ...) | one | only 1; a larger value is refused when the config is built (422 at deploy) |

A value that is not a whole number, or is outside 1 to 16, is refused the same way. Connect starts fewer tasks than asked when there is
less to divide, so asking for more than the tables or partitions is harmless. Code: `app/services/connect_config.py`
(`_tasks_max`, `PARALLEL_TASK_CONNECTORS`). Tests: `tests/test_connect_config_tasks_max.py` (13).

Live check (org `org_1001`, run 211474; the pipelines and `mt_tgt_211474` are kept as proof): `multi-task-sink-211474` (topic with 6
partitions, `tasks_max=3`) runs Tasks 0, 1 and 2 in Kafka Connect and `multi-task-source-211474` (two tables, `tasks_max=2`) runs
Tasks 0 and 1, all RUNNING. Monitoring lists them as Task 1, 2 and 3 (Connect counts from 0, the page counts from 1), each with its
own rates. There is no field for `tasks_max` in the New pipeline form yet; it is set through the API.

## Custom SMT on a real table: `sql_practice.products` through Debezium PostgreSQL CDC (2026-10-22)

| Setup | A Custom SMT made with "+ New SMT" and attached in the New pipeline form (the Custom SMT dropdown); table `sql_practice.products` (64 rows) on the host PostgreSQL; pipelines `smt-all10-v073732` (never deployed) and `smt-9-v073755` (running), org `org_1001`; topic `org_1001-raw-smttest-sqlpractice-products-cdc-v073755` |
|---|---|

All 10 SMT types in one policy (`products-smt-all10`) is refused before deploy: the readiness check `connector_compatibility` fails with
"Transform class 'org.apache.kafka.connect.transforms.HeaderFrom' is not installed" (the open HeaderFrom bug: the backend asks for the abstract class, not
`HeaderFrom$Value`). The same chain without HeaderFrom (`products-smt-9`, 9 steps) deployed: connector and task RUNNING, 64 records in the topic.

| Step | What a record shows in the topic |
|---|---|
| ValueToKey `product_id`, then ExtractField (key) `product_id` | The Kafka key is the plain id (`3`, `9`, `16`), not a struct |
| MaskField `description,sku` (replacement `***`) | Both are `"***"` |
| ReplaceField rename `product_name:name`, `stock_quantity:stock`, exclude `sample` | `name`, `stock` present; `sample` gone |
| InsertField static `source_db=sql_practice`, `topic.field`, `timestamp.field` | `source_db` and `kafka_topic` present (the topic name Debezium chose, before the platform's reroute); `ingested_at` is `null`: Debezium sets no record timestamp |
| TimestampConverter `updated_at` to string | `"2026-08-22 12:00:00"` (no year-58746 bug: the backend sets `time.precision.mode=connect`) |
| HoistField `product` then Flatten `_` | Every column is `product_<column>` |
| RegexRouter `(.*)` to `$1` | Topic unchanged (a user RegexRouter still has no effect: the platform's reroute runs after it) |

Live changes (a row inserted, updated and deleted in `products`, then removed): the insert and update arrive as new records with the same chain applied (price 10.5 to
12.75, stock 5 to 4, `updated_at` as text). The delete arrives as a record with `product___deleted: "true"` and every non-key column at its default
(`name ""`, `price 0.0`, `updated_at "1970-01-01 00:00:00"`), because the table's replica identity is the default (key only) and the platform unwraps with
`delete.handling.mode=rewrite`. Nothing is left in the table. The pipelines, policies and topic are kept as proof.

### Fixed after that run (2026-10-22): the three open SMT problems

| Problem | Fix | Where |
|---|---|---|
| HeaderFrom could not deploy (abstract class) | HeaderFrom is now a scoped transform: `HeaderFrom$Value` / `HeaderFrom$Key` | `app/services/transform_policy.py` (`SCOPED_TRANSFORMS`) |
| The form sent Debezium's ExtractNewRecordState last, so every field transform ran on the change envelope | An explicit ExtractNewRecordState always runs first; the other transforms keep their order | `app/services/connect_config.py` (`_unwrap_first`) |
| A user RegexRouter on a source did nothing (the platform routes a source to its pipeline's topic after it) | A source refuses it: creating/updating a source pipeline with a policy that has one is a 422, and the deploy-readiness check `transform_configuration_valid` fails if a shared policy gains one later; a sink may still use it. The frontend does not offer it on a source | `transform_policy.py` (`source_transform_problems`), `pipeline_spec_validator.py`, `pipeline_service.py` |

Live proof (same table): policy `products-smt-header` (9 steps: everything a source may use, HeaderFrom included) on pipeline `smt-header-v075200` deployed,
connector and task RUNNING, 64 records, each with the header `product_category:<value>`, the connector config shows `HeaderFrom$Value` and `unwrap` first.
Pipeline `smt-unwrap-v075302` (boxes MaskField + ExtractNewRecordState ticked in the form) deployed with the order `ExtractNewRecordState,MaskField,reroute`;
64 records, `sku` masked (before the fix every record was dropped). The earlier pipelines `smt-all10-v073732` and `smt-9-v073755` hold a RegexRouter, so
their readiness check now says so; the running one is unchanged. Tests: `tests/test_smt_deploy_fixes.py` (9; each fix disabled in turn was caught).

## MySQL with large data (2026-11-02)

MySQL 8.4.11 from the `mysql` test unit (`./test-backends.py up mysql`, image already local, login `dataconnect`/`dataconnect`, binlog ROW).
Connection `mysql-test` (host `mysql`, database `dataconnect`). Data: `sql_practice.electronics_customers_live` on the Mac's Postgres, 141,000 rows.

### Test 1: Postgres → MySQL, batch (incrementing), no quality check, no transforms: PASSED

| Pipeline | What it is | Result |
|---|---|---|
| `pg2my-src-plain` | JDBC source, `load_mode` incrementing on `row_id`, topic `org_1001-raw-pg2my-pg-electronics_customers_live-batch-v1` | RUNNING, 141,000 records |
| `pg2my-sink-plain` | JDBC sink, connection `mysql-test`, table `pg2my_plain` (auto-created) | RUNNING, 141,000 rows in MySQL within about a minute |

Checked beyond the count: same number of distinct `row_id`, same min / max / sum, and the same MD5 over every row's `order_id`, `customer_name`,
`email`, `order_date`, `unit_price`, `total_amount` in `row_id` order (values compared at 2 decimal places).

Findings (not fixed, waiting for a decision):

1. **The MySQL connection test does not log in.** With a wrong password, `POST /connections/test-connection` answers `ok` ("Reachable mysql:3306"),
   and a saved connection's test says "structurally valid". Only the TCP port is checked. The earlier wrong-password fix covered Postgres only.
2. **Money becomes floating point.** The JDBC source sets `numeric.mapping=best_fit`, so `NUMERIC(12,2)` (`unit_price`, `total_amount`, `total_amount_usd`)
   travels as FLOAT64 and the sink creates `DOUBLE` columns. Every value still rounds to the right cents, but sums show float error
   (`3071423019.2899995` against `3071423019.29`). Same for any sink, not only MySQL.
3. Text columns are created as `TEXT` (Postgres `varchar(n)` loses its length), and the table has no primary key (`pk.mode=none`, `insert.mode=insert`):
   a redelivered record becomes a duplicate row.

### Blocked: MySQL CDC (Debezium MySQL)

The Connect worker runs Java 11 (`cp-kafka-connect` 7.6.0). The image installs Debezium MySQL 3.0.8 (because MySQL 8.4 removed `SHOW MASTER STATUS`,
which Debezium 2.x still uses), and Debezium 3.x is built for Java 17: Connect logs `UnsupportedClassVersionError ... class file version 61.0` at start-up
and never loads `io.debezium.connector.mysql.MySqlConnector`. The deploy-readiness check says so correctly ("Connector class ... is not installed on the
Kafka Connect worker"), so a new MySQL CDC pipeline cannot be deployed; the older `rm-mysql-cdc-882592` keeps failing.

### Unblocked (his choice A): MySQL 8.0 + Debezium MySQL 2.5.4

* `mysql:8.0` pulled (8.0.46) and run as a separate container `mysql80` on the `dataconnect` network (host port 3307, volume `tb_mysql80_data`,
  label `inaiera.temp-test=mysql-cdc-2026-11-02`), binlog ROW / FULL, GTID on, the same `test-backends-init/mysql` grants. Not in any compose file.
  The 8.4 `mysql` unit is unchanged (Test 1's table is there).
* Connect worker: `debezium-connector-mysql` 3.0.8 moved to `/usr/share/connect-plugin-stash/` inside the container, 2.5.4.Final installed from Maven
  Central, worker restarted: `MySqlConnector 2.5.4.Final` loads. A hand install: recreating the container (or rebuilding the image) brings 3.0.8 back.
* Template `tpl_debezium_mysql` re-qualified through `PUT /connector-templates/tpl_debezium_mysql` (his approval): `connector_version` 2.5.4.Final,
  `supported_source_versions` 5.7 / 8.0, a migration note saying why. A pipeline created before that stays pinned to the old revision and fails the
  `connector_compatibility` check, so the first draft was deleted and created again.
* Test data: `dataconnect.electronics_customers` in MySQL 8.0, the same 141,000 rows (CSV from Postgres, `LOAD DATA`), `row_id` primary key, DECIMAL money:
  count and sums identical to Postgres.

### Test CDC-1: MySQL CDC source (Debezium MySQL 2.5.4, snapshot `initial`), no quality check, no transforms: PASSED

Pipeline `my80-cdc-src` (connection `mysql80-cdc`), topic `org_1001-raw-my80-mysql-electronics_customers-cdc-v1`: RUNNING, 141,000 snapshot records (op `r`),
values equal to the table. Live: an insert (op `c`, row 141001), an update of row 7 (op `u`, before and after, `unit_price` 455.87 → 999.99) and a delete of
row 8 (op `d` + a tombstone) arrived within seconds: 141,004 records. Kept in MySQL as proof.

What a record looks like: the full Debezium envelope (`before`, `after`, `source`, `op`) as plain JSON without a schema; dates as days since 1970
(`19358` = 2023-01-01); DECIMAL as plain numbers.

### CDC topic → JDBC sink: refused, as designed

Draft `my80-cdc-sink-pg` (JDBC sink to Postgres `sql_practice`, table `my80_cdc_electronics`) on that topic: the readiness check refuses it ("pipeline
'my80-cdc-src' writes it as plain JSON without a schema ... every record would be skipped"). Kept as a draft for proof. Small finding: the same message is
shown under "Connector plugin installed" as well as "Connector compatibility", although the plugin is installed.

### Tests 2–6, run together (2026-11-02)

Sources: JDBC incrementing on `row_id`. Postgres side `sql_practice.electronics_customers_live` (141,000 rows); MySQL side `mysql80` `dataconnect.electronics_customers`
(141,000 rows after the CDC test). Quality = `Electronics_Order_Check` (14 rules) on the sink, ENFORCE (valid records go through the prepared topic,
failures to quarantine). Standard transforms = a per-pipeline auto policy (ValueToKey row_id, InsertField source_system, ReplaceField exclude state /
rename customer_type→customer_segment, MaskField mobile_number, TimestampConverter order_date→yyyy-MM-dd). Custom = `Elec JDBC custom chain`.
The expected quality result was worked out independently in SQL on both sources: 32,043 failing rows, 108,957 passing.

| Test | Pipelines | Target | Result |
|---|---|---|---|
| 2 PG → MySQL, standard transforms + quality | `pg2my-src-std` → `pg2my-sink-quality` | MySQL `pg2my_quality` | **PASSED exactly**: 108,957 passed / 32,043 quarantined; the set of row_ids in MySQL has the same MD5 as the rows that pass all 14 rules in SQL; 0 rows in the table break a rule; every transform applied |
| 3 PG → MySQL, custom chain | `pg2my-src-custom` → `pg2my-sink-custom` | MySQL `pg2my_custom` | **PASSED after a restart** (see OOM): all 141,000 row_ids, every row masked / tagged / email+mobile dropped / date `dd-MM-yyyy`; 347 duplicates from the restart |
| 4 MySQL → PG, plain | `my2pg-src-plain` → `my2pg-sink-plain` | PG `my2pg_plain` | **Data correct, with duplicates**: all 141,000 row_ids, money sums equal MySQL to the cent (exact `numeric` columns); 40,683 duplicate rows from an OOM restart |
| 5 MySQL → PG, standard transforms + quality | `my2pg-src-std` → `my2pg-sink-quality` | PG `my2pg_quality` | **FAILED**: every record quarantined, 0 passed (see finding A) |
| 6 MySQL → PG, custom chain | `my2pg-src-custom` → `my2pg-sink-custom` | PG `my2pg_custom` | **PASSED after a restart**: all 141,000 row_ids, every transform applied; 21,046 duplicates from the restart |

Findings:

A. **MySQL DECIMAL arrives as base64 bytes, and the quality worker cannot read it.** `numeric.mapping=best_fit` only converts columns the driver reports as
   NUMERIC; MySQL reports DECIMAL, so `unit_price` / `total_amount` / `total_amount_usd` keep Connect's Decimal logical type, which JSON carries as bytes
   (`"unit_price": "ITcg"`). The JDBC sink decodes it (Test 4 got exact numerics), but the quality worker compares the base64 text, so `unit_price_range`
   and `total_usd_range` fail on every MySQL record. Candidate fixes: `numeric.mapping=best_fit_eager_double` on the source, or decode Decimal (and the
   other logical types) in the quality worker from the record's schema.
B. **The Connect worker ran out of heap.** `KAFKA_HEAP_OPTS=-Xms256M -Xmx768M` for ~75 connectors; five 141,000-row loads + five sinks at once caused
   `OutOfMemoryError: Java heap space` at 05:03:01, killing 18 tasks across the org (also older pipelines: final-check, products, gap-lin, tf-*, price_list_2).
   DataConnect's self-heal restarted them, and each restarted JDBC source re-sent everything since its last committed offset: the duplicates above
   (the sinks have no primary key, so they become duplicate rows). Heap stays at ~550 of 768 MB even when idle.
C. **A task can hang while reported RUNNING.** The OOM killed the producer network thread of `pg2my-src-custom` and the querier of `my2pg-src-custom`;
   both tasks stayed RUNNING in Connect and in DataConnect (no error, no alert) while sending nothing. Only a thread dump showed it. A manual Restart fixed both.
D. The JDBC source's money columns from Postgres become DOUBLE (finding 2 of Test 1); from MySQL they stay exact. The two directions behave differently.
E. The quality-failures alert rule fired for both quality sinks and was sent to the Teams / Slack channels (the alerts feature working as intended).

## MySQL round: cleanup, fixes and re-test (2026-11-02, his go-ahead: "fix all, remove the large pipelines first, then test")

### Cleanup

Deleted through the API (connectors and their offsets go with them): the 5 `elec-*` pipelines of the 28 Oct large-data test and the 14 of the MySQL round
(`pg2my-*`, `my2pg-*`, `my80-*`). Then by hand: their 22 Kafka topics (three quality topics came straight back empty because `auto.create.topics.enable`
lets the stopping quality worker recreate them; deleted again, they stayed gone), the target tables (MySQL `pg2my_plain/quality/custom`; Postgres
`elec_sink_from_jdbc`, `my2pg_plain/custom`), the rows the pipeline delete leaves behind (3 quality results, 36 lineage edges, 39 lineage assets that
nothing else used, 7 alerts with 29 events, 57 alert observations) and the 4 per-pipeline "auto" transform policies. Kept: the source tables, the
connections, `Electronics_Order_Check`, the two custom chains. Database backup first: `~/.inaiera-local/backups/dataconnect-db-before-large-cleanup-20261102.sql`.

### Fixes (uncommitted)

| Finding | Fix | Where |
|---|---|---|
| Connect ran out of heap (768M, ~75 connectors); OOM left tasks RUNNING but dead | `-Xms512M -Xmx2G -XX:+ExitOnOutOfMemoryError` on the dev worker (exit-on-OOM on every pool worker): a JVM out of heap exits and `restart: unless-stopped` brings every connector back from its offsets | `infra/docker-compose.yml`, `docker-compose.fresh.yml` |
| Money as DOUBLE (Postgres) / unreadable bytes (MySQL) | New pipelines (`record_format` v2, migration 0056) keep exact decimals: JDBC `numeric.mapping=precision_only`, Debezium `decimal.handling.mode=precise`; the quality worker decodes Connect decimals with the record's schema (all pipelines) | `connect_config.py`, `quality_stream_worker.py` |
| Duplicates after a restart | v2 JDBC source keys each record by the table's primary key (`pkkey` ValueToKey, before the user's transforms); a v2 JDBC sink upserts by it (`pk.mode=record_key`) when the producer is v2, one table, and no user transform changed the key (a ValueToKey on exactly the PK still counts as the PK) | `connect_config.py` (`_upsert_settings`, `_changes_key`) |
| CDC topic unreadable by a database sink | v2 CDC source: rows with schema (ExtractNewRecordState first, `delete.handling.mode=rewrite`, `drop.tombstones=false`), `time.precision.mode=connect`; the sink upserts and `delete.enabled=true`; the quality worker forwards tombstones in ENFORCE instead of dead-lettering them; the producer can send a null value | `connect_config.py`, `quality_stream_worker.py`, `kafka_runtime.py` |
| MySQL connection test did not log in | live probe logs in (`SELECT VERSION()`), like Postgres | `connection_adapters.py` |
| Build error shown under "Connector plugin installed" | that check asks the worker for the plugin on its own | `pipeline_service.py` |
| Debezium MySQL 3.0.8 needs Java 17 | Dockerfile and catalog seed on 2.5.4 (MySQL 5.7 / 8.0); the running worker has 2.5.4 hand-installed again after the recreate | `infra/kafka-connect.Dockerfile`, `app/db/seed_templates.py` |

Every pipeline that existed is v1 and keeps exactly its config (checked live after the restart: the reconciler pushed no new config to any connector;
the only config PUTs were the two old bad-config test pipelines `gap-tf-jdbc-v2/v7`, which have never deployed). Tests: `tests/test_record_format_v2.py`
(44), migration HEAD 0056 + "existing rows v1, default v2"; 16 mutations, all caught. Backend 2,327 unit passed; real-DB 2,477 passed + the 2 known
(chain-from-empty, quality-job timing flake that passes alone). Live: wrong MySQL password → "Access denied", right one → "Authenticated to MySQL".

### Re-test, all v2 (2026-11-02): ALL PASSED

16 pipelines deployed at once (8 sources then 8 sinks) on the 2G worker: 0 OutOfMemoryError, 0 worker restarts, all connectors RUNNING.
Sources: `pg2my-{plain,quality,custom,cdc}-src` (Postgres `electronics_customers_live`, versions v5/v6/v7/v8) and `my2pg-{plain,quality,custom,cdc}-src`
(MySQL 8.0 `electronics_customers`); sinks `*-sink` into MySQL 8.4 `pg2my_*` / Postgres `my2pg_*`.

| Test | Result |
|---|---|
| Every source | exactly 141,000 records (no restart duplicates) |
| PG → MySQL plain / MySQL → PG plain | 141,000 rows = 141,000 row_ids; `row_id` PRIMARY KEY; money DECIMAL(65,2) / numeric, sums equal the source to the cent (PG 3071423019.29 / 3056618278.21 / 76344576.20, MySQL 3071423820.44 / 3056619867.55 / 76344027.03); dates DATE |
| Quality, both directions | 108,957 passed / 32,043 quarantined each (before: MySQL 0 passed); the row_id set in each target has the same MD5 as the rows that pass all 14 rules in SQL; 0 rule-breaking rows |
| Custom chain, both directions | 141,000 rows, every transform applied; sink inserts (the chain re-keys by order_id, so no upsert, no primary key) |
| CDC → database, both directions | snapshot 141,000 rows with PK; live: row X inserted then updated → arrives updated; row Y inserted then deleted → absent; row 12 changed and changed back → equal to the source; 141,001 rows = 141,001 ids = the source |
| Batch targets after those changes | X arrived (incrementing mode sees new rows only; it never sees a later update or delete, by design) |

Quality speed with two ENFORCE pipelines side by side: ~5,700 records/min each (~25 min for 141,000).

## Throughput: one source with a quality policy, then a sink to Postgres (2026-11-02)

`perf-pg-src` (JDBC incrementing, Postgres `sql_practice.electronics_customers_live`, 141,001 rows, `Electronics_Order_Check` attached in OBSERVE) →
topic `org_1001-raw-perf-pg-electronics_customers_live-batch-v1` (6 partitions) → `perf-pg-sink` (JDBC sink, upsert by row_id) → Postgres `perf_electronics`.
Run one after the other (sink deployed once the topic was complete), on the 2G Connect worker with the 16 re-test pipelines idle alongside. Measured
from Kafka's CreateTime on every record (source), 1-second samples of the table and the quality worker's running total, and Kafka Connect's JMX metrics.

| Stage | Records | Time | Speed |
|---|---|---|---|
| Postgres → Kafka (source) | 141,001 (2,754 bytes each with the embedded schema; 388 MB) | deploy→first record 0.36 s; first→last 1.14 s | ~123,000 rec/s average, 108,840 in the busiest second (~339 MB/s) |
| Kafka → Postgres (sink, upsert) | 141,001 rows | deploy→first row 0.7 s; first→last 5.0 s | ~28,000 rows/s average, ~33,000 peak |
| Quality worker (OBSERVE, 14 rules) | 141,001 evaluated, 32,044 failed (quarantined) | 492 s | ~287 rec/s |

End to end (source deploy → every row in the table): 13 s. Table = source (141,001 rows = 141,001 row_ids, same money sums). Kafka Connect's own rates
(Prometheus, 15 s scrape over a 30 s window): source write ≤3,266 rec/s, sink send ≤3,902 rec/s, sink put batch 9.3 ms avg / 67 ms max, sink lag max
23,486 — window averages, so they understate a load that finishes in seconds. The quality worker is ~100× slower than the data path.

Follow-up (same day): the Dashboard showed "Consumer lag 282.0k" with every sink idle and complete. It was the two ENFORCE quality sinks, 141,001
each: the backlog was measured with the sink's consumer group against the input topic in its settings, but an ENFORCE sink's connector reads
the prepared topic (its group had committed 108,957 of 108,957 there; it never reads the input topic, which only the quality worker does).
Fixed in `app/services/runtime_metrics.py` (`connector_input_topics`: the prepared topic for an ENFORCE sink); tests `tests/test_sink_backlog_topics.py`
(3, the reverted logic fails 2). Live after the fix: 26 sinks, total lag 0.
Then (his choice): an ENFORCE sink's lag also counts what its quality worker has not read yet (records reach the table only after the worker
passes them on). Sink backlog = the connector group on the prepared topic + the worker's group `inai-quality-<org>-<pipeline>` on the input
topic (`quality_mode.quality_consumer_group`, now also what the worker itself uses). OBSERVE sinks unchanged. `tests/test_sink_backlog_topics.py`
(7; 4 mutations all caught). Live: both quality workers committed 141,001/141,001, every sink 0.

### MySQL round closed (2026-11-02): test setup removed

At his request: the 16 re-test pipelines (`pg2my-*`, `my2pg-*`) deleted through the API (the Postgres CDC slot and publication went with them,
by `cdc_slot_cleanup`), their 14 Kafka topics, the target tables (MySQL `pg2my_*`, Postgres `my2pg_*`), their leftover DataConnect rows (2 quality
results, 32 lineage edges, 34 assets, 4 alerts with 16 events, 32 observations), the two auto transform policies, the connections `mysql-test` and
`mysql80-cdc`; the `mysql80` container, its volume `tb_mysql80_data` and the `mysql:8.0` image; the 8.4 `mysql` unit stopped
(`./test-backends.py down mysql`). Kept: the `test-backends_tb_mysql_data` volume (older than this round; holds `rm_customers` for the
`org_release_matrix` pipeline `rm-mysql-cdc-882592`), the `mysql:8` image, `perf-pg-src`/`perf-pg-sink`, the 20,000 `-LAG` rows, every code fix,
and Debezium MySQL 2.5.4 on the Connect worker (what the Dockerfile and the catalog now say). Backup: `~/.inaiera-local/backups/dataconnect-db-before-mysql-cleanup-20261102.sql`.

Found while closing the round: recreating the Connect container for the 2G heap (earlier the same day) had reset it to the image, so the
hand-installed Splunk S2S 2.3.12 and Syslog 1.5.17 plugins were gone and org_1001's `demo-splunk` / `demo-syslog` pipelines FAILED ("Failed to
find any class"). Reinstalled both from Confluent Hub, copied the licensing jars from Syslog into Splunk S2S (`NoClassDefFoundError
InvalidLicenseException` otherwise, as in CONNECTOR_TEST_SETUP.md), restarted Connect: both RUNNING; failures back to the three pre-existing
`org_release_matrix` REST tests. `~/.inaiera-local/connect-hand-plugins.sh` now restores every hand-installed plugin after a recreate.

## 1M rows: groceries_customers, with a 15-rule quality check (2026-11-02)

Data: his `groceries_customers_1M.xlsx` (the copy in Downloads; the one in the InaiEra folder is a 79 MB partial copy, not a valid zip) → CSV (2 min) →
`sql_practice.groceries_customers` (`row_id` bigint identity PK, money numeric(…,2) exact, loaded in 4 s): 1,000,000 rows, sums equal to the file.
Quality check `Groceries_Order_Check`, 15 rules (required, string length, two regexes, three allowed-values lists, seven ranges, a currency list);
worked out in SQL first: 481,519 rows pass, 518,481 fail ≥1 rule, of which 2,469 fail 4, 163 fail 5, 5 fail 6.

Run one phase at a time (`groc-*` pipelines, all record_format v2):

| Phase | Pipeline | Result |
|---|---|---|
| Postgres → Kafka | `groc-pg-src` (JDBC incrementing) | 1,000,000 records, deploy→first 0.3 s, first→last **8.5 s**, **~118,000 rec/s** (peak 126,553/s) |
| Kafka → Postgres, no quality | `groc-pg-sink` → `groceries_sink_all` (upsert by row_id) | deploy→first row 1 s, first→last **35 s**, **~28,500 rows/s** (peak 36,886/s) |
| CDC | `groc-cdc-src` (stream only, `snapshot_mode: never`) → `groc-cdc-sink` → `groceries_cdc` | insert `GRO-CDC-TEST` and update of row 5 (Delivered/3 → Shipped/4): 2 events, both rows in `groceries_cdc` with the new values (sink upserts) |
| Kafka → Postgres WITH quality (ENFORCE) | `groc-pg-quality-sink` → `groceries_sink_valid` | 1,000,001 checked in **5,440 s (~91 min)**: **~184 records checked/s**; passed → prepared topic **~89/s**, failed → quarantine **~95/s**; rows into the table **~87/s** |

Verified: `groceries_sink_valid` = 481,520 rows = the 481,519 rows that pass all 15 rules + the CDC test row (same MD5 of the row_id set), 0 duplicates,
0 rule-breaking rows; quarantine 518,481 records with exactly the SQL's distribution (2,469 × 4 rules, 163 × 5, 5 × 6) and per-rule counts.
Kafka Connect (Prometheus): source write ≤24,663 rec/s, plain sink send ≤25,102 rec/s (put batch ~9 ms), quality sink send ≤95 rec/s (put batch ~1 ms:
the sink waits for records, not for the database). With quality ENFORCE the data path is limited by the quality worker: each record is evaluated,
counted in the database and written to Kafka (prepared or quarantine) with a broker acknowledgement, one at a time; this data fails 52% (vs 23% for the
electronics data, which checked at ~287/s in OBSERVE), so more acknowledged quarantine writes. The local backend also runs LOG_LEVEL=DEBUG.
Pipelines, topics and tables kept as proof.

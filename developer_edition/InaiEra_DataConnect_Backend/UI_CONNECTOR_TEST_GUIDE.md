# DataConnect — UI Connector Test Guide

How to exercise **every source and sink connector through the UI** (http://localhost:3000),
in all aspects: connection → test → discover → pipeline (tables / snapshot / transforms /
quality) → deploy → verify data flow → lifecycle → cleanup.

> Connections, pipelines and policies you create in the UI are stored in DataConnect's own Postgres database
> (`dataconnect`, tables `dc_*`, container `infra-postgres-1`, `localhost:5435`), so they survive backend rebuilds.
> That database is the app's metadata store — **not** a test source or sink; don't point connections at it.

---

## 0. Prerequisites

### Services that must be running
```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -iE 'postgres|mysql|mongo|elastic|kafka|connect|frontend|backend'
```

| Service | Container | From your machine | From inside a container |
|---|---|---|---|
| Backend API | `infra-backend-1` | http://localhost:8001 | — |
| Frontend UI | `infra-frontend-1` | http://localhost:3000 | — |
| Kafka Connect | (infra) | http://localhost:8083 | `http://kafka-connect:8083` |
| PostgreSQL (test source/sink) | your own Postgres — not part of the stack | `localhost:5432` | `host.docker.internal:5432` |
| MySQL | `mysql` | `localhost:3306` | `host.docker.internal:3306` |
| MongoDB | `mongo` | `localhost:27017` | `host.docker.internal:27017` (Connections: host `mongo`, see §4) |
| Elasticsearch | `elasticsearch` (`./test-backends.py up elasticsearch`) | `localhost:9200` | `host.docker.internal:9200` |

**Golden rule:** connectors run **inside** Kafka Connect, so in every **Connection** you create,
use **`host.docker.internal`** as the host — never `localhost`.

### Credentials (local dev)
| System | User | Password | DB |
|---|---|---|---|
| PostgreSQL | your Postgres user | your password | `<your test database>` |
| MySQL | `dataconnect` (or `root`) | `dataconnect` (root too) | `dataconnect` |
| MongoDB | `dataconnect` | `dataconnect` | authSource `admin` |
| Elasticsearch | — | — (security disabled) | — |
| AWS S3 | your access key id | your secret (never commit it) | bucket + region |

### First, confirm which connector plugins are actually installed
Only installed plugins can deploy. List them:
```bash
curl -s http://localhost:8083/connector-plugins | python3 -c "import sys,json;[print(p.get('type'),p['class']) for p in sorted(json.load(sys.stdin),key=lambda x:x['class'])]"
```
Match the `class` column against the **Plugin class** column in the matrix below.

---

## 1. Connector test matrix

Legend: ✅ ready now (plugin installed + backing service running) · ⚙️ plugin installed,
you must supply the backing service · ❌ not installed locally / not feasible without a cloud account.

### Sources
| # | Template (UI name) | Plugin class | Backing service | Status |
|---|---|---|---|---|
| 1 | **JDBC Source** | `io.confluent.connect.jdbc.JdbcSourceConnector` | Postgres / MySQL | ✅ |
| 2 | **Debezium PostgreSQL CDC** | `io.debezium.connector.postgresql.PostgresConnector` | Postgres (`wal_level=logical`) | ✅ |
| 3 | **Debezium MySQL CDC** | `io.debezium.connector.mysql.MySqlConnector` | MySQL (binlog) | ✅ |
| 4 | **Debezium MongoDB CDC** | `io.debezium.connector.mongodb.MongoDbConnector` | Mongo **replica set** (enabled: `rs0`) | ✅ |
| 5 | **Debezium SQL Server CDC** | `io.debezium.connector.sqlserver.SqlServerConnector` | SQL Server + CDC | ❌ no arm64 image (Apple Silicon) |
| 6 | **Debezium Oracle CDC** | `io.debezium.connector.oracle.OracleConnector` | Oracle + LogMiner | ❌ no arm64 image (Apple Silicon) |
| 7 | **S3 Source** | `io.confluent.connect.s3.source.S3SourceConnector` | AWS S3 bucket | ✅ (needs AWS creds) |
| 8 | **Local File Source** | `org.apache.kafka.connect.file.FileStreamSourceConnector` | a file on the worker (installed) | ✅ |
| 9 | **GCS Source** | `io.confluent.connect.gcs.GcsSourceConnector` | Google Cloud Storage | ❌ verify/likely not installed |
| 10 | **Azure Blob Source** | `io.confluent.connect.azure.blob.AzureBlobStorageSourceConnector` | Azure | ❌ verify/likely not installed |
| 11 | **SFTP Source** | `io.confluent.connect.sftp.SftpSourceConnector` | SFTP server | ❌ verify/likely not installed |
| 12 | **REST API Source** | `com.github.castorm.kafka.connect.http.HttpSourceConnector` | any HTTP endpoint | ❌ verify/likely not installed |
| 13 | **Webhook Ingress** | `inai.connect.webhook.WebhookSourceConnector` | — | ❌ placeholder class, not a real plugin |

### Sinks (a sink consumes a topic — you must have a source feeding that topic first)
| # | Template (UI name) | Plugin class | Backing service | Status |
|---|---|---|---|---|
| 14 | **PostgreSQL Sink** | `io.confluent.connect.jdbc.JdbcSinkConnector` | Postgres / MySQL | ✅ |
| 15 | **Elasticsearch Sink** | `io.confluent.connect.elasticsearch.ElasticsearchSinkConnector` | Elasticsearch | ✅ |
| 16 | **S3 Sink** | `io.confluent.connect.s3.S3SinkConnector` | AWS S3 bucket | ✅ (needs AWS creds) |
| 17 | **Snowflake Sink** | `com.snowflake.kafka.connector.SnowflakeSinkConnector` | Snowflake account | ❌ verify/likely not installed |
| 18 | **Webhook Sink** | `inai.connect.webhook.WebhookSinkConnector` | — | ❌ placeholder class, not a real plugin |

**Bottom line — 9 templates testable end‑to‑end today:** JDBC Source, Debezium PostgreSQL,
Debezium MySQL, Debezium MongoDB, S3 Source, Local File Source, PostgreSQL/JDBC Sink,
Elasticsearch Sink, S3 Sink. SQL Server & Oracle can't run on this Apple‑Silicon machine
(x86‑only images); GCS/Azure/SFTP/HTTP/Snowflake need a plugin install + cloud account;
the two Webhook entries are placeholder classes — see §7.

> **Infra set up this session (lives inside containers — recreate them and it's gone):**
> the `connect-file` jar was added to the Kafka Connect worker's plugin path. MySQL and MongoDB are test
> sources started with `infra/test-backends.py up mysql mongo` (not by `docker compose up`); `mongo` runs as a
> single‑node replica set `rs0` with the login `dataconnect` / `dataconnect` (auth database `admin`), and
> `up` initialises `rs0` automatically. See `infra/TEST_BACKENDS.md`.

---

## 2. The "all aspects" checklist (run this for each connector)

Tick these off per connector. Details/recipes in §4–§5.

- [ ] **A. Connection – create** (Connections → the source type → **+ New connection**)
- [ ] **B. Connection – Test** (the ✓/Test action → expect `status: ok` with server version + latency)
- [ ] **C. Connection – Discover** (lists tables/collections/objects/indices)
- [ ] **D. Pipeline – create** with the adaptive fields:
  - [ ] correct **Tables/Collections to ingest** (DB sources) — try one table, several, and `*`
  - [ ] **Initial load / snapshot** control (Debezium `snapshot.mode`, JDBC `load_mode`, Mongo `startup.mode`)
  - [ ] **Transformations** with field targets (MaskField/ReplaceField/…); on CDC confirm auto‑unwrap
  - [ ] **Quality policy** attached (optional) → auto‑runs on a live sample at create
- [ ] **E. Schema** — confirm a schema version was auto‑introspected (Schemas tab)
- [ ] **F. Deploy** (Pipelines → ▶/Deploy) → expect status **deployed**, then **running**; a deploy error now shows the **real Kafka Connect message**, not a blank 500
- [ ] **G. Data flow** — Pipelines page shows **records + % ingested**; verify in the destination (§6)
- [ ] **H. Transform result** — inspect the topic/destination: fields masked/renamed/dropped as configured
- [ ] **I. Lifecycle** — **Pause → Resume → Restart**; watch silent auto‑refresh (5 s, no blink) update state/%
- [ ] **J. Quality** — Quality tab shows the auto‑run result; also try manual **Execute** with sample records
- [ ] **K. Delete** — remove the pipeline; (for Debezium Postgres also drop the replication slot, §8)

---

## 3. Source → topic → sink chaining (important for sinks)

A **sink** reads an existing Kafka topic; it can't be tested alone. Always test in this order:

1. Deploy a **source** pipeline (e.g. JDBC Source on `employees`). Note its **topic name**
   (e.g. `raw-sales-sample-employees-batch-v1`) shown on the Pipelines page.
2. Create a **sink** pipeline; in **Source topic to read** paste that exact topic; set **Entity**
   to the destination table/index/collection name.
3. Deploy the sink → verify rows land in the destination (§6).

---

## 4. Standard recipe — fields to enter in the UI

### Connections (use `host.docker.internal` as host!)
| Source type | Fields to enter |
|---|---|
| postgres | host `host.docker.internal`, port `5432`, database `<your test database>`, your Postgres user and password |
| mysql | host `host.docker.internal`, port `3306`, database `dataconnect`, user `dataconnect`, pass `dataconnect` (root password is also `dataconnect`) |
| mongodb | host **`mongo`**, port `27017`, database `<database>` (created on first write), user `dataconnect`, pass `dataconnect`, auth database `admin` (RS `rs0`, auth **enabled**) |
| elasticsearch | host `host.docker.internal`, port `9200` (no auth) |
| s3 | bucket `<your-bucket>`, region `<bucket-region, e.g. us-east-2>`, access_key_id, secret_access_key, prefix (optional folder) |
| file | path `/tmp/dc-file-source.txt` (a sample file already created **on the Kafka Connect worker**) |

> Mongo is the one exception to the `host.docker.internal` rule: its replica set advertises the
> member as `mongo:27017`, so use host **`mongo`** (the container name on the shared network).

### Pipeline identity (Topic identity section)
All lowercase `a-z 0-9 _ -`. Example: zone `raw`, domain `sales`, source_system `sample`,
entity `employees`, mode `batch` (or `cdc` for Debezium), version `v1`.
The produced topic becomes `raw-sales-sample-employees-batch-v1`.

---

## 5. Concrete per‑connector recipes (the ✅ ones)

### 5.1 JDBC Source (Postgres) — flat rows, easiest
1. Connection: **postgres** (see table above).
2. Pipeline: template **JDBC Source**, connection = that, entity `employees`,
   **Tables** blank (or `employees, orders_info`, or `*`), **Load mode** `auto`.
3. Deploy → Pipelines shows 6 records / 100 %.
4. Verify the topic:
   ```bash
   docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
     --topic raw-<...>-employees-<...> --from-beginning --max-messages 3 --timeout-ms 8000
   ```
5. **Transforms aspect:** add ReplaceField → drop `is_active`, rename `salary:package`;
   redeploy a fresh pipeline; confirm the topic rows have no `is_active` and `salary`→`package`.

### 5.2 JDBC Source (MySQL)
Same as 5.1 but connection **mysql**, database `dataconnect`. Good for testing multi‑table + `*`.

### 5.3 Debezium PostgreSQL CDC — change events, tests snapshot + unwrap
1. Connection: **postgres**.
2. Pipeline: template **Debezium PostgreSQL CDC**, entity `employees`, mode `cdc`,
   **Snapshot mode** `initial` (try `no_data`, `when_needed` too), **Tables** `employees`.
3. Deploy → the initial snapshot loads existing rows; then `INSERT/UPDATE/DELETE` in Postgres
   appear as change events:
   ```bash
   psql -h localhost -U <user> -d <database> -c \
     "INSERT INTO employees(first_name,last_name,email,department,salary) VALUES('Test','User','t@corp.com','Sales',50000);"
   ```
4. **Transform + unwrap aspect:** add a field transform → the app auto‑adds `ExtractNewRecordState`,
   so the topic shows **flat** rows (not the `{op,before,after}` envelope). Numbers come through
   readable (decimals emitted as doubles).
5. Cleanup: delete pipeline **and** drop the replication slot (§8).

### 5.4 Debezium MySQL CDC
Connection **mysql**, template **Debezium MySQL CDC**, entity a table in `dataconnect`, mode `cdc`.
Same snapshot/unwrap aspects as 5.3. (No replication slot to clean up; it uses binlog.)

### 5.5 S3 Source
1. Connection: **s3** with your bucket, correct **region**, keys, and a **prefix** (folder) that
   contains JSON objects.
2. Pipeline: template **S3 Source**, entity `<label>`, mode `batch`. (No "tables" field — the note
   explains it reads objects from the bucket/prefix.)
3. Deploy → objects under the prefix stream into the topic. Verify with the console‑consumer.
   *Region must match the bucket's real region, or S3 returns a redirect error (now shown clearly).*

### 5.6 Local File Source
1. Put a file on the Kafka Connect worker (or a mounted path).
2. Connection: **file** with `path` = that file.
3. Pipeline: template **Local File Source**. Each line becomes a record on the topic.
   *Verify the `FileStreamSourceConnector` plugin is listed (§0); some images omit it.*

### 5.7 PostgreSQL / JDBC Sink
1. First deploy a **source** producing a topic with data (5.1).
2. Connection: **postgres** (the sink can write to the same or another DB).
3. Pipeline: template **PostgreSQL Sink**, **Source topic to read** = the source's topic,
   entity `employees_copy` (destination table — auto‑created).
4. Deploy → verify:
   ```bash
   psql -h localhost -U <user> -d <database> -c "SELECT count(*) FROM employees_copy;"
   ```

### 5.8 Elasticsearch Sink
1. Source topic with data (5.1).
2. Connection: **elasticsearch**, host `host.docker.internal`, port `9200`.
3. Pipeline: template **Elasticsearch Sink**, **Source topic** = the source's topic, entity = index name.
4. Deploy → verify:
   ```bash
   curl -s localhost:9200/_cat/indices?v
   curl -s "localhost:9200/<index>/_search?size=2&pretty"
   ```

### 5.9 S3 Sink
1. Source topic with data (5.1).
2. Connection: **s3** with your bucket + **correct region** + keys.
3. Pipeline: template **S3 Sink**, **Source topic** = the source's topic, entity = label.
4. Deploy → verify objects appear:
   ```bash
   aws s3 ls s3://<your-bucket>/ --recursive | tail
   ```

---

## 6. How to verify data in each destination
```bash
# Postgres
psql -h localhost -U <user> -d <database> -c "\dt"
psql -h localhost -U <user> -d <database> -c "SELECT * FROM <table> LIMIT 5;"
# MySQL
docker exec mysql mysql -uroot -pdataconnect dataconnect -e "SHOW TABLES; SELECT * FROM <table> LIMIT 5;"
# MongoDB
docker exec mongo mongosh -u dataconnect -p dataconnect --authenticationDatabase admin \
  --eval 'db.getSiblingDB("<database>").<collection>.find().limit(5)'
# Elasticsearch
curl -s localhost:9200/_cat/indices?v
# S3
aws s3 ls s3://<your-bucket>/ --recursive | tail
# Any Kafka topic
docker exec infra-kafka-1 kafka-console-consumer --bootstrap-server localhost:29092 \
  --topic <topic> --from-beginning --max-messages 5 --timeout-ms 8000
```

---

## 7. Connectors that need extra setup (⚙️/❌)

- **Debezium MongoDB CDC** — MongoDB must be a **replica set**. `infra/test-backends.py up mongo` starts
  `mongod --replSet rs0` and runs `rs.initiate()` for you, so it is ✅ once that is up.
- **Debezium SQL Server / Oracle** — plugins are installed, but you must run a SQL Server / Oracle
  instance (with CDC / LogMiner enabled) and point a connection at it.
- **GCS / Azure Blob / SFTP / REST API / Snowflake** — confirm the plugin is installed (§0). If not,
  install it (`confluent-hub install <owner/name:version>` into the Connect plugin path, then
  restart the worker) and create a connection with that provider's credentials.
- **Webhook Ingress / Webhook Sink** — these template classes (`inai.connect.webhook.*`) are
  **placeholders**, not real installed plugins; they will not deploy. Treat them as catalog entries only.

For any not‑installed plugin, the UI will now show a clear deploy error like
`Kafka Connect error (400): Failed to find any class that implements Connector … <class>`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Deploy shows `Kafka Connect error (400): …` | The message is Connect's own — read it; usually a missing/invalid config field or a plugin not installed. |
| Deploy shows connection refused / timeout | You used `localhost` in the connection. Use **`host.docker.internal`**. |
| Debezium Postgres deploy fails on redeploy | Old **replication slot** still exists. Drop it: `psql -h localhost -U <user> -d <database> -c "SELECT pg_drop_replication_slot('dbz_<entity>');"` |
| S3 `PermanentRedirect` / wrong region | Set the connection **region** to the bucket's real region. |
| Sink deploys but 0 records | The **Source topic to read** is wrong/empty. Copy the exact topic from the source pipeline row. |
| CDC transform didn't change fields | Field SMTs need flat records — the app auto‑unwraps CDC when you select a transform; make sure a transform is actually selected and the pipeline was **re‑created** (transforms apply at deploy). |
| Debezium **MySQL** task FAILS / 0 snapshot | The DB user needs **global replication privileges**. `infra/test-backends.py up mysql` grants them on a fresh database (`infra/test-backends-init/mysql/01-cdc-grants.sql`): `GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT, LOCK TABLES ON *.* TO 'dataconnect'@'%'`. An already-initialised data volume keeps whatever grants it had. |
| **Elasticsearch sink** — can't find my index | The ES sink names the index after the **topic**, not the entity. Look for an index named like `raw-<...>` : `curl -s localhost:9200/_cat/indices?v`. |
| Everything I made vanished | A normal rebuild keeps them (they live in Postgres). The backend is probably pointing at a different or freshly created database — check `DATACONNECT_DATABASE_URL` and that the `postgres-data` volume was not removed. |

---

## 10. Verified test run (this environment)

Full end‑to‑end pass across every feasible template — **all green**:

| Scenario | Result |
|---|---|
| JDBC Source (Postgres) + ReplaceField transform | ✅ 6 recs, `is_active` dropped, `salary`→`package` |
| Quality policy auto‑run on pipeline create | ✅ result stored (`failed 1/6`) |
| JDBC Source (MySQL) | ✅ 4 recs |
| **Debezium PG `snapshot=initial_only`** + insert after deploy | ✅ snapshot 6, insert **IGNORED** (correct) |
| **Debezium PG `snapshot=initial`** + insert after deploy | ✅ snapshot 7 → **8 streamed** (correct) |
| Debezium PG CDC auto‑unwrap + transform | ✅ flat rows with `package` |
| Debezium MySQL snapshot + stream | ✅ 5 → 6 streamed (after replication grant) |
| Debezium MongoDB snapshot + stream | ✅ 2 → 3 streamed |
| S3 Source deploy + run | ✅ RUNNING (0 objects if prefix empty) |
| Local File Source | ✅ 3 lines → topic |
| JDBC Sink (Postgres) | ✅ 8 rows written to `employees_sink` |
| Elasticsearch Sink | ✅ 8 docs indexed |
| S3 Sink deploy + run | ✅ RUNNING, writing to bucket |

---

## 9. Cleanup
```bash
# delete a connector directly on Kafka Connect
curl -s -X DELETE http://localhost:8083/connectors/<connector-name>
# list what's deployed
curl -s http://localhost:8083/connectors | python3 -m json.tool
# drop a Debezium PG slot
psql -h localhost -U <user> -d <database> -c "SELECT pg_drop_replication_slot('dbz_<entity>');"
```
In the UI, **Delete** on a pipeline removes it (and its connector). Delete connections when done.

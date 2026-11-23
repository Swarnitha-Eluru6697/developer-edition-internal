# Connector test backends

The external systems the connector templates are tested against (SQL Server, Splunk, Hadoop, Cassandra, …).
They are **not** part of the normal stack: together they are tens of GB of images and need several GB of RAM each, so
they live in their own compose file, [docker-compose.test-backends.yml](docker-compose.test-backends.yml), and are
started through a helper that keeps you inside what a laptop can run.

## The rules

| Tier | How many at once | Why |
|---|---|---|
| **Heavy** (18 units, 300 MB – 1.9 GB images, JVM/database engines) | **one**; starting another stops the first | A single one can want 1–4 GB of RAM |
| **Light** (18 units, ≤ 285 MB) | **three** (`TB_MAX_LIGHT` to change) | Small, but they add up |

Both rules are enforced by [test-backends.py](test-backends.py), not by compose itself — **always start units through it**.
A heavy unit and up to three light units may run together. Give Docker Desktop at least 8 GB of memory.

## Quick start

```bash
cd infra
docker compose up -d                      # main stack first: it creates the `dataconnect` network the backends join
./test-backends.py list                   # every unit: tier, size, group, whether its images are already downloaded
./test-backends.py up light-1             # three light units together (ftps, influxdb, qdrant)
./test-backends.py up mssql               # one heavy unit — stops whichever heavy unit was running
./test-backends.py down mssql             # when its test is done (data volumes are kept)
./test-backends.py down all --volumes     # everything, including the data
```

* An image that is not on your machine yet is **never downloaded without asking** (`--yes` to skip the question;
  `./test-backends.py pull mssql` downloads without starting).
* Container names are the hostnames the connection profiles use (`t1-mssql`, `namenode`, `t1-etcd`, …). Don't rename them.
* A *unit* is what you test one connector against. Some units are several containers started together:
  `hadoop` (namenode + datanode), `milvus` (milvus + etcd + MinIO), `firebase` (emulator + TLS front).

## Light groups (three that fit together, no port clashes inside a group)

| Group | Units |
|---|---|
| `light-1` | ftps, influxdb, qdrant |
| `light-2` | questdb, weaviate, aerospike |
| `light-3` | activemq, lightstreamer, diffusion |
| `light-4` | clickhouse, minio, aerospike-ee |
| `light-5` | gridgain, geode, cassandra |
| `light-6` | scylla, mysql, mongo |

You can also mix units freely (`up qdrant questdb ftps`). Ports to watch across groups: `influxdb` and `diffusion` both want 8086.
Among heavy units `vertica` and `yugabyte` both want 5433 — fine, because they never run together.

## MySQL and MongoDB

They used to start with the main stack; they are test sources, so they now live here as light units (`mysql`, `mongo`,
group `light-6`). Hostnames (`mysql`, `mongo`) and ports (3306, 27017) are unchanged, so existing connections keep working.
`docker compose up -d` in this folder no longer starts them — use `./test-backends.py up mysql mongo`.

* `mongo` runs as the single-node replica set `rs0` (change streams / Debezium MongoDB). `up` runs `rs.initiate()` the first time.
* `mysql` has the binary log on with `binlog_format=ROW`, and on a fresh volume `test-backends-init/mysql/01-cdc-grants.sql`
  gives the `dataconnect` user the global replication privileges the Debezium MySQL connector needs.
* Both use the throwaway login `dataconnect` / `dataconnect`.

## Heavy units (largest first)

splunk (1.9 GB), hadoop (1.9 GB, two containers), singlestore, vertica, yugabyte, milvus (+etcd, MinIO), firebase, timeplus,
localstack, mssql, cosmos, elasticsearch, couchbase, ignite3, littlehorse, neo4j, hbase, omnisci. Sizes are in `./test-backends.py list`.

* `littlehorse` needs the main stack's Kafka (`kafka:29092`).
* `mssql`, `hadoop`, `hbase`, `vertica`, `omnisci`, `singlestore`, `splunk`, `diffusion`, `geode` are **amd64-only images**:
  on Apple Silicon they run under emulation (slow to start).

## Certificates and fixtures

`./test-backends.py init` (run automatically by `up`) generates what cannot live in git into `infra/test-backends-data/`
(git-ignored): the FTPS and Cosmos DB certificates, and the Firebase emulator config. `up ftps` also seeds
`/ftp/dataconnect/employees.csv` (3 rows) and sets `require_ssl_reuse=NO` (Java FTPS clients cannot reuse the TLS
session on the data channel).

## Credentials

Every password/token is an environment variable with a throwaway default, e.g. `TB_MSSQL_SA_PASSWORD`,
`TB_SPLUNK_PASSWORD`, `TB_MINIO_ROOT_PASSWORD`, `TB_COSMOS_CERT_SECRET` (full list: `grep -o '\${TB_[A-Z0-9_]*' docker-compose.test-backends.yml`).
The defaults are the values the existing connection profiles and `docs/CONNECTOR_TEST_LOG.md` already use; they only protect
containers on your own machine. Override them in `infra/.env` if you share the host.

## What this does NOT give you

* **Data and DB setup.** A fresh unit is an empty server. Anything that was created by hand during testing (databases and
  tables, CDC enabled on SQL Server, sample rows, buckets, Cassandra keyspaces, HBase tables, …) is described per connector in
  [docs/CONNECTOR_TEST_LOG.md](../docs/CONNECTOR_TEST_LOG.md), not scripted here.
* **Pinned versions.** Several images are `:latest` (as they were when tested). Pin them before relying on a reproducible run.
* **Verification of every unit.** The helper's rules were exercised with `light-1` (FTPS over explicit TLS incl. file download,
  InfluxDB, Qdrant) and a heavy switch `neo4j` → `ignite3`. The other units were generated from the exact settings of the
  containers that were used for testing, but were not started again. `firebase` is additionally **unverified**: its connector
  test was blocked (see the T2 Firebase section of the test log), and its emulator config / TLS front are regenerated, not copied.

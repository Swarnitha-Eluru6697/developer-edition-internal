# Kafka Connect worker pools

Kafka Connect runs in **distributed mode**, one cluster per **workload** — each a *worker pool*:

| Pool | Id | Runs |
|---|---|---|
| CDC | `connect-cdc-pool` | log-based change data capture (Debezium) |
| JDBC | `connect-jdbc-pool` | polling and batch database sources |
| File | `connect-file-pool` | file and object-store sources |
| API | `connect-api-pool` | REST and webhook API sources |
| Stream | `connect-stream-pool` | queue and message-stream sources (JMS, MQ, syslog) |
| Sink | `connect-sink-pool` | **every** sink connector — and nothing else runs there |

Why separate: plugin isolation, failure isolation (a stuck CDC task cannot starve a sink), workload-specific scaling and
resource profiles, security segmentation (a pool holds only the credentials its connectors need), and connector upgrades one
pool at a time.

The definition lives in one place — `app/core/connect_pools.py` — and the worker config, the topology check and the
provisioning tool all read it; `tests/test_connect_pool_infra.py` fails if this directory drifts from it.

## How a connector gets its pool

From its **connector template** (`runtime_pool_type`), when the pipeline is created. It is never chosen by the client, and it
is fixed for the pipeline's life (a connector cannot move between Connect clusters): editing a *deployed* pipeline to a connector
in a different pool is refused — create a new pipeline. A draft may be retargeted freely.

## Internal topics

Each pool's cluster keeps its state in three topics, named per the design:

```
_inai_connect_<pool>_configs   1 partition   (Connect requires exactly one)
_inai_connect_<pool>_offsets   25 partitions (more than configs/status — every source task commits here)
_inai_connect_<pool>_status    5 partitions
cleanup.policy=compact   replication.factor=3   min.insync.replicas=2
```

`<pool>` is the short name: `cdc`, `jdbc`, `file`, `api`, `stream`, `sink`. Create them **before** the workers first start —
Connect would otherwise create them with its own defaults and never correct them afterwards:

```bash
python -m app.tools.provision_connect_topics                  # every pool that has its own cluster; idempotent
python -m app.tools.provision_connect_topics --pool connect-cdc-pool
python -m app.tools.provision_connect_topics --check          # report only; exit 1 if anything is off-design
```

It never alters an existing topic (partition/replication changes need a reassignment). `GET /runtime/connect-pools/topology`
reports the same check for a running system.

## Running them (dev)

```bash
cp infra/pools.env.example infra/.env
docker compose --profile pools up -d --build      # connect-topics-init first, then the six workers
```

Each worker image is built with `--build-arg POOL=<short>` and carries only that pool's plugins (see the table at the top of
`kafka-connect.Dockerfile`). This dev stack has one broker, so replication defaults to 1; on a cluster of 3+ brokers set
`CONNECT_INTERNAL_REPLICATION_FACTOR=3` and `CONNECT_INTERNAL_MIN_INSYNC_REPLICAS=2`.

Without `CONNECT_POOLS_JSON`, **every pool runs on the shared `kafka-connect` worker** — a single-cluster setup, unchanged.

## Moving a pool to its own cluster

Setting a pool's URL does **not** migrate connectors already running on the shared cluster. The reconciler sees them missing
from the new cluster; to avoid running two copies of a source (duplicate records, a contested replication slot) it **refuses to
create the second one** and shows the pipeline a runtime error until the original is gone. For each such connector:

1. pause it, and note its offsets (`GET /connectors/<name>/offsets` on the shared cluster);
2. delete it from the shared cluster;
3. (if it must resume where it stopped) the new cluster's copy can be given those offsets (`PATCH .../offsets` while stopped);
   the reconciler creates it on the pool's cluster on its next tick.

Pools nobody has connectors in yet can be switched freely.

## Connector names

`<orgid>--<domain>--<source_system>--<entity>--<mode>--<version>`, e.g. `org_1001--erp--sap--customers--cdc--v1`. The parts are
joined by `--`, so a part may not contain `--` or begin/end with `-` (it would make the name impossible to split).

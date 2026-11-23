# Daily setup — keep connections & pipelines every day

## Why you need this
The backend stores **connections and pipelines in memory**, so they disappear whenever the
backend process restarts (a rebuild, `docker compose down`, or a machine reboot). The Kafka
Connect connectors and the databases persist — only the app's *view* is lost. `bootstrap.py`
re-creates that view and re-deploys, idempotently.

## Every morning (or after any restart), run:
```bash
cd ~/Desktop/inai-era-dataconnect-v2
docker compose start                 # if containers are stopped (keeps data; no rebuild)
python3 bootstrap.py                 # restores infra + connections + pipelines
```
Then open http://localhost:3000 → Pipelines. All 12 pipelines show **deployed/running**.

`bootstrap.py` is **idempotent** — safe to run anytime. It skips whatever already exists and
only creates what's missing, so it never duplicates.

## What it restores
- **Infra** (only if missing): Local File connector jar on the worker, MongoDB replica set
  `rs0`, MySQL CDC replication grants, the worker sample file.
- **Connections**: `sample-postgres`, `sample-mysql`, `sample-mongo`, `local-elasticsearch`,
  `s3-inaiera`, `worker-file`.
- **Policies**: `reshape-emp` transform, `quality-emp` quality policy.
- **Pipelines** (created + deployed): the 12 test pipelines (JDBC PG/MySQL, Debezium
  PG-initial / PG-initial_only / MySQL / Mongo, S3 source, File source, feed, and JDBC/ES/S3 sinks).

## AWS credentials for the S3 connection
By default the script uses the keys from testing. To override (recommended), export first:
```bash
export AWS_ACCESS_KEY_ID=...       export AWS_SECRET_ACCESS_KEY=...
export DC_S3_BUCKET=your-bucket    export DC_S3_REGION=us-east-2    export DC_S3_PREFIX=folder/
python3 bootstrap.py
```

## Avoid losing state in the first place
- Use **`docker compose start` / `stop`** (keeps the container + its memory) instead of
  `down` / `up --build`. Only rebuild (`up --build backend`) when you changed backend code —
  and re-run `bootstrap.py` afterwards.
- A full `docker compose down` also removes the connect worker; `bootstrap.py` re-installs the file connector
  automatically. MySQL and MongoDB are no longer part of that stack: start them with
  `cd infra && ./test-backends.py up mysql mongo` (it also re-creates the Mongo replica set `rs0` if needed).

## Want it fully automatic?
Two options (ask and I'll wire either):
1. **Auto-run on boot** — a small script/`launchd` job (macOS) that runs `bootstrap.py` a
   minute after Docker starts.
2. **True persistence** — store connections & pipelines in Postgres so they survive restarts
   with no script. This is a backend change (bigger, but then you'd never run bootstrap again).

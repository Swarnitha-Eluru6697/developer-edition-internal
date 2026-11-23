# InAiEra FlinkSQL Runner — self-contained image

Runs FlinkSQL jobs **the way the dbt runner runs dbt: everything on its own image.**

Just as the dbt runner ships dbt + its adapters in one image and points at an
external warehouse, this image ships the **entire Flink compute engine plus the
Runner API in a single container**. There is no separate Flink cluster to stand
up — `docker run` the image and you have a working FlinkSQL runner.

```
┌──────────────────────── one image ────────────────────────┐
│  Apache Flink 1.19.1                                       │
│    ├─ JobManager     REST / Web UI   :8081                 │
│    ├─ TaskManager    (task slots)                          │
│    └─ SQL Gateway    REST            :8083                 │
│  + Kafka / Iceberg / JDBC / Avro connectors preinstalled   │
│                                                            │
│  FastAPI Runner API                  :8080                 │
│    ├─ job registry · validate · deploy · stop · metrics    │
│    └─ boots Flink, migrates the DB, then serves the API    │
└────────────────────────────────────────────────────────────┘
        │ connects out to external data systems ↓
   Kafka (source)   ·   Postgres (metadata DB)   ·   Redis (alerts)
```

Everything the runner *computes with* travels inside the image. Everything it
*connects to* (like a dbt warehouse) stays external.

---

## Quickstart — full local stack

Brings up the runner image alongside a Postgres, Kafka and Redis to talk to:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Then:

```bash
open http://localhost:8080/docs      # Runner API (Swagger)
open http://localhost:8081           # Flink Web UI (inside the image)
curl http://localhost:8083/info      # SQL Gateway health
curl http://localhost:8080/healthz   # → {"status":"ok","service":"flinksql-runner"}
```

First boot takes ~90s while Flink initialises. The API container's healthcheck
goes green once `/healthz` responds.

---

## Quickstart — the image on its own

Build once:

```bash
docker build -f docker/Dockerfile -t inaiera-flinksql-runner:latest .
```

Run it, pointing at your own external systems:

```bash
docker run --rm \
  -p 8080:8080 -p 8081:8081 -p 8083:8083 \
  -e DATABASE_URL="postgresql+asyncpg://user:pass@db-host:5432/flinksql" \
  -e KAFKA_BOOTSTRAP_SERVERS="broker-host:9092" \
  -e KAFKA_SECURITY_PROTOCOL="PLAINTEXT" \
  -e REDIS_URL="redis://redis-host:6379/3" \
  inaiera-flinksql-runner:latest
```

The container boots Flink + the SQL Gateway internally, runs the Alembic
migrations against `DATABASE_URL`, then serves the Runner API on `:8080`.

> The metadata DB is the one hard dependency (the schema uses Postgres-specific
> types). Migrations retry for ~60s at boot; if the DB never comes up the API
> still serves `/healthz` and `/readyz` reports the DB as down — it never
> silently pretends to be ready.

---

## What boots, in what order

`docker/entrypoint.sh` runs inside the container:

1. `start-cluster.sh` — Flink JobManager + TaskManager (standalone session mode)
2. `sql-gateway.sh start` — Flink SQL Gateway on `:8083`
3. waits for `:8081/overview` and `:8083/info` to answer
4. `alembic upgrade head` — applies the metadata schema (retries until the DB is reachable)
5. `uvicorn app.main:app` — the Runner API on `:8080` (foreground; owns the container lifetime)

Because Flink runs in the same container, the API defaults
`FLINK_GATEWAY_URL=http://localhost:8083` and `FLINK_REST_URL=http://localhost:8081`.

---

## Ports

| Port | Service |
|---|---|
| 8080 | Runner API (Swagger at `/docs`) |
| 8081 | Flink JobManager REST + Web UI |
| 8083 | Flink SQL Gateway REST |

## Key environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://flinksql:flinksql@localhost:5432/flinksql` | External Postgres metadata DB (required) |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka.dp-shared...:9092` | External Kafka |
| `KAFKA_SECURITY_PROTOCOL` | `SASL_SSL` | `PLAINTEXT` for local dev |
| `REDIS_URL` | `redis://localhost:6379/3` | Alert pub/sub (optional) |
| `FLINK_GATEWAY_URL` | `http://localhost:8083` | In-image gateway; override to use an external cluster |
| `FLINK_REST_URL` | `http://localhost:8081` | In-image JobManager |
| `INTERNAL_SERVICE_TOKEN` | — | Shared token for the internal `/validate` endpoint |
| `FLINK_PROPERTIES` | — | Extra Flink config appended to `flink-conf.yaml` (e.g. `taskmanager.numberOfTaskSlots: 8`) |

Full app configuration lives in [app/core/config.py](app/core/config.py).

---

## Tuning the embedded Flink

Slots, memory and checkpointing live in [docker/flink-conf.yaml](docker/flink-conf.yaml).
Bump them per container at runtime without rebuilding:

```bash
docker run ... -e FLINK_PROPERTIES="taskmanager.numberOfTaskSlots: 8
taskmanager.memory.process.size: 4096m" inaiera-flinksql-runner:latest
```

## Scaling out (when one image isn't enough)

The single image runs one TaskManager, so its capacity is bounded by
`taskmanager.numberOfTaskSlots`. For production throughput, point the Runner API
at an external Flink cluster instead by overriding `FLINK_GATEWAY_URL` and
`FLINK_REST_URL` — the same API image then acts as a thin control plane. The
combined project's Helm charts (`../inaiera-flinksql-combined/docker/helm`) cover
that topology.

## Authentication (X-001)

The runner accepts two kinds of caller on its user-facing routes (`/api/v1/jobs`, `/api/v1/kafka`), told apart by
how the token is signed. Both must carry the route's scope, and the caller's roles are then checked (RUN-FLINK-009).

| Caller | Token | Verified with |
|---|---|---|
| Workspace, on a user's behalf | short-lived delegated JWT: HS256, `aud` = `flinksql-runner`, one scope, `sub` = the user, `act.sub` = `workspace`, at most `DELEGATION_JWT_MAX_LIFETIME_SECONDS` (120) | `DELEGATION_JWT_SECRET` |
| External CI | control-plane service-principal JWT: RS256, `aud` = `flinksql-runner`, the route's scope | the control plane's JWKS |

Scopes: `flink.jobs.read` (list/get/status/metrics/deployments), `flink.jobs.edit` (create/update/delete/validate),
`flink.jobs.deploy` (deploy/stop/cancel/savepoint), `flink.catalog.read` (Kafka catalog and templates).
`CONTROL_PLANE_AUDIENCE` defaults to `flinksql-runner`; a token minted for Workspace (`workspace.inaiera`) is
refused (the chart refuses that value too). `POST /api/v1/validate` keeps its own scoped service token.

## Deployments freeze an execution manifest (X-004)

`POST /api/v1/jobs/{id}/deploy` freezes, in the same transaction that claims the generation, everything the deployment
will run: the **SQL text** and its sha256, the job's name, kind, engine (`sink_engine`), parallelism, restore savepoint
and target resource (`ucs_fqn`), the subject (who deployed), org and workspace, the Git **source** the SQL was imported
from (repository without credentials, ref, path, exact commit), the idempotency key and the runtime versions. It is stored
as `flink_deployments.manifest` with `manifest_sha256`; a trigger refuses to change either (or `sql_hash`) once set.

The submission, including a reconciler resume after a crash, reads **only** the manifest (its hash is re-checked first),
never the mutable job row: editing or renaming the job meanwhile does not change what runs, and a tampered manifest is
refused (the deployment fails, nothing is submitted). New SQL or a new commit means a **new** deployment.

* `GET /api/v1/jobs/{job_id}/deployments/{deployment_id}/manifest` returns the audit record (no credentials).
* `source` on `POST /jobs` / `PATCH /jobs/{id}` records where the SQL came from (`git_url`, `git_ref`, `sql_path`, a full
  40-hex `commit_sha`, `provider`). The Workspace service sets it from its own Git-import routes and strips a client-supplied
  one; editing `sql` without a new `source` clears it. Migration `0008_execution_manifest`.
* A deployment created by an older runner has no manifest and keeps the old behaviour (it fails if the job SQL changed).
* The shared shape across the platform: `InaiEra_workspace_backend/docs/EXECUTION_MANIFEST.md`.

## pg_lake sinks use a governed catalog, never a password in SQL (FL-001)

The old "pg_lake PostgreSQL sink" template put `${PG_LAKE_JDBC_URL}`, `${PG_USER}` and `${PG_PASSWORD}` in the job's SQL. Nothing
substituted them, and substituting them would have stored a database password in `flink_jobs.sql`, every deployment manifest and
API responses. Now the **runner owns the connection**:

* Configure it on the runner: `PG_LAKE_JDBC_BASE_URL` (e.g. `jdbc:postgresql://pg-lake:5432`), `PG_LAKE_DATABASE`, `PG_LAKE_USER`,
  `PG_LAKE_PASSWORD` (and optionally `PG_LAKE_CATALOG_NAME`, default `pg_lake`). In the chart: `pgLake.enabled`, `jdbcBaseUrl`,
  `database` and `credentialsSecretRef` (the user and password come from a Secret; the chart refuses to render without them).
  Use a least-privilege role: it is the identity every job writes as.
* The runner registers a named JDBC catalog in every SQL Gateway session (validation and deployment) **before** the user's
  statements run. SQL only names the catalog identity and writes to an **existing** table:

  ```sql
  INSERT INTO pg_lake.analytics.orders_sink SELECT id, amount FROM orders;   -- pg_lake.<database>.<table>
  ```
* Refused at validation **and** at deploy: SQL that writes a credential (an option such as `'password'`, `'…secret…'`, `'…token…'`,
  or a `password=` inside an option value), and any `${PLACEHOLDER}` that nothing substitutes. SQL that uses `pg_lake.…` on a runner
  without the four settings is refused with a clear message. The catalog statement (which carries the password) exists only on the
  runner -> Gateway call: it is never stored, returned, or logged, and errors from it are scrubbed.
* The SQL template `pg_lake PostgreSQL sink` now generates the `INSERT INTO pg_lake.{DATABASE}.{PG_TABLE} …` form.

## Workspace association is a vouched-for label, the registry is org-shared (FL-003)

Jobs are visible to every caller of the org with the Flink capability: access is **org + role capability**, never workspace.
`workspace_id` on a job is a grouping label (the `?workspace_id=` list filter is a filter, not isolation). The runner never trusts
a `workspace_id` in a request body: it is accepted only if it equals the `workspace_id` claim of the signed delegated token, which
the Workspace service sets only after checking that the caller owns that workspace. External CI (no claim) omits it; a body workspace
without a matching claim is `403 WORKSPACE_NOT_AUTHORIZED`. See `InaiEra_workspace_backend/docs/RESOURCE_SCOPING.md`.

## Credentials
Who owns which Kafka / Schema Registry / Polaris / pg_lake credential, what is enforced and what is still open: [CREDENTIAL_OWNERSHIP.md](CREDENTIAL_OWNERSHIP.md).

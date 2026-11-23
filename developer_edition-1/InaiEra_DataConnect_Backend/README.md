# InaiEra DataConnect

A multi-tenant data ingestion and pipeline management platform backed by **Confluent Kafka Connect**, **FastAPI**, **PostgreSQL**, and **React + Redux Toolkit**.

---

## Architecture

```
Users → React SPA → FastAPI (Python) → Kafka Connect REST API
                         │
                    PostgreSQL (PGML)
                         │
              Confluent Kafka ← Connector nodes → Kafka Topics → Database / Sink
```

**Source types supported:** RDBMS (PostgreSQL, MySQL, SQL Server, Oracle, DB2), NoSQL (MongoDB, Elasticsearch), File (FTP/S3/GCS/SFTP), HTTP/REST APIs

---

## Error handling

Every error response — whatever raised it — is the same JSON shape (`app/schemas/errors.py`'s `ErrorEnvelope`, rendered by the handlers in `app/main.py`):

```json
{
  "code": "connector_config_error",
  "message": "'username' is required for source type 'postgres'",
  "field_errors": [{"field": "connection_config.username", "message": "'username' is required for source type 'postgres'"}],
  "request_id": "b6f2...",
  "retryable": false,
  "details": {"source_type": "postgres"}
}
```

- **`code`** — a stable, machine-readable identifier a UI can switch on. The fixed set this codebase raises deliberately (`app/core/errors.py`'s `DataConnectError` subclasses): `validation_error`, `connector_config_error`, `source_connectivity_error`, `kafka_connect_error`, `schema_conflict`, `quality_rule_error`, `backend_unavailable`, `request_timeout` — plus a handful of infra failures mapped onto the same codes (a Postgres outage → `backend_unavailable`, an outbound timeout → `request_timeout`) and a status-derived fallback (`not_found`, `forbidden`, ...) for routes that still `raise HTTPException(...)` directly.
- **`field_errors`** — populated for validation-shaped failures (a request body that fails Pydantic validation, a connector config missing required fields, a malformed quality rule); `null` when the error isn't about specific fields.
- **`request_id`** — echoes the same `X-Request-Id` the response header carries, for tracing an issue back through the structured logs.
- **`retryable`** — the backend's own judgment on whether retrying the exact same request unchanged might succeed (a transient Kafka Connect/database failure) versus won't (bad input, a real conflict, a permission the caller doesn't have).
- **`details`** — additional structured, UI-safe context specific to `code`. Never raw exception internals or anything secret-shaped.

Not every one of the 8 domain codes has a real call site raising it yet — `source_connectivity_error` and `schema_conflict` are defined and fully wired into the exception-handling machinery (see `tests/test_error_envelope.py`), but no current service raises them: connection testing is deliberately tolerant-by-design (`ConnectionTestResult{status: "error", ...}` as normal 200 data, not an HTTP error — see `app/services/connection_adapters.py`), and schema-version compatibility checking (`validate_compatibility` in `app/services/schema_service.py`) is a stub that always returns `valid: True`, not a real check yet.

---

## Authentication — the north-south trust boundary (Gate 1)

This service sits behind Envoy, which has already validated the caller's
signed ticket, injected identity headers (`x-user-name`, `x-user-roles`, …)
and a per-data-plane **gateway secret**, and stripped any client-supplied
identity headers. Every API route enforces **Gate 1** via the shared
**`gateway-auth`** package (a pinned git dependency declared in
`pyproject.toml`):

- verify the gateway secret (constant-time) — a request that bypassed Envoy
  has no secret → **401 `request did not come through the gateway`**;
- extract the identity from the Envoy headers (Envoy base64-encodes the
  roles claim; the package decodes it);
- resolve the role against this service's vocabulary and apply the policy.

The per-service policy lives in `app/core/gateway.py`:

| Role (from the ticket) | Behavior |
| --- | --- |
| none / unknown | 403 `app_not_in_role` (Data Connect not in your role) |
| `reader` | GET allowed; writes → 403 `read_only_role` |
| `data-engineer`, `admin` | full read + write |

It's wired app-wide in `app/main.py`:
`app.include_router(api_router, prefix=settings.api_prefix, dependencies=[Depends(gateway_gate)])`.

> **Why a `gateway.py` at all if the package authenticates?** The package
> handles *authentication* (identical everywhere). `gateway.py` is the ~20
> lines of *this service's* authorization: its role names and its read/write
> rule. That is inherently per-service and can't live in the shared package.

The dependency is pulled from a private repo, so both local installs and
Docker builds need a GitHub read token:

```bash
pip install -e .                                            # needs repo read access
docker build --secret id=gh_token,env=GH_TOKEN -t dataconnect-backend .
```

---

## Operations docs

- [`docs/QUALITY_MODES.md`](docs/QUALITY_MODES.md) — OBSERVE vs ENFORCE: whether a quality policy only records failures or keeps them out of the prepared topic.
- [`docs/FAILURE_COUNTERS.md`](docs/FAILURE_COUNTERS.md) — how failure counts are kept exact across a crash, and what is still at-least-once.
- [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — supported / requires_configuration / unavailable for connectors and quarantine options, with stable reason codes.
- [`docs/ACCESS_CONTRACT.md`](docs/ACCESS_CONTRACT.md) — permission-based access (`permissions.json`, `GET /me`), and how the frontend's permission list is generated from it and kept from drifting. Mapping and route coverage: [`docs/auth/PERMISSIONS.md`](docs/auth/PERMISSIONS.md).
- [`docs/LINEAGE.md`](docs/LINEAGE.md) — what the lineage graph draws for a source pipeline (no policy, a policy and its quarantine topic, ENFORCE and the prepared topic) and how stale edges are removed.
- [`docs/SECRETS.md`](docs/SECRETS.md) — how credentials are stored today (Postgres + AES-256-GCM), key rotation, and the proposed Vault design.
- [`docs/SUPPLY_CHAIN.md`](docs/SUPPLY_CHAIN.md) — pinned images, `make audit` / `make sbom`, the security workflow and how findings are accepted.
- [`docs/MIGRATIONS.md`](docs/MIGRATIONS.md) — how a fresh database is bootstrapped, how upgrades run, and the previous-release test.
- [`docs/METRICS_POLICY.md`](docs/METRICS_POLICY.md) — the low-cardinality label policy every metric must follow.
- [`docs/SUPPORT_DIAGNOSTICS.md`](docs/SUPPORT_DIAGNOSTICS.md) — the redacted support bundle (`GET /diagnostics/bundle`, `python -m app.diagnostics`).

---

## Repository Structure

DataConnect is **two separate repositories**, not a monorepo — see
[`docs/REPOSITORY_MODEL.md`](docs/REPOSITORY_MODEL.md) for the full
rationale (independent release cadence, independent toolchains, and how
compatibility between them is checked in CI without a shared version
number). `infra/docker-compose.yml` in this repo already assumes this: it
builds the frontend from a **sibling clone**, `../../InaiEra_DataConnect_Frontend`
by default (override with `FRONTEND_DIR`) — so the two repos should be
checked out next to each other on disk:

```
dataconnect/
├── InaiEra_DataConnect_Backend/      ← this repo (FastAPI)
│   ├── app/
│   │   ├── api/
│   │   │   └── routes/             One file per resource domain
│   │   ├── core/
│   │   │   ├── config.py           Pydantic-settings (env-driven)
│   │   │   ├── gateway.py          Gate 1 policy (RoleConfig + gateway_gate) ← north-south
│   │   │   ├── logging.py          Structured JSON logging (structlog)
│   │   │   ├── tenant.py           tenant from the gateway principal (x-tenant-id) + validation
│   │   │   ├── gateway.py          Gate 1: gateway-auth protect_app (401), public health paths, dev principal
│   │   │   └── permissions.py      permissions.json loader + require("dataconnect:...") (403)
│   │   ├── db/
│   │   │   ├── base.py             SQLAlchemy ORM models
│   │   │   └── session.py          Async engine + session factory
│   │   ├── repositories/           SQLAlchemy repository layer
│   │   ├── schemas/                Pydantic request/response models
│   │   ├── services/               Business logic (one service per domain)
│   │   └── main.py                 FastAPI app factory (lifespan, middleware)
│   ├── alembic/                    Database migrations
│   ├── tests/                      Pytest unit tests
│   ├── infra/
│   │   └── docker-compose.yml      Full local stack — builds the frontend
│   │                               from a sibling clone (see above)
│   ├── docs/
│   │   ├── DESIGN_TRACEABILITY.md
│   │   └── REPOSITORY_MODEL.md
│   ├── .github/workflows/ci.yml    Lint, type check, test, Docker build,
│   │                               cross-repo API contract check
│   ├── Dockerfile
│   └── pyproject.toml
│
└── InaiEra_DataConnect_Frontend/    ← separate repo (React + Redux Toolkit + Vite)
    ├── src/
    ├── tests/                      Playwright unit + e2e suites
    ├── .github/workflows/ci.yml    Lint, type check, test, build, Docker build
    ├── Dockerfile
    └── package.json
```

---

## Quick Start

### Prerequisites

- Docker Desktop
- Python 3.11+
- Node 20+

### 1. Clone both repos as siblings

```bash
mkdir dataconnect && cd dataconnect
git clone https://github.com/AadhyaAnalyticsInc/InaiEra_DataConnect_Backend
git clone https://github.com/AadhyaAnalyticsInc/InaiEra_DataConnect_Frontend
cd InaiEra_DataConnect_Backend
cp .env.example .env
```

`infra/docker-compose.yml` (below) expects the frontend at
`../../InaiEra_DataConnect_Frontend` relative to itself — i.e. the two repos
as siblings, as above. Override with `FRONTEND_DIR=/path/to/frontend` if
your clones live elsewhere.

### 2. Start the full local stack

```bash
cd infra
docker compose up -d
```

Services available after ~30 seconds:

| Service | URL |
|---|---|
| Backend API | http://localhost:8000/docs |
| Frontend | http://localhost:5173 |
| Kafka UI | http://localhost:8090 |
| Kafka Connect | http://localhost:8083 |
| Schema Registry | http://localhost:8081 |
| PostgreSQL | localhost:5432 |

> **Connector test sources are not in this stack.** MySQL, MongoDB and the other external systems the connector
> templates are tested against start separately, one at a time or a few at a time, from
> `infra/docker-compose.test-backends.yml`:
> `cd infra && ./test-backends.py up mysql mongo` (see [infra/TEST_BACKENDS.md](infra/TEST_BACKENDS.md)).
> The MySQL and MongoDB connector tests need that first.

### 3. Run the backend locally (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

### 4. Run the frontend locally

```bash
cd ../InaiEra_DataConnect_Frontend
npm install
npm run dev
```

---

## Environment Variables

See [`.env.example`](.env.example) for a full list. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATACONNECT_ENV` | `development` | `development` / `staging` / `production` |
| `DATACONNECT_DATABASE_URL` | local postgres | AsyncPG connection string |
| `DATACONNECT_KAFKA_CONNECT_URL` | `http://localhost:8083` | Kafka Connect REST URL |
| `DATACONNECT_RUNTIME_DEPLOY_ENABLED` | `false` | Enable live Kafka Connect deployment |
| `DATACONNECT_QUALITY_STREAM_DEGRADED_AFTER_SECONDS` | `300` | A quality worker stuck on one record (the dead-letter topic is down) never commits past it, so its partition stalls. After this long the pipeline is reported **DEGRADED** with the reason; the connector is not restarted and no data is lost |
| `DATACONNECT_QUALITY_STREAM_BLOCKED_HEARTBEAT_SECONDS` | `10` | How often a stuck worker refreshes its heartbeat. A heartbeat older than three intervals means the worker died and the stall stops counting |
| `DATACONNECT_SECRET_BACKEND` | `env` | Where connection credentials are stored: `postgres` (or `env`, the same thing) = AES-256-GCM encrypted rows in PostgreSQL, written in the same transaction as the connection. `vault` is not implemented and is rejected at startup, as is anything else. `GET /health/ready` reports `secret_store` (encryption key works, secret table readable; no detail beyond `ok` and the backend name) |
| `TRUST_ENVOY_HEADERS` | `false` | `true` to trust Envoy's identity headers (Gate 1) — not prefixed, read directly by the gateway-auth package |
| `ENVOY_INTERNAL_SECRET` | _(empty)_ | must match the secret Envoy injects on this route — not prefixed, read directly by the gateway-auth package |
| `VITE_API_BASE_URL` | local backend | Frontend → backend API base URL |

---

## Operation results

`GET /operations/{id}` reports two independent outcomes. `status` is whether the **action** ran (`queued | running | succeeded | failed | stale`);
`result_status` is whether what it returned met its contract and size limit (`valid | invalid`, null while unfinished or when it failed).
An action can succeed while its result is malformed, and that is no longer hidden behind a plain success: the operation is recorded as
`status=succeeded`, `result_status=invalid`, `error_code=OPERATION_RESULT_INVALID`, `result=null`, and counted in
`dataconnect_operation_result_invalid_total{operation}`. The action itself took effect, so check the resource's state rather than
repeating the operation. `infra/prometheus/alerts.yml` alerts on it (`DataConnectOperationResultInvalid`).

## Quality execution jobs: missing input

A job's input records are staged in the database. The retention sweep never deletes them while the job is `queued` or `running`, and a job
that fails keeps them for a full TTL from the failure (so `POST /quality-execution-jobs/{id}/resume` can continue it). If the input is
gone anyway, retrying cannot recreate it: the job fails with `QUALITY_JOB_INPUT_MISSING`, `retryable=false`, and `available_actions` is
`["resubmit"]`. `resume` then answers `410` with the same code and `details.resubmit_path`. `POST /quality-execution-jobs/{id}/resubmit`
takes the records again (`{"records": [...]}`) and queues a NEW job for the same pipeline and policy, linked by `resubmitted_from`; only a
failed or stale job can be resubmitted.

## Quality worker stalls (alerting)

A quality worker never commits past a record it can neither handle nor write to the worker dead-letter topic; if that topic is down the
partition stalls (backpressure, never loss). `/metrics` makes that visible, with no pipeline, topic or error text in any label:

| Metric | Meaning |
|---|---|
| `dataconnect_quality_retries_total{connector_type,operation}` | Retried attempts of the `stats` or `quarantine` step |
| `dataconnect_quality_dlq_failures_total{connector_type}` | Failed writes to the worker dead-letter topic (each leaves the record uncommitted) |
| `dataconnect_quality_blocked_partitions` | Partitions that cannot advance because their worker is stuck |
| `dataconnect_quality_oldest_uncommitted_age_seconds` | Age of the oldest record a worker holds and has not committed |

Which pipeline is stuck is in the logs (`quality_stream.dead_letter_unavailable`) and on the pipeline itself: past
`DATACONNECT_QUALITY_STREAM_DEGRADED_AFTER_SECONDS` it shows **DEGRADED** with the reason, and returns to running when the worker moves on.

## Key Improvements (v0.1 → v0.2)

### Backend

| Area | Change |
|---|---|
| `main.py` | Uses `lifespan` context manager; adds request-ID middleware and global exception handlers |
| `config.py` | Added `env`, `app_version`, JWT settings, Redis URL, DB pool config |
| `tenant.py` | org_id format validated via regex; prepared for JWT RBAC |
| `connection_service.py` | `delete_connection` now org-scoped (bug fix); structured logging on all mutations |
| `pipeline_service.py` | State machine validation prevents invalid transitions; `delete_pipeline` org-scoped |
| `connection.py` (schema) | `source_type` validated against whitelist; `status` constrained |
| `pipeline.py` (schema) | `zone`, `mode`, `version` validated via field validators |
| `docker-compose.yml` | Full Confluent stack: Kafka, Zookeeper, Schema Registry, Kafka Connect, Kafka UI, Redis |
| `pyproject.toml` | Added `structlog`, `mypy`, `pytest-cov`, `prometheus-fastapi-instrumentator` (prod) |
| CI | Added mypy, coverage gate (70%), Docker build smoke test |

### Frontend

| Area | Change |
|---|---|
| `api.ts` | `X-Org-Id` from env; error interceptor; typed per-resource helper functions |
| `pipelinesSlice.ts` | Error state; lifecycle thunks (deploy/pause/resume); optimistic status update |

---

## Production Checklist

- [ ] Wire SQLAlchemy repository layer to all CRUD routes (replace in-memory store)
- [ ] Add JWT validation and RBAC using `DATACONNECT_JWT_SECRET`
- [ ] Set `DATACONNECT_RUNTIME_DEPLOY_ENABLED=true` and configure Kafka Connect URL
- [ ] Set a real `DATACONNECT_SECRET_ENCRYPTION_KEY` (credentials are stored encrypted in PostgreSQL; a Vault backend is not implemented)
- [ ] Add Prometheus/OpenTelemetry metrics (`prod` extras in pyproject.toml)
- [ ] Add Helm chart / Kubernetes manifests under `infra/k8s/`
- [ ] Set `DATACONNECT_ENV=production` (disables `/docs`, enables JSON logging)

---

## Running Tests

```bash
make test                       # from a fresh clone: creates .venv, installs ".[dev]" if pyproject.toml changed, runs pytest
make test PYTEST_ARGS="-k topic --no-cov"   # pass pytest arguments
make lint                       # ruff
```

`make test` is the supported entry point. Plain `pytest` / `python -m pytest` also work from any directory once the dev dependencies
are installed (`pip install -e ".[dev]"`): the repo root is on pytest's import path (`pythonpath` in `pyproject.toml`) and
`tests/conftest.py` sets the test environment before importing the app, so neither `pip install -e .` of the app itself, a
`PYTHONPATH`, nor a local `.env` is needed.

```bash
pytest --cov=app --cov-report=html    # with HTML coverage report
```

# InaiEra DataConnect — Backend Architecture (complete reference)

A code-verified, folder-by-folder reference for the `backend/app` service: what each layer
does, how a request flows through it, the domain model, and the end-to-end business flows
(pipeline build → deploy → lineage, streaming quality, schema drift).

> Scope: everything under `backend/app/`, plus how it's configured and run.
> Version `0.2.0`, FastAPI + Pydantic v2 + SQLAlchemy 2 (async) + structlog, Python 3.11.

---

## 1. What this service is

DataConnect is a **tenant-aware control plane for Kafka-Connect-based data ingestion**. Users
register *connection profiles* (sources) and pick a *connector template* (a Kafka Connect plugin),
then compose a **pipeline** that the system turns into a deterministic Kafka Connect connector
config. Around that core it layers **transform policies** (Kafka SMTs), **schema versions + drift
detection**, **data-quality policies** (batch + streaming), **data lineage**, and **runtime
monitoring**.

It does *not* move data itself. It **generates connector configuration** and (optionally) calls a
real Kafka Connect cluster to deploy it. With the runtime flag off (default) it is a pure
metadata/config service.

```
            ┌──────────────────────────────────────────────────────────┐
  Client →  │  FastAPI app (this service)                               │
            │  validate → org-scope → service logic → generate config   │
            └───────────────┬───────────────────────┬──────────────────┘
                            │ (flag on)              │ (flag on)
                            ▼                        ▼
                    Kafka Connect REST        Kafka REST Proxy
                    (deploy connectors)       (produce quarantine records)
```

---

## 2. Tech stack (`pyproject.toml`)

| Concern | Library |
|---|---|
| Web framework | `fastapi` (ASGI, run by `uvicorn`) |
| Validation / models | `pydantic` v2 + `pydantic-settings` |
| ORM / migrations | `sqlalchemy` 2 (async) + `alembic` + `asyncpg` |
| HTTP client (Kafka Connect / REST Proxy) | `httpx` |
| Logging | `structlog` |
| Secrets (optional) | `hvac` (Vault) |
| Tests / lint | `pytest`, `pytest-asyncio`, `ruff`, `mypy` |

Run targets: `uvicorn app.main:app` (Dockerfile exposes `:8000`; `Makefile` `backend` target adds
`--reload`).

---

## 3. Directory map of `app/`

```
app/
├── main.py                  # App factory: middleware, exception handlers, router mount, lifespan
├── api/
│   ├── router.py            # Aggregates every route module under prefixes
│   └── routes/              # One module per domain (thin HTTP layer)
│       ├── health.py        catalog.py     connections.py  pipelines.py
│       ├── transforms.py    schemas.py     quality.py       lineage.py
│       └── monitoring.py
├── core/
│   ├── config.py            # Settings (env-driven, DATACONNECT_ prefix)
│   ├── tenant.py            # tenant from the gateway principal (x-tenant-id) + validation (get_org_id)
│   └── logging.py           # structlog configuration
├── schemas/                 # Pydantic request/response models (the API contract)
│   ├── common.py  catalog.py  connection.py  pipeline.py  transform.py
│   ├── schema.py  quality.py  lineage.py  monitoring.py
├── services/                # Business logic (the brain) — pure-ish, framework-light
│   ├── catalog_service.py        connection_service.py    connection_adapters.py
│   ├── pipeline_service.py       pipeline_config.py       topic_naming.py
│   ├── transform_policy.py       schema_service.py        schema_inference.py
│   ├── schema_drift.py           quality_service.py       quality.py
│   ├── streaming_quality_processor.py                     lineage_service.py
│   ├── lineage_event_consumer.py monitoring_service.py
│   ├── runtime_orchestrator.py   kafka_runtime.py
├── storage/
│   └── memory.py            # In-memory store + seed data (the ACTIVE persistence today)
├── db/
│   ├── base.py              # SQLAlchemy declarative base + mixins (+ legacy model block)
│   └── session.py           # Async engine + session factory + get_session dependency
├── models/                  # SQLAlchemy ORM models, one file per domain (the FUTURE persistence)
└── repositories/            # Async repository pattern over the ORM models
    ├── base.py  dataconnect.py
```

### The single most important architectural fact

There are **two persistence layers in the codebase, and only one is wired into the request path**:

- **Active today:** `app/storage/memory.py` — a process-global in-memory dict store. **Every
  service imports and uses `store` from here.** Data resets on every restart and is not shared
  across processes/replicas.
- **Built but dormant:** `app/db/` (async engine/session), `app/models/` (full ORM schema), and
  `app/repositories/` (async repos). The Postgres schema, FK relationships, unique constraints, and
  Alembic migrations all exist, but the service methods **do not call them** (e.g.
  `connection_service.py`'s docstring says "uses SQLAlchemy repository" but the body calls
  `store.*`). This is the intended production path, staged for cut-over.

Keep this in mind reading the rest: **business behavior = `services/` + `storage/memory.py`**;
**`db/` + `models/` + `repositories/` = the schema-of-record / migration target.**

---

## 4. Request lifecycle (how a call flows through the layers)

```
HTTP request
   │
   ├─► request_id_middleware           (main.py)  bind X-Request-Id into structlog context
   │
   ├─► CORS middleware                 (main.py)
   │
   ├─► Router match                    (api/router.py → routes/<domain>.py)
   │
   ├─► Dependency: get_org_id          (core/tenant.py)  ─ 400 if header missing/invalid
   │
   ├─► Pydantic body validation        (schemas/<domain>.py field_validators)
   │        └─ on failure → RequestValidationError → 422
   │
   ├─► Route handler                   (routes/…) — thin: delegates to a service
   │
   ├─► Service logic                   (services/…) — reads/writes storage.memory.store,
   │        builds config, runs validators, (optionally) calls runtime over httpx
   │
   └─► Response model serialization    (schemas/…)  → JSON
```

### Cross-cutting concerns (all in `main.py` / `core/`)

- **App factory** `create_app()` (`main.py`): builds `FastAPI`, adds CORS + request-id middleware,
  registers exception handlers, mounts `api_router` under `settings.api_prefix`
  (`/api/v1/dataconnect`), wires the `lifespan` (configures logging on startup, disposes the DB
  engine on shutdown). `docs_url`/`redoc_url` are disabled when `env == "production"`.
- **Tenancy** (`core/tenant.py`): `get_org_id` is a FastAPI dependency that reads the gateway principal's tenant (`x-tenant-id`; `X-Org-Id` is ignored),
  enforces regex `^[A-Za-z0-9_-]{3,100}$`, and 400s otherwise. Every org-scoped route depends on it.
- **Exception handlers** (`main.py`):
  - `ValueError` → **422** `{"detail": str(exc)}` (services raise plain `ValueError` for domain
    rule violations, e.g. topic-segment casing).
  - `RequestValidationError` → **422** `{"detail": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})}`
    — the `custom_encoder` is required because Pydantic v2 puts the raised `ValueError` object in
    the error `ctx`, which is otherwise not JSON-serializable.
  - `Exception` (catch-all) → **500** `{"detail": "Internal server error"}`.
- **Logging** (`core/logging.py`): structlog with contextvar merging; `ConsoleRenderer` in
  development, `JSONRenderer` otherwise. Services log events like `pipeline.deployed`.
- **Settings** (`core/config.py`): pydantic-settings, env prefix `DATACONNECT_`, `.env` supported.
  Notable flags: `runtime_deploy_enabled` (default **false** → no real Kafka calls),
  `topic_soft_limit=180`, `kafka_connect_url`, `kafka_rest_proxy_url`, `secret_backend`.

---

## 5. Folder-by-folder deep dive

### 5.1 `api/` — the HTTP layer

`api/router.py` aggregates all route modules and assigns prefixes:

| Module | Prefix | Org-scoped? |
|---|---|---|
| `health` | *(none)* | no |
| `catalog` | `/connector-templates` | **no** |
| `connections` | `/connections` | yes |
| `pipelines` | `/pipelines` | yes |
| `transforms` | `/transform-policies` | yes |
| `schemas` | `/schemas` | yes (except `/infer`) |
| `quality` | *(none — paths self-prefixed)* | yes |
| `lineage` | *(none)* | yes |
| `monitoring` | `/runtime` | **no** |

The whole router is then mounted under `settings.api_prefix`, so the public base is
`/api/v1/dataconnect`. Route handlers are deliberately **thin**: validate (via Pydantic + the
`get_org_id` dependency), call a service, return. Domain logic does not live here. A couple of
routes embed small logic exceptions:
- `transforms.py` calls `generate_smt_config(...)` directly to validate on create/update.
- `quality.py` catches service `ValueError` and re-raises as **400** (so bad quality rules are 400,
  not the global 422).
- `lineage.py` overwrites a mismatched body `org_id` with the header value (header wins).

### 5.2 `core/` — configuration, tenancy, logging
Covered in §4. These are the framework-facing primitives every request touches.

### 5.3 `schemas/` — the API contract (Pydantic v2)

One module per domain. These define request bodies and response models and carry **field-level
validation** that produces 422s. Key validators:

- `connection.py`: `source_type` must be in `SOURCE_TYPES` (18 values); `status` ∈
  {draft, active, inactive, error}.
- `pipeline.py`: `zone` ∈ 5 values, `mode` ∈ 7 values, `version` matches `^v[0-9]+$`.
- `common.py`: shared `ValidationResult {valid, errors}`, `Timestamped`, `OrmModel`
  (`from_attributes=True`).

Response models intentionally differ from create models (e.g. `Pipeline` adds server-computed
`id, org_id, topic_name, connector_name, generated_config`).

### 5.4 `services/` — the business logic

This is the brain. Services are plain singletons (`xxx_service = XxxService()` at module bottom)
that operate on `storage.memory.store`. Grouped by domain:

**Catalog** — `catalog_service.py`: thin CRUD over `connector_templates`. No org scoping (templates
are global). No validation beyond Pydantic (template fields are free-form strings/dicts).

**Connections** — `connection_service.py` + `connection_adapters.py`:
- CRUD with org guards. `create` stamps `org_id`; `get_or_404` enforces org match.
- `test_connection`/`discover_objects` pick an **adapter** by `source_type` via
  `adapter_for_source_type()`:
  - `DatabaseConnectionAdapter` — requires `hostname` + `database`; discover reads `tables`.
  - `ApiConnectionAdapter` — requires `base_url`/`url` (http/https); if `health_path` present it
    makes a **real outbound `GET`**; discover reads `endpoints`.
  - `FileConnectionAdapter` — requires `bucket`/`path`/`container`; discover reads `objects`.
  - Fallthrough is the DB adapter, so `mongodb/elasticsearch/http/ftp` (valid source types not in
    the explicit adapter map) require `hostname`+`database` for `/test`.

**Pipelines** — the heart of the system. Three collaborating modules:
- `topic_naming.py` — the **7-part naming convention** and its validation.
  - Topic = `org.zone.domain.source_system.entity.mode.version` (dot-joined).
  - Connector = `org--domain--source_system--entity--mode--version` (double-dash, *zone omitted*).
  - Each segment must match `^[a-z0-9_-]+$` (lowercase), zone/mode must be in their allowed sets,
    version `^v[0-9]+$`, and the topic must stay under `topic_soft_limit` (180). Violations raise
    `ValueError` → surfaced as 422.
- `pipeline_config.py` — `generate_pipeline_config()` deterministically builds the Kafka Connect
  config by **layering dicts in precedence order**:
  ```
  template.default_config
    → connection.connection_config
      → pipeline.connector_config
        → generated SMT config (from transform policy)
          → DEFAULT_ERROR_HANDLING_CONFIG
            → computed keys: name, connector.class, topics, topic.prefix,
              errors.deadletterqueue.topic.name (the quarantine/validation topic),
              and inai.secret.ref (if the connection has a secret_ref)
  ```
  Returns `{topic_name, connector_name, generated_config}`.
- `pipeline_service.py` — CRUD + **lifecycle state machine** + runtime delegation.
  - `_build_pipeline_row` resolves and **validates references**: template must exist (else 404),
    connection must exist *and* match org (404), transform policy (if given) must match org (404),
    then calls `generate_pipeline_config`.
  - **State machine** `VALID_TRANSITIONS`:
    ```
    draft       → {deployed}
    deployed    → {paused, running, failed}
    running     → {paused, restarting, failed}
    paused      → {running, restarting}
    restarting  → {running, failed}
    failed      → {deployed, restarting}
    ```
    Illegal transitions → **409**. `deploy/pause/resume/restart` each assert the transition, then
    (only if `runtime_deploy_enabled`) call the `RuntimeOrchestratorService`; otherwise they just
    flip the stored status and return a synthetic runtime block.
  - `deploy` additionally calls `lineage_service.materialize_pipeline_lineage(...)` — **deploy is
    what creates lineage assets/edges.**

**Transforms** — `transform_policy.py`: maps friendly SMT type names → Kafka Connect SMT classes
(`SMT_TYPES`), enforces scope (`key|value|header`), rejects `NON_SMT_TRANSFORMATIONS`
(join/aggregation/…) and unknown types (raise → 422), and emits the flattened
`transforms.<name>.<key>` config dict via `generate_smt_config()`.

**Schemas & drift** — `schema_service.py`, `schema_inference.py`, `schema_drift.py`:
- `schema_inference.infer_schema` — infers field name/type/nullable/confidence from sample records;
  `_detect_type` recognizes bool/int/number/object/array and ISO-8601 strings as `timestamp`.
- `schema_drift.compare_schema_fields` — diffs an approved field list against observed fields and
  emits **drift events** each carrying a `DriftAction`:
  `allow_and_notify` (new optional), `hold_for_approval` (new required), `quarantine`
  (type changed), `route_to_schema_review` (nested/object change), `alert` (field removed),
  `block_deployment` (primary key changed). `compatible = not any(blocking)` where blocking =
  {block_deployment, hold_for_approval}. `approval_required` = any {hold_for_approval,
  block_deployment}.
- `schema_service` — CRUD + `detect_drift` (infers observed, compares, sets approval_required) +
  drift-approval lifecycle (create/list/approve/reject), all over the in-memory store.

**Quality** — `quality.py` (pure functions) + `quality_service.py` (orchestration) +
`streaming_quality_processor.py`:
- `validate_quality_policy` — checks each rule `type` is one of 10 supported and compiles `regex`
  patterns; returns `{valid, errors}`. `quality_service.create/update` raise `ValueError` on
  invalid → route turns it into **400**.
- `evaluate_record`/`evaluate_batch` — actually evaluate records. **Only 6 of the 10 rule types are
  evaluated** (`required, type_check, regex, allowed_values, min_max_numeric,
  min_max_string_length`); the other 4 (`timestamp_freshness, null_percentage_threshold,
  duplicate_key_batch, referential_hint`) are accepted/validated but not enforced at runtime.
  `min_max_numeric` guards `float()` so non-numeric values fail the rule instead of crashing.
- `execute_policy` — batch evaluate + build a `QualityResult` summary (with quarantine envelopes for
  failures) and persist it.
- `StreamingQualityProcessor.process_record` — per-record: if valid → routes to the *prepared* topic
  (`.raw.`→`.prepared.`); if invalid → builds a quarantine envelope and **produces it to the
  quarantine topic** via a `KafkaProducer` (the route injects an `InMemoryKafkaProducer`).

**Lineage** — `lineage_service.py` + `lineage_event_consumer.py`:
- `materialize_pipeline_lineage` (called on deploy) upserts 5 assets (source → connector → raw
  topic → quality processor → prepared topic) and 4 typed edges between them.
- `graph_for_asset` / `graph_for_pipeline` read assets+edges back as a graph.
- `consume_runtime_event` upserts assets/edges from an external runtime event (idempotent by
  `qualified_name` / edge identity). `LineageEventConsumer` is the (unwired) Kafka-consumer driver
  for the same.

**Monitoring** — `monitoring_service.py`: returns **static/derived** data — three hard-coded connect
clusters, mock connector status/tasks (always `RUNNING`), and a `metrics_summary` derived by
counting pipelines in the store. No org scoping.

**Runtime adapters** — only used when `runtime_deploy_enabled`:
- `runtime_orchestrator.py` — `KafkaConnectClient` (httpx wrapper over the Connect REST API:
  deploy/pause/resume/restart/delete/status) and `RuntimeOrchestratorService` mapping pipeline
  lifecycle to those calls.
- `kafka_runtime.py` — `KafkaProducer` protocol with `InMemoryKafkaProducer` (test/offline) and
  `RestProxyKafkaProducer` (real, via Kafka REST Proxy).

### 5.5 `storage/memory.py` — the active store

`InMemoryStore`: a `defaultdict` of `collection → {id → row}` with `list/get/create/upsert/delete`
(deep-copying in and out to avoid aliasing). `list(collection, org_id=...)` filters by org. On init
it **seeds** three rows used by tests/demos:

| Collection | Seeded id | Notes |
|---|---|---|
| `connector_templates` | `tpl_debezium_postgres` | global |
| `connection_profiles` | `conn_demo_salesforce` | `org_id=org_1001`, `source_type=api` |
| `transform_policies` | `tfp_customer_basic_v1` | `org_id=org_1001` |

A module-level `store = InMemoryStore()` singleton is imported everywhere.

### 5.6 `db/`, `models/`, `repositories/` — the production persistence (dormant)

- `db/base.py` — `Base` declarative class + `UuidPk`/`TimestampMixin` mixins. (It also still
  contains a **legacy inline copy** of every table, kept for Alembic import compatibility while the
  per-domain `models/` modules take over.)
- `db/session.py` — async engine (`create_async_engine`, pool sizing from settings, `pool_pre_ping`)
  + `async_sessionmaker` + a `get_session` dependency that commits on success / rolls back on error.
- `models/` — one ORM module per domain, registered in `models/__init__.py`. Tables are prefixed
  `dc_` and carry real **FK relationships and unique constraints** — e.g. `Pipeline` enforces
  `UNIQUE(org_id, topic_name)` and `UNIQUE(org_id, connector_name)` (preventing duplicate
  connectors that would corrupt Kafka). Entities: Organization, ConnectorTemplate,
  ConnectionProfile, TransformPolicy, SchemaVersion, SchemaDriftApproval, QualityPolicy, Pipeline,
  PipelineRun, Asset, LineageEdge, QualityResult, AuditLog.
- `repositories/` — `SqlAlchemyRepository` (generic async CRUD) and `OrgScopedRepository`
  (`list_for_org` / `get_for_org`), plus per-entity repos with domain finders
  (`find_by_connector_name`, `find_contract`, `list_pending`, `get_by_qualified_name`, …).

These mirror the in-memory collections one-to-one, so cut-over means swapping `store.*` calls for
repository calls inside services.

---

## 6. The 9 functional domains (maps to the Postman folders)

| # | Domain | Endpoints (relative to `/api/v1/dataconnect`) | Service(s) |
|---|---|---|---|
| 0 | Catalog | `GET/POST/PUT/DELETE /connector-templates[/{id}]` | catalog_service |
| 0 | Connections | `…/connections[/{id}]`, `/test`, `/discover` | connection_service + adapters |
| 1 | Transform policies | `…/transform-policies[/{id}]`, `/validate`, `/preview` | transform_policy |
| 2 | Schema versions | `…/schemas`, `/infer`, `/{id}`, `/{id}/versions`, `/validate-compatibility`, `/drift-events` | schema_service + inference |
| 3 | Quality policies | `…/quality-policies[/{id}]`, `/validate`, `/quality-results` | quality_service + quality |
| 4 | Pipelines | `…/pipelines[/{id}]`, `/status`, `/generated-config` | pipeline_service + pipeline_config + topic_naming |
| 5 | Pipeline lifecycle | `…/pipelines/{id}/deploy|pause|resume|restart` | pipeline_service (+ runtime_orchestrator if flag on) |
| 6 | Lineage | `…/assets`, `/lineage/{asset_id}`, `/lineage/pipeline/{id}`, `/lineage/events` | lineage_service |
| 7 | Quality execution | `…/quality-policies/{id}/execute`, `/stream/process` | quality_service + streaming_quality_processor |
| 8 | Schema drift | `…/schemas/{id}/detect-drift`, `…/schemas/drift-approvals[...]/approve|reject` | schema_service + schema_drift |
| 9 | Monitoring | `…/runtime/connect-clusters[...]`, `/connectors/{name}/status|tasks`, `/metrics/summary` | monitoring_service |

---

## 7. End-to-end flows

### 7.1 Build → deploy a pipeline (the core flow)

```
1. POST /connector-templates           (or use seeded tpl_debezium_postgres)
2. POST /connections                    create source profile (org-scoped)         → connection_id
   POST /connections/{id}/test          adapter validates connection_config structurally
3. (optional) POST /transform-policies  SMT policy                                  → transform_id
   (optional) POST /schemas             approved schema version                     → schema_id
   (optional) POST /quality-policies    quality rules                               → quality_id
4. POST /pipelines                       references template + connection (+ optional ids)
        └─ pipeline_service._build_pipeline_row:
              resolve+org-check refs  →  generate_pipeline_config:
                 topic_naming.validate_topic_parts (lowercase, limits)
                 build topic_name + connector_name
                 layer config dicts (template→connection→pipeline→SMT→error-handling→computed)
           → store.create("pipelines", row)                                        → pipeline_id
5. GET /pipelines/{id}/generated-config  inspect the merged Kafka Connect config
6. POST /pipelines/{id}/deploy           state draft→deployed
        ├─ if runtime_deploy_enabled: RuntimeOrchestratorService.deploy_pipeline → Connect REST PUT
        └─ lineage_service.materialize_pipeline_lineage(pipeline, connection)  ← creates the graph
7. pause/resume/restart                  guarded by VALID_TRANSITIONS (409 on illegal)
```

### 7.2 Streaming quality (per-record routing)

```
POST /quality-policies/{id}/stream/process  { pipeline_id, record }
   route injects InMemoryKafkaProducer
   → quality_service.process_streaming_record
       → StreamingQualityProcessor.process_record
            evaluate_record(record, policy)
            valid    → { status: "valid",       destination_topic: <prepared topic> }
            invalid  → build quarantine envelope → producer.produce(<quarantine topic>)
                        → { status: "quarantined", failed_rules, quarantine_record }
```
Batch variant: `POST /quality-policies/{id}/execute { pipeline_id, records }` →
`evaluate_batch` → persisted `QualityResult` (with up to 10 sample failure envelopes).

### 7.3 Schema drift → approval

```
POST /schemas/{id}/detect-drift  { records }
   → infer observed schema → compare_schema_fields(approved, observed) → drift events + actions
   → approval_required?  yes → schema_service.create_drift_approval(...)  → approval_request_id
GET  /schemas/drift-approvals[?status=pending]
POST /schemas/drift-approvals/{id}/approve | /reject   { reviewed_by, comment }
```
Drift action severity, ascending: allow_and_notify < alert < quarantine < route_to_schema_review
< hold_for_approval < block_deployment. Only the last two block compatibility.

---

## 8. Behavioral notes & gotchas (verified)

- **Runtime is off by default** (`runtime_deploy_enabled=false`): deploy/pause/resume/restart and
  streaming work fully **offline** — they manipulate stored status and an in-memory producer; no
  Kafka cluster needed for testing.
- **In-memory store resets on restart** and is per-process — not durable, not multi-replica safe.
  Durability is the job of the dormant `db/`+`models/`+`repositories/` layer.
- **Lowercase topic segments**: `domain/source_system/entity` (and `org_id`) must be lowercase
  `[a-z0-9_-]`. `org_1001` passes; `Sales` does not (→ 422).
- **`resume` after `restart` is legal** (restarting → running) — not a 409.
- **Quality rules**: 10 accepted, 6 enforced (see §5.4).
- **Connection adapter fallthrough**: `mongodb/elasticsearch/http/ftp` use the DB adapter, so their
  `/test` needs `hostname`+`database`.
- **Two fixed bugs** (now corrected in source): (A) the `RequestValidationError` handler used to
  500 on validator `ValueError`s because `exc.errors()` embeds a non-serializable exception — fixed
  with `jsonable_encoder(..., custom_encoder={Exception: str})`; (B) `min_max_numeric` used to crash
  on non-numeric input — now caught so the rule simply fails.

---

## 9. Configuration quick reference (`core/config.py`, env prefix `DATACONNECT_`)

| Setting | Default | Effect |
|---|---|---|
| `api_prefix` | `/api/v1/dataconnect` | base path for the whole router |
| `env` | `development` | toggles docs UI + log renderer + SQL echo |
| `runtime_deploy_enabled` | `false` | when true, lifecycle ops call Kafka Connect |
| `kafka_connect_url` | `http://localhost:8083` | Connect REST target |
| `kafka_rest_proxy_url` | `http://localhost:8082` | producing quarantine records |
| `topic_soft_limit` | `180` | max topic name length |
| `database_url` | `postgresql+asyncpg://…` | async engine (dormant path) |
| `secret_backend` | `env` | `env` or `vault` (hvac) |

OpenAPI: `GET /openapi.json` (served at root; routes inside carry the prefix). Swagger UI at `/docs`
when `env != production`.

# DataConnect Backend — Knowledge Transfer (KT) Guide

A read-and-present guide for handing over the DataConnect backend. Read top-to-bottom once to
prepare; on the call, follow the **agenda** and use the **talking points** as speaker notes.
For deep detail, point people to `BACKEND_ARCHITECTURE.md`.

> How to use this: the *italic "Say:"* lines are what you can literally say out loud. The bullet
> points under them are the backup facts if someone digs in.

---

## 0. 30-second elevator pitch (start here)

*Say: "DataConnect is the control plane for data ingestion. It does NOT move data itself — it lets
a tenant register a source, pick a connector, and compose a pipeline, and then it generates the
exact Kafka Connect configuration for that pipeline. Around that core it adds transforms, schema
management with drift detection, data-quality rules, lineage, and monitoring. It's a FastAPI
service, multi-tenant by an org id, and today it runs fully in-memory so it's easy to demo."*

Three sentences to anchor the whole session:
1. **It generates connector config; it doesn't run the data plane.** (Kafka Connect does that.)
2. **Everything is scoped to a tenant** via the `X-Org-Id` header.
3. **There are two persistence layers** — an in-memory store that's live today, and a full
   Postgres/SQLAlchemy layer that's built but not yet wired in.

---

## 1. Suggested agenda (≈45 min)

| Time | Topic | Section |
|---|---|---|
| 5 min | What it is + the mental model | §0, §2 |
| 5 min | Tech stack + how to run it | §3 |
| 8 min | Architecture & request lifecycle | §4 |
| 10 min | The 9 functional domains | §5 |
| 7 min | The core end-to-end flow (build→deploy→lineage) | §6 |
| 3 min | The naming convention (emphasize) | §7 |
| 5 min | Live demo | §8 |
| 2 min | Gotchas & known issues | §9 |
| — | Q&A | §10 |

---

## 2. The mental model (draw this on the whiteboard)

```
                 ┌─────────────────────────────────────────────┐
  REST client →  │   DataConnect (FastAPI control plane)        │
  (UI / Postman) │                                              │
                 │   validate → tenant-scope → business logic   │
                 │           → GENERATE connector config        │
                 └───────────────┬──────────────────┬───────────┘
                                 │  (flag ON only)   │  (flag ON only)
                                 ▼                   ▼
                         Kafka Connect REST     Kafka REST Proxy
                         (deploy connectors)    (quarantine records)
```

*Say: "Read it left to right. A request comes in, we validate it, scope it to the org, run the
service logic, and the output is configuration. Only if the runtime flag is on do we actually call
Kafka. With the flag off — which is the default — it's a pure metadata service, which is why it
runs anywhere with no infrastructure."*

---

## 3. Tech stack & how to run

*Say: "Standard modern Python service."*

- **FastAPI** (web) + **Uvicorn** (server), **Pydantic v2** (validation), **SQLAlchemy 2 async** +
  **Alembic** + **asyncpg** (DB layer), **httpx** (calls to Kafka), **structlog** (JSON logs).
- Python 3.11, packaged with Poetry / `pyproject.toml`.

**Run it (Docker):**
```bash
docker build -t dataconnect ./backend
docker run --rm -p 8000:8000 dataconnect
# Swagger UI:  http://localhost:8000/docs
# Health:      http://localhost:8000/api/v1/dataconnect/health
```
**Run it (local dev):** `make backend` → `uvicorn app.main:app --reload`.

*Say: "Two things people always get wrong: the base path is `/api/v1/dataconnect`, not root; and
the in-memory data resets every time the container restarts."*

---

## 4. Architecture — the layers & a request's journey

*Say: "It's a clean layered architecture. Each layer has one job."*

```
app/
  main.py        → app factory: middleware, error handlers, mounts the router
  api/routes/    → HTTP layer (thin): one file per domain, just validates + calls a service
  core/          → config, tenancy (X-Org-Id), logging
  schemas/       → Pydantic request/response models = the API contract + field validation
  services/      → THE BRAIN: all business logic lives here
  storage/       → in-memory store (active persistence today) + seed data
  db / models / repositories → the Postgres layer (built, not yet wired in)
```

**A request flows like this** (walk through it slowly):
1. **Middleware** stamps a request id for tracing.
2. **`get_org_id` dependency** reads `X-Org-Id`; missing/invalid → **400**.
3. **Pydantic** validates the body (enums, required fields); bad input → **422**.
4. **Route handler** (thin) calls a **service**.
5. **Service** reads/writes the store, builds config, runs domain rules, optionally calls Kafka.
6. **Response model** serializes the result to JSON.

**Error codes to remember** (one line you can recite):
*Say: "400 = no/invalid tenant header; 422 = bad input or a domain rule; 400 specifically for bad
quality rules; 404 = not found / wrong org; 409 = illegal pipeline state change; 500 = unexpected."*

**The one architectural fact to stress:**
*Say: "There are two persistence layers. Today every service uses an in-memory store in
`storage/memory.py` — that's why data resets on restart and isn't shared across replicas. The full
Postgres schema, models, and repositories all exist under `db/`, `models/`, `repositories/` — with
real foreign keys and unique constraints — but the services don't call them yet. Cutting over to
Postgres means swapping the `store.*` calls for repository calls. That's the single most important
thing to know before anyone tries to productionize this."*

---

## 5. The 9 functional domains (the bulk of the session)

For each: **what it does**, **the key endpoints**, and **one talking point**.

### 1) Connector Catalog — `/connector-templates`
- Manages connector *templates* (which Kafka Connect plugin + default config). Global (not
  org-scoped). CRUD only; fields are free-form (no enum validation).
- *Say: "Think of these as the 'menu' of connector types — Debezium Postgres, JDBC, S3, REST, etc.
  A pipeline points at one of these."*

### 2) Connections — `/connections` (+ `/test`, `/discover`)
- A *connection profile* = how to reach a source. Org-scoped. Stores a **secret reference**, never
  the raw secret. `/test` and `/discover` pick an **adapter** by `source_type`.
- 18 supported source types → 3 adapters: **Database** (needs `hostname`+`database`), **API**
  (needs `base_url`/`url`), **File** (needs `bucket`/`path`/`container`).
- *Say: "Validation of the source type happens at create; the structural check of the config
  happens at `/test`. And note `mongodb/elasticsearch/http/ftp` fall back to the DB adapter."*

### 3) Transform Policies — `/transform-policies` (+ `/validate`, `/preview`)
- Ordered list of **Kafka SMTs** (single-message transforms): mask, rename, drop, insert,
  value-to-key, flatten, route, etc. The service maps friendly names to SMT classes and emits the
  Connect config fragment.
- *Say: "SMTs are lightweight per-record transforms only. Anything heavy — joins, aggregations — is
  explicitly rejected and meant to run outside Connect."*

### 4) Schema Versions — `/schemas` (+ `/infer`)
- Infer a schema from sample records, store **versioned schemas** (field name/type/nullable/pk),
  manage compatibility. `/infer` is the only unauthenticated endpoint.
- *Say: "The schema you approve becomes the contract that drift detection later compares against."*

### 5) Quality Policies — `/quality-policies` (+ `/validate`, `/quality-results`)
- Define **data quality rules** (10 types: required, type_check, regex, allowed_values, numeric/
  string ranges, freshness, null %, dup key, referential hint) and an `on_failure` action.
- *Say: "Heads up — all 10 types are accepted and validated, but only 6 are actually enforced at
  runtime today. The other 4 are placeholders."*

### 6) Pipelines — `/pipelines` (+ `/status`, `/generated-config`)
- **The integration point.** A pipeline binds template + connection (+ optional transform, schema,
  quality) and the service deterministically generates `topic_name`, `connector_name`, and the full
  `generated_config`.
- *Say: "This is where it all comes together. Creating a pipeline validates that the referenced
  template and connection exist and belong to your org, then builds the Kafka Connect config by
  layering defaults → connection config → pipeline overrides → transforms → error handling."*

### 7) Pipeline Lifecycle — `/deploy`, `/pause`, `/resume`, `/restart`
- A **state machine**: `draft → deployed → running ⇄ paused → restarting → failed`. Illegal
  transitions return **409**. With the runtime flag off, it just flips stored status.
- *Say: "Deploy is special — it also builds the lineage graph. So lineage only appears after a
  deploy."*

### 8) Quality Execution — `/execute`, `/stream/process`
- **Batch**: evaluate a set of records → a quality result summary. **Streaming**: per-record →
  valid goes to the *prepared* topic, invalid goes to the *quarantine* topic with an envelope.
- *Say: "This is the runtime side of quality — it mirrors the design's Level 2 streaming validator
  and Level 3 batch profiling."*

### 9) Schema Drift — `/detect-drift`, `/drift-approvals` (+ approve/reject)
- Compare observed records against an approved schema → **drift events**, each with an action:
  allow-and-notify, hold-for-approval, quarantine, block-deployment, etc. Blocking drift opens an
  **approval** that a reviewer approves/rejects.
- *Say: "New optional field is fine; new required field or a primary-key change blocks and needs
  human approval."*

### + Lineage — `/assets`, `/lineage/...`, `/lineage/events`
- The **graph**: source → connector → raw topic → quality processor → prepared topic. Built on
  deploy; also accepts runtime events.

### + Monitoring — `/runtime/...`
- Connect clusters, connector status/tasks, and a metrics summary. Mostly static/derived today; not
  org-scoped.

---

## 6. The core end-to-end flow (narrate this like a story)

*Say: "Let me walk the happy path a user takes."*

```
1. Pick a connector template        (catalog)            "what plugin"
2. Create a connection + test it     (connections)        "how to reach the source"
3. (optional) transform / schema / quality policies        "how to shape & guard data"
4. Create a pipeline                 (pipelines)           "bind it all + generate config"
        → produces topic_name, connector_name, generated_config
5. Deploy the pipeline               (lifecycle)           "go live + build lineage"
6. Records flow → quality routes them to prepared or quarantine topics
7. Monitor health + view lineage graph
```

*Say: "Steps 1–4 are configuration. Step 4 is where the magic is — the deterministic config
generation. Step 5 is the only place we'd touch real Kafka."*

---

## 7. The naming convention (emphasize — it's the heart)

*Say: "Everything keys off a 6-part topic name. Memorize this."*

```
Topic:     <zone>-<domain>-<source_system>-<entity>-<mode>-<version>
Connector: <domain>--<source_system>--<entity>--<mode>--<version>   (no zone)

Example:   raw-crm-salesforce-accounts-api-v1
```

The organization is **not** part of a name (since 2026-11-06): an organization owns its own Kafka, so its names need no prefix. Names built
before that (`org_1001-raw-...`) are still read; only new pipelines get the new names. Two organizations on one Kafka / Connect cluster
cannot create the same topic or connector name: the database refuses the second (migration 0057).

Rules: **lowercase only**, allowed chars in each part `a-z 0-9 _` (**no `-` inside a part** — it is the
separator, and a `-` inside one would make two different names collide), `zone` ∈ {raw, prepared, curated,
quarantine, meta}, `mode` ∈ {cdc, snapshot, batch, api, stream, validation, replay}, `version`
matches `^v[0-9]+$`, topic ≤ 180 chars. Break a rule → **422**.

The separator is `-` (it was `.`): Gravitino, the catalog behind UCS, cannot load an object name with more than
two dots. Pipelines created before the change keep their dotted topic (Kafka cannot rename one) and every topic
derived from them — prepared, quarantine, dead-letter — stays dotted too; each pipeline records which separator it
uses (`topic_separator`). An organization whose id contains a character other than `a-z 0-9 _` keeps dotted topics.

*Say: "Quarantine and prepared topics are derived from this — the streaming validator just swaps
the zone. So `.raw.` becomes `.prepared.` for good records and a `.quarantine.…validation.` topic
for bad ones."*

---

## 8. Live demo script (have a terminal ready)

```bash
BASE=http://localhost:8000/api/v1/dataconnect
ORG="X-Org-Id: org_1001"; J="Content-Type: application/json"

# 1. it's alive
curl -sS $BASE/health

# 2. seeded template + create a pipeline (uses seeded connection conn_demo_salesforce)
curl -sS -X POST $BASE/pipelines -H "$J" -H "$ORG" -d '{
  "name":"demo","template_id":"tpl_debezium_postgres",
  "connection_profile_id":"conn_demo_salesforce",
  "zone":"raw","domain":"crm","source_system":"salesforce","entity":"accounts",
  "mode":"api","version":"v1"}'        # ← note topic_name + connector_name in the response

# 3. show the generated Kafka Connect config (the core value)
curl -sS $BASE/pipelines/<PIPELINE_ID>/generated-config -H "$ORG"

# 4. deploy → state flips, lineage is built
curl -sS -X POST $BASE/pipelines/<PIPELINE_ID>/deploy -H "$ORG"
curl -sS $BASE/assets -H "$ORG"

# 5. monitoring
curl -sS $BASE/runtime/metrics/summary
```

*Say: "The thing to point at is step 3 — that generated config is what the whole service exists to
produce."* Alternatively, run the **Postman collection** `DataConnect-Full-E2E…json` in the
Collection Runner to show all 11 domains green.

Seed data that always exists (handy for demos): template `tpl_debezium_postgres`, connection
`conn_demo_salesforce`, transform `tfp_customer_basic_v1` — all under org `org_1001`.

---

## 9. Gotchas & known issues (be honest about these)

- **In-memory store**: data resets on restart, not durable, not multi-replica safe. Postgres layer
  is built but dormant.
- **Base path** is `/api/v1/dataconnect`; **`X-Org-Id` required** on most endpoints (not on
  templates, `/schemas/infer`, monitoring).
- **Runtime flag off by default** → lifecycle/streaming work without real Kafka.
- **Quality rules**: 10 accepted, 6 enforced.
- **DB adapter fallthrough**: `mongodb/elasticsearch/http/ftp` need `hostname`+`database` for `/test`.
- **Two bugs we found & fixed during testing** (mention as "recently hardened"):
  1. Validation errors returned **500 instead of 422** because the error handler couldn't serialize
     a Pydantic `ValueError` — fixed with `jsonable_encoder(..., custom_encoder={Exception: str})`.
  2. A numeric quality rule crashed on non-numeric input — now it fails the rule instead.
- **Pipeline uniqueness** (`UNIQUE(org_id, topic_name/connector_name)`) is defined in the ORM but
  **not enforced** while the in-memory store is active.

---

## 10. Anticipated Q&A (rehearse these)

**Q: Does this move/process the actual data?**
A: No. It generates Kafka Connect configuration and orchestrates connectors. Kafka Connect + the
streaming validator do the data plane. With the runtime flag off it's pure metadata.

**Q: Where is data stored?**
A: Today, in-memory (`storage/memory.py`), reset on restart. The production target is Postgres via
the `db/`+`models/`+`repositories/` layer, which is built but not yet wired into the services.

**Q: How is multi-tenancy enforced?**
A: Every org-scoped request carries `X-Org-Id`; `core/tenant.py` validates it and services filter/
guard by `org_id`. Templates and monitoring are global.

**Q: How do we add a new connector type?**
A: Add a connector template (catalog) — no code change needed for the template itself. To support a
new *source_type* adapter behavior, add it to `SOURCE_TYPES` and the adapter map in
`connection_adapters.py`.

**Q: How does deploy work without Kafka?**
A: `runtime_deploy_enabled=false` short-circuits the orchestrator; it just updates stored status and
returns a synthetic runtime block. Flip the flag + point `kafka_connect_url` at a real cluster for
live deploys.

**Q: What happens to bad records?**
A: The streaming validator routes them to a quarantine topic with an envelope (original topic,
pipeline id, failed rules, the record). Good records go to the prepared topic.

**Q: How do we test it?**
A: Postman collections in `backend/` (`DataConnect-Full-E2E…json` is the complete, ordered one) and
the `API_TEST_PLAN.md`. Run folders 1→11 in the Collection Runner.

**Q: Is there auth?**
A: Org context via header today; JWT settings exist in config for future RBAC but aren't wired.

---

## 11. Code cheat sheet (where to look)

| To understand… | Open… |
|---|---|
| App wiring, middleware, error handlers | `app/main.py` |
| Route definitions (per domain) | `app/api/routes/*.py` |
| Tenant header logic | `app/core/tenant.py` |
| Config / feature flags | `app/core/config.py` |
| API contracts + validation | `app/schemas/*.py` |
| Business logic | `app/services/*.py` |
| Config generation + naming | `services/pipeline_config.py`, `services/topic_naming.py` |
| Pipeline state machine | `services/pipeline_service.py` |
| Active persistence + seed data | `app/storage/memory.py` |
| Future Postgres schema | `app/db/`, `app/models/`, `app/repositories/` |
| Full deep-dive reference | `BACKEND_ARCHITECTURE.md` |

---

## 12. Glossary (for the audience)

- **Connector template** — a reusable definition of a Kafka Connect plugin + defaults.
- **Connection profile** — credentials/config to reach one source system.
- **Pipeline** — a configured ingestion unit binding template + connection + policies.
- **SMT** — Single Message Transform; a per-record Kafka Connect transform.
- **Zone** — data lifecycle stage in the topic name (raw/prepared/curated/quarantine/meta).
- **Drift** — observed schema diverging from the approved schema.
- **Quarantine** — topic/envelope where records failing quality are routed.
- **Lineage** — the graph of how data flows source → topic → processor → prepared.
- **org_id / X-Org-Id** — the tenant identifier; the basis of multi-tenancy.
```

# DataConnect — API Test Plan (code-verified)

A practical, **dependency-ordered** guide to exercising every backend API with **valid**
bodies/params, plus the full set of **connection templates (source types)** the code accepts and
what each one needs. Every value here was extracted from the source, not guessed.

> ⚠️ **Two things the older `API_TEST_MATRIX.md` gets wrong — fix these first:**
> 1. **Routes are NOT at the root.** `app/main.py` mounts the router at `settings.api_prefix`
>    (`/api/v1/dataconnect`). So `/health` is actually
>    `http://localhost:8000/api/v1/dataconnect/health`.
> 2. **The DB connection adapter requires `hostname` + `database`** in `connection_config`
>    (see `connection_adapters.py`), **not `host`**. Using `host` makes `/test` return `failed`.

---

## 0. Setup & global rules

| Thing | Value | Source |
|---|---|---|
| Base URL (Docker) | `http://localhost:8000/api/v1/dataconnect` | `Dockerfile` (port 8000) + `config.py api_prefix` |
| Org header | `X-Org-Id: org_1001` | `core/tenant.py` |
| Org id format | regex `^[a-zA-Z0-9_-]{3,100}$` | `core/tenant.py` — bad format → **400** |
| Content type | `application/json` on every POST/PUT | — |
| Docs UI | `http://localhost:8000/docs` and `/openapi.json` (env≠production) | `main.py` |
| `runtime_deploy_enabled` | **false** by default → deploy/pause/etc. work **without** a real Kafka Connect | `config.py` |

Set a base var to keep the curls short:

```bash
export BASE=http://localhost:8000/api/v1/dataconnect
export ORG="X-Org-Id: org_1001"
export JSON="Content-Type: application/json"
```

### Error-code conventions (verified in code)
- **Missing `X-Org-Id`** on an org-scoped route → **400** (`get_org_id`).
- **Pydantic body/field validation** (bad enum via `field_validator`, missing required field) → **422**.
- **Service-raised `ValueError`** (e.g. topic-segment not lowercase, bad SMT type) → **422** via the global
  `ValueError` handler in `main.py`.
- **Quality policy** create/update is the exception: the route catches `ValueError` → **400**.
- **Not found / org mismatch** → **404**.
- **Illegal pipeline lifecycle transition** → **409**.

### Seed data already present at startup (`storage/memory.py → seed()`)
You can test the whole pipeline flow **without creating anything** by reusing these:

| Collection | id | Notes |
|---|---|---|
| connector_templates | `tpl_debezium_postgres` | no org scoping on templates |
| connection_profiles | `conn_demo_salesforce` | `org_id=org_1001`, `source_type=api` |
| transform_policies | `tfp_customer_basic_v1` | `org_id=org_1001` |

> The in-memory store is **process-global and resets on container restart**. Org scoping is enforced
> per-collection (`org_id` must match the header) for connections, pipelines, transforms, schemas, quality,
> lineage — but **NOT** for connector templates and monitoring.

---

## 1. Valid-value reference (the "allowed by code" tables)

### 1.1 Connection `source_type` → adapter → required `connection_config`
`source_type` is validated against `SOURCE_TYPES` (`schemas/connection.py`), then `/test` and `/discover`
pick an adapter via `adapter_for_source_type()` (`connection_adapters.py`). **The validated set is larger
than the adapter map**, so several valid source types silently fall through to the **Database** adapter.

| `source_type` (valid to create) | Adapter used | `/test` requires | `/discover` reads |
|---|---|---|---|
| `postgres`, `postgresql`, `mysql`, `sqlserver`, `oracle`, `jdbc` | **Database** | `hostname` **and** `database` | `tables: [...]` |
| `mongodb`, `elasticsearch`, `http`, `ftp` | **Database** (fallback!) | `hostname` **and** `database` | `tables: [...]` |
| `api`, `rest`, `webhook` | **API** | `base_url` *or* `url` (scheme `http`/`https`) | `endpoints: [...]` |
| `s3`, `gcs`, `azure_blob`, `sftp`, `file` | **File** | `bucket` *or* `path` *or* `container` | `objects: [...]` |

> Edge cases worth a test: `http`, `ftp`, `mongodb`, `elasticsearch` **create fine** but their `/test`
> demands DB fields (`hostname`,`database`) — an intentional gotcha to assert on.
> API adapter: if you also pass `health_path`, `/test` makes a **real outbound HTTP GET** to
> `base_url + health_path` (needs network/the host reachable from the container).

Allowed `status` on a connection: `draft` (default), `active`, `inactive`, `error`.

### 1.2 Pipeline enums (`schemas/pipeline.py` + `topic_naming.py`)
- `zone` ∈ `{raw, prepared, curated, quarantine, meta}`
- `mode` ∈ `{cdc, snapshot, batch, api, stream, validation, replay}`
- `version` matches `^v[0-9]+$` (e.g. `v1`, `v2`)
- **Topic segments must be lowercase**: `org_id`, `zone`, `domain`, `source_system`, `entity`, `mode`,
  `version` each match `^[a-z0-9_-]+$`. Passing `domain:"Sales"` → **422** (uppercase rejected).
- Built topic must be ≤ `topic_soft_limit` (180 chars) → otherwise **422**.

Topic name = `org.zone.domain.source_system.entity.mode.version` (dot-joined).
Connector name = `org--domain--source_system--entity--mode--version` (double-dash; zone omitted).

### 1.3 Pipeline lifecycle state machine (`pipeline_service.VALID_TRANSITIONS`)
| From | Allowed target | Endpoint that sets it |
|---|---|---|
| `draft` | `deployed` | `POST .../deploy` |
| `deployed` | `paused`, `running`, `failed` | `pause` → paused, `resume` → running |
| `running` | `paused`, `restarting`, `failed` | `pause`, `restart` |
| `paused` | `running`, `restarting` | `resume`, `restart` |
| `restarting` | `running`, `failed` | (no endpoint sets `running` from here — see note) |
| `failed` | `deployed`, `restarting` | `deploy`, `restart` |

> Note: `resume` always targets **`running`**, which is only legal from `deployed`/`paused`. After
> `restart` (state `restarting`) there is **no route** to reach `running` again → calling `resume` then
> returns **409**. That's a real constraint to document, not a bug to fix during testing.

### 1.4 Transform SMT types (`transform_policy.py`)
Allowed `type`: `ValueToKey, ExtractField, HoistField, Flatten, RegexRouter, HeaderFrom, MaskField,
ReplaceField, InsertField, TimestampConverter`.
- `scope` ∈ `{key, value, header}` (default `value`).
- `name` must be alphanumeric/underscore only.
- These types are **rejected** (raise → 422): `join, aggregation, stateful_deduplication,
  complex_enrichment, ml_cleansing, fuzzy_matching, cross_record_validation, heavy_nested_restructuring`.

### 1.5 Quality rule types (`services/quality.py`)
Allowed `rules[].type`: `required, type_check, regex, allowed_values, min_max_numeric,
min_max_string_length, timestamp_freshness, null_percentage_threshold, duplicate_key_batch,
referential_hint`. Unknown type → **400** on create/update.
`type_check.expected_type` ∈ `{string, integer, number, boolean, object, array}`.
`on_failure` default: `route_to_quarantine`.

### 1.6 Schema / drift
- `compatibility` default `BACKWARD`; `schema_format` default `json_schema`; `status` default `active`.
- For `detect-drift` to find drift, the **approved schema must store fields under
  `schema_definition.fields`** as a list of `{name,type,nullable,primary_key}` (that's what
  `schema_service.detect_drift` compares against). See §3.G.

---

## 2. Dependency graph & recommended test order

```
catalog (connector template)  ─┐
connections (profile, org)     ─┼─►  pipelines  ─► deploy ─► (lineage assets/edges appear)
transform-policies (optional)  ─┤                   │
schemas (optional)             ─┤                   ├─► pause/resume/restart/status/generated-config
quality-policies (optional)    ─┘                   │
                                                    └─► quality execute / stream  (needs pipeline_id)
schemas/{id} ─► detect-drift ─► drift-approvals ─► approve/reject
monitoring (/runtime/*)  = independent, read-only
```

**Run order:**

1. **Health** — `GET /health` (no deps).
2. **Connector templates** — `GET` list (seeded), `POST` create, `GET/{id}`, `PUT`, `DELETE`. No org header.
3. **Connections** — `POST` one per adapter family (DB/API/File), then `GET`, `GET/{id}`, `PUT`,
   `POST /test`, `POST /discover`. Capture an id.
4. **Transform policy** *(optional input to pipeline)* — `POST`, `validate`, `preview`.
5. **Schema version** *(optional)* — `POST /schemas`, plus `infer` (no auth).
6. **Quality policy** *(optional)* — `POST`, `validate`.
7. **Pipelines** — `POST` referencing the template + connection (+ optional transform/schema/quality ids).
   Capture `pipeline_id`. Then `GET`, `status`, `generated-config`.
8. **Pipeline lifecycle** — `deploy` → `pause` → `resume` → `restart` (respect §1.3).
9. **Lineage** — *after a deploy* (deploy materializes assets): `GET /assets`,
   `GET /lineage/{asset_id}`, `GET /lineage/pipeline/{pipeline_id}`, `POST /lineage/events`.
10. **Quality execution** — `POST /quality-policies/{id}/execute` and `/stream/process`
    (both require an existing `pipeline_id` in this org).
11. **Schema drift** — `POST /schemas/{id}/detect-drift` → if `approval_required`, an approval id comes back
    → `approve`/`reject`; also `GET /schemas/drift-approvals`.
12. **Monitoring** — `/runtime/*` anytime.

The fastest happy path (uses only seed data): **1 → 7 (seeded ids) → 8 → 9**.

---

## 3. Valid test cases (curl, in dependency order)

### A. Health
```bash
curl -sS "$BASE/health"
# 200 → {"status":"ok","service":"dataconnect"}
```

### B. Connector templates  (`/connector-templates`, no org header)
```bash
# list (includes seeded tpl_debezium_postgres)
curl -sS "$BASE/connector-templates"

# create  → 201, returns generated id
curl -sS -X POST "$BASE/connector-templates" -H "$JSON" -d '{
  "name":"Postgres CDC",
  "connector_type":"source",
  "category":"database_cdc",
  "connector_class":"io.debezium.connector.postgresql.PostgresConnector",
  "runtime_pool_type":"connect-cdc-pool",
  "config_schema":{"database.hostname":{"type":"string","required":true}},
  "default_config":{"plugin.name":"pgoutput","tasks.max":"1","snapshot.mode":"initial"},
  "default_transforms":[],
  "version":"v1",
  "is_active":true
}'

# get one / update / delete
curl -sS "$BASE/connector-templates/tpl_debezium_postgres"
curl -sS -X PUT "$BASE/connector-templates/tpl_debezium_postgres" -H "$JSON" -d '{ ... same shape ... }'
curl -sS -X DELETE "$BASE/connector-templates/tpl_debezium_postgres" -i   # 204
```

### C. Connections — one valid template per adapter family  (`/connections`, org required)

**C1. Database family** (`postgres`/`postgresql`/`mysql`/`sqlserver`/`oracle`/`jdbc` — and fallbacks
`mongodb`/`elasticsearch`/`http`/`ftp`). Note `hostname` (not `host`):
```bash
curl -sS -X POST "$BASE/connections" -H "$JSON" -H "$ORG" -d '{
  "name":"orders-postgres",
  "source_type":"postgres",
  "connection_config":{
    "hostname":"db.internal",
    "port":5432,
    "database":"orders",
    "tables":["public.orders","public.customers"]
  },
  "secret_ref":"vault://dataconnect/org_1001/orders-pg",
  "network_profile":"default",
  "status":"draft"
}'
```
Swap `source_type` to test each DB-family value (same body works):
`postgresql, mysql, sqlserver, oracle, jdbc, mongodb, elasticsearch, http, ftp`.

**C2. API family** (`api`/`rest`/`webhook`):
```bash
curl -sS -X POST "$BASE/connections" -H "$JSON" -H "$ORG" -d '{
  "name":"billing-api",
  "source_type":"rest",
  "connection_config":{
    "base_url":"https://api.example.com",
    "health_path":"/healthz",
    "endpoints":["/invoices","/customers"]
  },
  "status":"active"
}'
# Omit health_path to keep /test fully offline (no outbound call).
```

**C3. File / object-store family** (`s3`/`gcs`/`azure_blob`/`sftp`/`file`):
```bash
curl -sS -X POST "$BASE/connections" -H "$JSON" -H "$ORG" -d '{
  "name":"lake-s3",
  "source_type":"s3",
  "connection_config":{
    "bucket":"my-data-lake",
    "prefix":"raw/orders/",
    "objects":["orders-2024.parquet","orders-2025.parquet"]
  }
}'
# gcs → use "bucket"; azure_blob → use "container"; sftp/file → use "path".
```

**C-read/ops** (capture `CID` from a create above):
```bash
curl -sS "$BASE/connections" -H "$ORG"                       # list
curl -sS "$BASE/connections/$CID" -H "$ORG"                  # get
curl -sS -X PUT "$BASE/connections/$CID" -H "$JSON" -H "$ORG" -d '{ ...full body... }'
curl -sS -X POST "$BASE/connections/$CID/test"     -H "$ORG" # ConnectionTestResult
curl -sS -X POST "$BASE/connections/$CID/discover" -H "$ORG" # DiscoveryResult
curl -sS -X DELETE "$BASE/connections/$CID" -H "$ORG" -i     # 204
```

### D. Transform policies  (`/transform-policies`, org required)
```bash
# create (validated through generate_smt_config) → 201
curl -sS -X POST "$BASE/transform-policies" -H "$JSON" -H "$ORG" -d '{
  "name":"customer-basic",
  "description":"key + mask PII + insert metadata",
  "transforms":[
    {"order":1,"name":"createKey","type":"ValueToKey","scope":"value","config":{"fields":"customer_id"}},
    {"order":2,"name":"maskPII","type":"MaskField","scope":"value","config":{"fields":"email,phone"}},
    {"order":3,"name":"insertSource","type":"InsertField","scope":"value","config":{"static.field":"src","static.value":"orders_app"}}
  ]
}'

curl -sS "$BASE/transform-policies" -H "$ORG"                                  # list
curl -sS "$BASE/transform-policies/tfp_customer_basic_v1" -H "$ORG"            # get (seeded)
curl -sS -X PUT "$BASE/transform-policies/$TPID" -H "$JSON" -H "$ORG" -d '{...}'
curl -sS -X POST "$BASE/transform-policies/$TPID/validate" -H "$ORG"          # ValidationResult
curl -sS -X POST "$BASE/transform-policies/$TPID/preview" -H "$JSON" -H "$ORG" -d '{
  "input_record":{"customer_id":"c1","email":"a@b.com","phone":"555"}
}'
curl -sS -X DELETE "$BASE/transform-policies/$TPID" -H "$ORG" -i              # 204
```

### E. Schemas  (`/schemas`)
```bash
# infer — NO org header required
curl -sS -X POST "$BASE/schemas/infer" -H "$JSON" -d '{
  "records":[{"id":"1","amount":9.99,"active":true,"created_at":"2024-01-01T00:00:00Z"}]
}'

# create version → 201 (org required). Store fields under schema_definition.fields for drift testing:
curl -sS -X POST "$BASE/schemas" -H "$JSON" -H "$ORG" -d '{
  "domain":"sales","source_system":"orders_app","entity":"orders",
  "version":"v1","schema_format":"json_schema","compatibility":"BACKWARD","status":"active",
  "schema_definition":{"fields":[
    {"name":"id","type":"string","nullable":false,"primary_key":true},
    {"name":"amount","type":"number","nullable":true,"primary_key":false}
  ]}
}'

curl -sS "$BASE/schemas" -H "$ORG"                                  # list
curl -sS "$BASE/schemas/$SID" -H "$ORG"                             # get
curl -sS -X POST "$BASE/schemas/$SID/versions" -H "$JSON" -H "$ORG" -d '{ ...SchemaVersionCreate... }'
curl -sS -X POST "$BASE/schemas/$SID/validate-compatibility" -H "$ORG"   # always {valid:true,errors:[]}
curl -sS "$BASE/schemas/$SID/drift-events" -H "$ORG"
```

### F. Quality policies  (`/quality-policies`, org required)
```bash
curl -sS -X POST "$BASE/quality-policies" -H "$JSON" -H "$ORG" -d '{
  "name":"orders-quality",
  "rules":[
    {"name":"id_required","type":"required","field":"id"},
    {"name":"amount_type","type":"type_check","field":"amount","expected_type":"number"},
    {"name":"status_allowed","type":"allowed_values","field":"status","values":["new","paid","void"]},
    {"name":"amount_range","type":"min_max_numeric","field":"amount","min":0,"max":100000},
    {"name":"email_regex","type":"regex","field":"email","pattern":"^[^@]+@[^@]+$"}
  ],
  "on_failure":"route_to_quarantine"
}'

curl -sS "$BASE/quality-policies" -H "$ORG"                           # list
curl -sS "$BASE/quality-policies/$QID" -H "$ORG"                      # get
curl -sS -X PUT "$BASE/quality-policies/$QID" -H "$JSON" -H "$ORG" -d '{...}'
curl -sS -X POST "$BASE/quality-policies/$QID/validate" -H "$ORG"     # ValidationResult
curl -sS "$BASE/quality-results" -H "$ORG"                            # list results
curl -sS -X DELETE "$BASE/quality-policies/$QID" -i                  # 204 (no org guard here)
```

### G. Pipelines  (`/pipelines`, org required) — the integration point
```bash
# Using SEED ids (template + connection both exist in org_1001):
curl -sS -X POST "$BASE/pipelines" -H "$JSON" -H "$ORG" -d '{
  "name":"ingest-orders",
  "description":"Orders CDC into raw zone",
  "template_id":"tpl_debezium_postgres",
  "connection_profile_id":"conn_demo_salesforce",
  "transform_policy_id":"tfp_customer_basic_v1",
  "zone":"raw","domain":"sales","source_system":"orders_app","entity":"orders",
  "mode":"cdc","version":"v1",
  "connector_config":{"tasks.max":"2"},
  "status":"draft"
}'
# Capture PID = .id from the response.

curl -sS "$BASE/pipelines" -H "$ORG"                              # list
curl -sS "$BASE/pipelines/$PID" -H "$ORG"                         # get
curl -sS -X PUT "$BASE/pipelines/$PID" -H "$JSON" -H "$ORG" -d '{...full PipelineCreate...}'
curl -sS "$BASE/pipelines/$PID/status" -H "$ORG"                  # PipelineStatus
curl -sS "$BASE/pipelines/$PID/generated-config" -H "$ORG"        # the merged connector config dict
```

### H. Pipeline lifecycle (run in this order — see §1.3)
```bash
curl -sS -X POST "$BASE/pipelines/$PID/deploy"  -H "$ORG"   # draft → deployed (also builds lineage)
curl -sS -X POST "$BASE/pipelines/$PID/pause"   -H "$ORG"   # deployed → paused
curl -sS -X POST "$BASE/pipelines/$PID/resume"  -H "$ORG"   # paused → running
curl -sS -X POST "$BASE/pipelines/$PID/restart" -H "$ORG"   # running → restarting
# (resume after restart → 409 by design)
curl -sS -X DELETE "$BASE/pipelines/$PID" -H "$ORG" -i      # 204
```

### I. Lineage  (run AFTER at least one deploy)
```bash
curl -sS "$BASE/assets" -H "$ORG"                                  # list[Asset]; capture AID
curl -sS "$BASE/lineage/$AID" -H "$ORG"                            # graph around an asset
curl -sS "$BASE/lineage/pipeline/$PID" -H "$ORG"                   # graph for a pipeline
curl -sS -X POST "$BASE/lineage/events" -H "$JSON" -H "$ORG" -d '{
  "org_id":"org_1001",
  "event_type":"runtime_emit",
  "from_asset":{"asset_type":"kafka_topic","name":"raw-sales-orders_app-orders-cdc-v1","qualified_name":"org_1001:topic:raw:orders","metadata":{}},
  "to_asset":{"asset_type":"processor","name":"orders-quality","qualified_name":"org_1001:processor:orders:quality","metadata":{}},
  "edge_type":"VALIDATED_BY",
  "pipeline_id":"'"$PID"'"
}'
# Header org wins: a mismatched body org_id is overwritten with org_1001.
```

### J. Quality execution & streaming  (need an existing pipeline_id in this org)
```bash
curl -sS -X POST "$BASE/quality-policies/$QID/execute" -H "$JSON" -H "$ORG" -d '{
  "pipeline_id":"'"$PID"'",
  "records":[
    {"id":"1","amount":10,"status":"new","email":"a@b.com"},
    {"id":"","amount":-5,"status":"???","email":"bad"}
  ]
}'   # QualityResult with failed_count>0 and sample_failures

curl -sS -X POST "$BASE/quality-policies/$QID/stream/process" -H "$JSON" -H "$ORG" -d '{
  "pipeline_id":"'"$PID"'",
  "record":{"id":"2","amount":20,"status":"paid","email":"c@d.com"}
}'   # StreamingQualityResult {status, destination_topic, failed_rules, quarantine_record?}
```

### K. Schema drift → approvals
```bash
# Send records whose inferred types differ from the approved schema_definition.fields:
curl -sS -X POST "$BASE/schemas/$SID/detect-drift" -H "$JSON" -H "$ORG" -d '{
  "records":[{"id":"200","amount":"oops-a-string","new_required":42}]
}'
# → SchemaDriftResult; if approval_required, response carries approval_request_id (=APPID)

curl -sS "$BASE/schemas/drift-approvals" -H "$ORG"                       # list (optional ?status=pending)
curl -sS -X POST "$BASE/schemas/drift-approvals" -H "$JSON" -H "$ORG" -d '{
  "schema_version_id":"'"$SID"'",
  "observed_schema":{"fields":[{"name":"id","type":"string"}]},
  "drift_events":[],
  "requested_by":"reviewer"
}'
curl -sS -X POST "$BASE/schemas/drift-approvals/$APPID/approve" -H "$JSON" -H "$ORG" -d '{"reviewed_by":"reviewer","comment":"ok"}'
curl -sS -X POST "$BASE/schemas/drift-approvals/$APPID/reject"  -H "$JSON" -H "$ORG" -d '{"reviewed_by":"reviewer","comment":"breaking"}'
```

### L. Monitoring  (`/runtime/*`, no org header, mostly static/mocked)
```bash
curl -sS "$BASE/runtime/connect-clusters"                                  # 3 seeded pools
curl -sS "$BASE/runtime/connect-clusters/connect-cdc-pool/connectors"      # pipelines in that pool
curl -sS "$BASE/runtime/connectors/some-connector/status"                  # always RUNNING (mock)
curl -sS "$BASE/runtime/connectors/some-connector/tasks"                   # mock task list
curl -sS "$BASE/runtime/metrics/summary"                                   # MetricsSummary (derived from pipelines)
```

---

## 4. Negative / validation cases worth asserting

| # | Request | Expected |
|---|---|---|
| N1 | Any org-scoped GET **without** `X-Org-Id` | **400** "Missing tenant context" |
| N2 | `X-Org-Id: ab` (too short / bad chars) | **400** org_id format |
| N3 | Create connection with `source_type:"snowflake"` | **422** (not in `SOURCE_TYPES`) |
| N4 | Create connection `status:"weird"` | **422** |
| N5 | `POST /connections/$CID/test` on a `postgres` conn missing `hostname`/`database` | **200** body `{status:"failed", message:"Missing database fields: ..."}` |
| N6 | `POST /connections/$CID/test` on `api` conn missing `base_url`/`url` | **200** `{status:"failed","Missing API base_url"}` |
| N7 | Create pipeline with `zone:"bronze"` or `mode:"etl"` | **422** |
| N8 | Create pipeline with `domain:"Sales"` (uppercase) | **422** (topic segment lowercase rule) |
| N9 | Create pipeline with non-existent `template_id` | **404** "Connector template not found" |
| N10 | Create pipeline with `connection_profile_id` from another org | **404** "Connection profile not found" |
| N11 | `deploy` a pipeline that is already `deployed` | **409** invalid transition |
| N12 | `resume` right after `restart` | **409** (no `restarting → running` route) |
| N13 | Transform with `type:"join"` (non-SMT) | **422** |
| N14 | Transform with `type:"FooField"` (unknown SMT) | **422** |
| N15 | Quality policy with `type:"unknown_rule"` | **400** "Unsupported rule type" |
| N16 | Quality `execute`/`stream` with non-existent `pipeline_id` | **404** "Pipeline not found" |
| N17 | `GET /schemas/{bad-id}` | **404** "Schema not found" |
| N18 | `approve`/`reject` a non-existent approval id | **404** |

---

## 5. One-shot smoke script (seed-only happy path)

Save as `smoke.sh`, run while the container is up:

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE=http://localhost:8000/api/v1/dataconnect
ORG="X-Org-Id: org_1001"; JSON="Content-Type: application/json"

echo "health:";        curl -sS "$BASE/health"; echo
echo "templates:";     curl -sS "$BASE/connector-templates" | head -c 200; echo

PID=$(curl -sS -X POST "$BASE/pipelines" -H "$JSON" -H "$ORG" -d '{
  "name":"smoke-orders","template_id":"tpl_debezium_postgres",
  "connection_profile_id":"conn_demo_salesforce","transform_policy_id":"tfp_customer_basic_v1",
  "zone":"raw","domain":"sales","source_system":"orders_app","entity":"orders","mode":"cdc","version":"v1"
}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "pipeline_id=$PID"

curl -sS -X POST "$BASE/pipelines/$PID/deploy" -H "$ORG"; echo
curl -sS "$BASE/pipelines/$PID/generated-config" -H "$ORG" | head -c 300; echo
curl -sS "$BASE/assets" -H "$ORG" | head -c 300; echo
curl -sS "$BASE/runtime/metrics/summary"; echo
echo "OK"
```

> Tip: generate an always-current view with the live OpenAPI spec:
> `curl -sS $BASE/openapi.json > openapi.json` (or open `http://localhost:8000/docs`). Note `openapi.json`
> is served at the app root, but the **routes inside it are prefixed** with `/api/v1/dataconnect`.

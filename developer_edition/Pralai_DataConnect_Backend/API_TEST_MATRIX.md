# Backend API Test Matrix

Checklist
- [x] Enumerate every route with full path (including router prefixes)
- [x] For each route include: method, sync/async, path/query/header params, request body, response model, status codes, service/storage target
- [x] Provide a curl example for each endpoint and minimal example payloads

Notes
- Org-scoped endpoints require header `X-Org-Id`. Missing this header triggers a 400 from `get_org_id`.
- Pydantic validation errors typically return 422. Some handlers translate `ValueError` into 400.
- Use FastAPI's `/openapi.json` to generate clients or further docs.

Legend
- Body: JSON request body fields (required/optional). When a Pydantic model is referenced I list top-level fields.
- Service: the service/storage function called by the route (as seen in route implementation).

----

## 1) Health
- Route: GET `/health`
- Async: sync
- Params: none
- Headers: none
- Body: none
- Response: `{ "status": "ok", "service": "dataconnect" }`
- Status: 200
- Service: none (static)

Example
```bash
curl -sS http://localhost:8000/health
```

----

## 2) Connector templates (Catalog) — prefix: `/connector-templates`
File: `app/api/routes/catalog.py`

Models
- `ConnectorTemplateCreate`: name, connector_type, category, connector_class, runtime_pool_type, config_schema (dict), default_config (dict), default_transforms (list), version (str, default "v1"), is_active (bool), id (opt)
- `ConnectorTemplate` = above + id (str)

- GET `/connector-templates`
  - Response: `list[ConnectorTemplate]` (200)
  - Service: `catalog_service.list_templates()`
  - Example:
```bash
curl -sS -X GET http://localhost:8000/connector-templates
```

- POST `/connector-templates`
  - Body: `ConnectorTemplateCreate`
  - Response: `ConnectorTemplate` (201)
  - Service: `catalog_service.create_template(payload_dict)`
  - Example:
```bash
curl -sS -X POST http://localhost:8000/connector-templates \
  -H "Content-Type: application/json" \
  -d '{"name":"Postgres CDC","connector_type":"source","category":"database","connector_class":"io.debezium.connector.postgresql.PostgresConnector","runtime_pool_type":"kafka-connect","config_schema":{},"default_config":{},"default_transforms":[],"version":"v1","is_active":true}'
```

- GET `/connector-templates/{template_id}`
  - Path: `template_id`
  - Response: `ConnectorTemplate` or 404 (detail: "Connector template not found")
  - Service: `catalog_service.get_template(template_id)`
  - Example:
```bash
curl -sS -X GET http://localhost:8000/connector-templates/TEMPLATE_ID
```

- PUT `/connector-templates/{template_id}`
  - Body: `ConnectorTemplateCreate`
  - Response: `ConnectorTemplate` (200)
  - Service: `catalog_service.update_template(template_id, payload_dict)`

- DELETE `/connector-templates/{template_id}`
  - Response: 204 No Content
  - Service: `catalog_service.delete_template(template_id)`

----

## 3) Connections — prefix: `/connections`
File: `app/api/routes/connections.py`
Note: All endpoints require header `X-Org-Id`. Header is validated by `get_org_id`.

Models
- `ConnectionProfileCreate`: name (str), source_type (str, validated), connection_config (dict), secret_ref (opt), network_profile (opt), status (one of draft/active/inactive/error), id (opt)
- `ConnectionProfile`: above + id, org_id
- `ConnectionTestResult`: status, message, details
- `DiscoveryResult`: objects (list)

- GET `/connections`
  - Headers: `X-Org-Id`
  - Response: `list[ConnectionProfile]`
  - Service: `connection_service.list_connections(org_id)`
  - Example:
```bash
curl -sS -X GET http://localhost:8000/connections -H "X-Org-Id: org-1"
```

- POST `/connections`
  - Headers: `X-Org-Id`
  - Body: `ConnectionProfileCreate`
  - Response: `ConnectionProfile` (201)
  - Service: `connection_service.create_connection(org_id, payload_dict)`
  - Example:
```bash
curl -sS -X POST http://localhost:8000/connections \
  -H "Content-Type: application/json" -H "X-Org-Id: org-1" \
  -d '{"name":"my-postgres","source_type":"postgres","connection_config":{"host":"db.local","port":5432,"database":"test"}}'
```

- GET `/connections/{connection_id}`
  - Headers: `X-Org-Id`
  - Path: `connection_id`
  - Response: `ConnectionProfile` or 404
  - Service: `connection_service.get_or_404(org_id, connection_id)`

- PUT `/connections/{connection_id}`
  - Headers: `X-Org-Id`
  - Body: `ConnectionProfileCreate`
  - Response: `ConnectionProfile` (200)
  - Service: `connection_service.update_connection(org_id, connection_id, payload_dict)`

- DELETE `/connections/{connection_id}`
  - Headers: `X-Org-Id`
  - Response: 204
  - Service: `connection_service.delete_connection(org_id, connection_id)`

- POST `/connections/{connection_id}/test`
  - Headers: `X-Org-Id`
  - Async
  - Response: `ConnectionTestResult`
  - Service flow: `connection_service.get_or_404(...)` -> `await connection_service.test_connection(connection)`

- POST `/connections/{connection_id}/discover`
  - Headers: `X-Org-Id`
  - Async
  - Response: `DiscoveryResult`
  - Service flow: `get_or_404` -> `await connection_service.discover_objects(connection)`

----

## 4) Pipelines — prefix: `/pipelines`
File: `app/api/routes/pipelines.py`
Note: All endpoints require `X-Org-Id`.

Models
- `PipelineCreate`: name, description (opt), template_id, connection_profile_id, transform_policy_id (opt), schema_version_id (opt), quality_policy_id (opt), zone (validated, one of raw/prepared/curated/quarantine/meta), domain, source_system, entity, mode (validated), version (pattern v<number>), connector_config (dict), runtime_pool_id (opt), status
- `Pipeline`: adds id, org_id, topic_name, connector_name, generated_config
- `PipelineStatus`: pipeline_id, status, connector_name, topic_name, runtime

- GET `/pipelines`
  - Headers: `X-Org-Id`
  - Response: `list[Pipeline]`
  - Service: `pipeline_service.list_pipelines(org_id)`

- POST `/pipelines`
  - Headers: `X-Org-Id`
  - Body: `PipelineCreate`
  - Response: `Pipeline` (201)
  - Service: `pipeline_service.create_pipeline(org_id, payload)`
  - Example:
```bash
curl -sS -X POST http://localhost:8000/pipelines \
  -H "Content-Type: application/json" -H "X-Org-Id: org-1" \
  -d '{"name":"ingest-orders","template_id":"template-1","connection_profile_id":"conn-1","zone":"raw","domain":"sales","source_system":"orders_app","entity":"orders","mode":"cdc","version":"v1"}'
```

- GET `/pipelines/{pipeline_id}`
  - Headers: `X-Org-Id`
  - Response: `Pipeline` or 404

- PUT `/pipelines/{pipeline_id}`
  - Headers: `X-Org-Id`
  - Body: `PipelineCreate`
  - Response: `Pipeline`

- DELETE `/pipelines/{pipeline_id}`
  - Headers: `X-Org-Id`
  - Response: 204

- POST `/pipelines/{pipeline_id}/deploy`
  - Async
  - Headers: `X-Org-Id`
  - Response: `PipelineStatus`
  - Service: `await pipeline_service.deploy(org_id, pipeline_id)`

- POST `/pipelines/{pipeline_id}/pause`
  - Async -> `await pipeline_service.pause(org_id, pipeline_id)`

- POST `/pipelines/{pipeline_id}/resume`
  - Async -> `await pipeline_service.resume(org_id, pipeline_id)`

- POST `/pipelines/{pipeline_id}/restart`
  - Async -> `await pipeline_service.restart(org_id, pipeline_id)`

- GET `/pipelines/{pipeline_id}/status`
  - Headers: `X-Org-Id`
  - Response: `PipelineStatus` (builds status via `pipeline_service.build_status(pipeline)`)

- GET `/pipelines/{pipeline_id}/generated-config`
  - Headers: `X-Org-Id`
  - Response: pipeline[`generated_config`] (dict)

----

## 5) Transform policies — prefix: `/transform-policies`
File: `app/api/routes/transforms.py`
Note: `X-Org-Id` required on create/list/get/update/validate/preview

Models
- `TransformPolicyCreate` / `TransformPolicy`: name, description, transforms (list of `TransformStep`)
- `TransformStep`: order (int), name (str), type (str), scope (default "value"), config (dict)
- `TransformPreviewRequest`: { input_record: dict }
- `ValidationResult` (from `app.schemas.common`) -> { valid: bool, errors: list[str] }

- GET `/transform-policies`
  - Headers: `X-Org-Id`
  - Response: list[`TransformPolicy`]
  - Service/storage: `store.list("transform_policies", org_id=org_id)`

- POST `/transform-policies`
  - Headers: `X-Org-Id`
  - Body: `TransformPolicyCreate`
  - Handler: sets `org_id`, calls `generate_smt_config(transforms)`, then `store.create("transform_policies", data)`
  - Response: `TransformPolicy` (201)

- GET `/transform-policies/{policy_id}`
  - Headers: `X-Org-Id`
  - Response: `TransformPolicy` or 404 if not found or org mismatch

- PUT `/transform-policies/{policy_id}`
  - Headers: `X-Org-Id`
  - Body: `TransformPolicyCreate`
  - Handler upserts via `store.upsert("transform_policies", policy_id, data)`

- DELETE `/transform-policies/{policy_id}`
  - Response: 204 via `store.delete("transform_policies", policy_id)`

- POST `/transform-policies/{policy_id}/validate`
  - Headers: `X-Org-Id`
  - Response: `ValidationResult` — runs `generate_smt_config` and returns valid/errors

- POST `/transform-policies/{policy_id}/preview`
  - Headers: `X-Org-Id`
  - Body: `TransformPreviewRequest` { input_record }
  - Response: { input_record, output_record, generated_config }

----

## 6) Schemas — prefix: `/schemas`
File: `app/api/routes/schemas.py`
Note: `/infer` is unauthenticated; other routes require `X-Org-Id`.

Models
- `SchemaInferRequest`: { records: list[dict] }
- `SchemaInferResult`: { fields: list[SchemaField], confidence: float }
- `SchemaVersionCreate` / `SchemaVersion`: domain, source_system, entity, version (default v1), schema_format (default json_schema), schema_definition (dict), compatibility, status
- `SchemaDrift*` models for drift detection and approvals

- POST `/schemas/infer`
  - Body: `SchemaInferRequest`
  - Response: `SchemaInferResult`
  - Service: `schema_service.infer(payload.records)`

- GET `/schemas`
  - Headers: `X-Org-Id`
  - Response: `list[SchemaVersion]`

- POST `/schemas`
  - Headers: `X-Org-Id`
  - Body: `SchemaVersionCreate`
  - Response: `SchemaVersion` (201)
  - Service: `schema_service.create_schema(org_id, payload_dict)`

- GET `/schemas/drift-approvals`
  - Headers: `X-Org-Id`
  - Query: `status` (optional)
  - Response: list[`SchemaDriftApproval`]

- POST `/schemas/drift-approvals`
  - Headers: `X-Org-Id`
  - Body: `SchemaDriftApprovalCreate`
  - Response: `SchemaDriftApproval` (201)

- POST `/schemas/drift-approvals/{approval_id}/approve`
  - Headers: `X-Org-Id`
  - Body: `SchemaDriftReviewRequest` { reviewed_by, comment }
  - Service: `schema_service.approve_drift(org_id, approval_id, reviewed_by, comment)`

- POST `/schemas/drift-approvals/{approval_id}/reject`
  - Similar to approve but calls `schema_service.reject_drift(...)`

- GET `/schemas/{schema_id}`
  - Headers: `X-Org-Id`
  - Response: `SchemaVersion` or 404

- POST `/schemas/{schema_id}/versions`
  - Headers: `X-Org-Id`
  - Body: `SchemaVersionCreate`
  - Behavior: validates parent via `get_schema`, then `schema_service.create_schema`

- POST `/schemas/{schema_id}/validate-compatibility`
  - Headers: `X-Org-Id`
  - Response: `ValidationResult` via `schema_service.validate_compatibility(get_schema(...))`

- GET `/schemas/{schema_id}/drift-events`
  - Headers: `X-Org-Id`
  - Response: list[dict] via `schema_service.drift_events(get_schema(...))`

- POST `/schemas/{schema_id}/detect-drift`
  - Headers: `X-Org-Id`
  - Body: `SchemaDriftRequest` { records: [...] }
  - Response: `SchemaDriftResult` (compatible, events, approval_required, approval_request_id optional)
  - Handler will create a drift approval via `schema_service.create_drift_approval` if `approval_required` is True and include `approval_request_id` in the response

Example (detect-drift):
```bash
curl -sS -X POST http://localhost:8000/schemas/SCHEMA_ID/detect-drift \
  -H "Content-Type: application/json" -H "X-Org-Id: org-1" \
  -d '{"records":[{"id":"200","amount":99.9}]}'
```

----

## 7) Quality endpoints
File: `app/api/routes/quality.py`
Note: Most endpoints require `X-Org-Id`.

Models
- `QualityPolicyCreate` / `QualityPolicy`: name, description, rules (list), on_failure
- `QualityResult`, `QualityExecutionRequest` { pipeline_id, records }, `StreamingQualityRequest`, `StreamingQualityResult`

- GET `/quality-policies`
  - Headers: `X-Org-Id`
  - Response: list[`QualityPolicy`]

- POST `/quality-policies`
  - Headers: `X-Org-Id`
  - Body: `QualityPolicyCreate`
  - Response: `QualityPolicy` (201)
  - Handler converts `ValueError` from service into HTTP 400

- GET `/quality-policies/{policy_id}`
  - Headers: `X-Org-Id`
  - Response: `QualityPolicy` or 404

- PUT `/quality-policies/{policy_id}`
  - Headers: `X-Org-Id`
  - Body: `QualityPolicyCreate`
  - Response: `QualityPolicy` (400 on ValueError)

- DELETE `/quality-policies/{policy_id}`
  - Response: 204 via `quality_service.delete_policy(policy_id)`

- POST `/quality-policies/{policy_id}/validate`
  - Headers: `X-Org-Id`
  - Response: `ValidationResult` via `quality_service.validate_policy(policy)`

- GET `/quality-results`
  - Headers: `X-Org-Id`
  - Response: list[`QualityResult`] via `quality_service.list_results(org_id)`

- POST `/quality-policies/{policy_id}/execute`
  - Headers: `X-Org-Id`
  - Body: `QualityExecutionRequest` { pipeline_id, records }
  - Response: `QualityResult`
  - Handler checks pipeline existence via `pipeline_service.get_pipeline(org_id, payload.pipeline_id)` and returns 404 if not found

- POST `/quality-policies/{policy_id}/stream/process`
  - Async
  - Headers: `X-Org-Id`
  - Body: `StreamingQualityRequest` { pipeline_id, record }
  - Response: `StreamingQualityResult`
  - Uses `InMemoryKafkaProducer()` in handler and calls `await quality_service.process_streaming_record(...)`

----

## 8) Lineage
File: `app/api/routes/lineage.py`
Note: Endpoints require `X-Org-Id`.

- GET `/assets`
  - Headers: `X-Org-Id`
  - Response: list[`Asset`]
  - Service: `lineage_service.list_assets(org_id)`

- GET `/lineage/{asset_id}`
  - Headers: `X-Org-Id`
  - Path: `asset_id`
  - Response: `LineageGraph` via `lineage_service.graph_for_asset(org_id, asset_id)`

- GET `/lineage/pipeline/{pipeline_id}`
  - Headers: `X-Org-Id`
  - Response: `LineageGraph` via `lineage_service.graph_for_pipeline(org_id, pipeline_id)`

- POST `/lineage/events`
  - Headers: `X-Org-Id`
  - Body: `LineageRuntimeEvent` (contains `org_id` field)
  - Handler will overwrite `event["org_id"]` with header value if mismatched
  - Service: `lineage_service.consume_runtime_event(event)`

----

## 9) Monitoring — prefix: `/runtime`
File: `app/api/routes/monitoring.py`

- GET `/runtime/connect-clusters`
  - Response: list[`ConnectCluster`]
  - Service: `monitoring_service.list_connect_clusters()`

- GET `/runtime/connect-clusters/{cluster_id}/connectors`
  - Path: `cluster_id`
  - Response: list[dict] via `monitoring_service.list_cluster_connectors(cluster_id)`

- GET `/runtime/connectors/{connector_name}/status`
  - Response: dict via `monitoring_service.connector_status(connector_name)`

- GET `/runtime/connectors/{connector_name}/tasks`
  - Response: list[dict] via `monitoring_service.connector_tasks(connector_name)`

- GET `/runtime/metrics/summary`
  - Response: `MetricsSummary` via `monitoring_service.metrics_summary()`

----

## Example payload snippets (copy-paste into tests)

- Connector template
```json
{"name":"Postgres CDC","connector_type":"source","category":"database","connector_class":"io.debezium.connector.postgresql.PostgresConnector","runtime_pool_type":"kafka-connect","config_schema":{},"default_config":{},"default_transforms":[],"version":"v1","is_active":true}
```

- Connection (requires `X-Org-Id` header)
```json
{"name":"my-postgres","source_type":"postgres","connection_config":{"host":"db.local","port":5432,"database":"test"},"status":"draft"}
```

- Pipeline (requires `X-Org-Id` header)
```json
{"name":"ingest-orders","template_id":"template-1","connection_profile_id":"conn-1","zone":"raw","domain":"sales","source_system":"orders_app","entity":"orders","mode":"cdc","version":"v1"}
```

- Transform policy
```json
{"name":"mask-ssn","transforms":[{"order":1,"name":"mask_ssn","type":"mask","config":{"field":"ssn"}}]}
```

- Schema version (requires `X-Org-Id`)
```json
{"domain":"sales","source_system":"orders_app","entity":"orders","version":"v1","schema_format":"json_schema","schema_definition":{"type":"object","properties":{"id":{"type":"string"}}},"compatibility":"BACKWARD","status":"active"}
```

- Quality policy
```json
{"name":"not-null-check","rules":[{"name":"id_not_null","type":"not_null","field":"id"}],"on_failure":"route_to_quarantine"}
```

----

## Next steps
- If you'd like I can add this file to the repo (done) and also:
  - generate pytest test stubs that call each endpoint using `fastapi.testclient.TestClient` and these payloads, or
  - create a Postman/Insomnia collection from the FastAPI `openapi.json`.

If you want pytest stubs, tell me whether you prefer simple smoke tests (status code assertions) or fuller integration tests that assert response schema fields.


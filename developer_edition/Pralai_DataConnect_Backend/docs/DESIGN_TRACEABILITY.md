# InaiEra DataConnect Design Traceability

This file validates the repository against `Inai Era Dataconnect Technical Design.docx`.

## Validation Method

1. Extract design requirements from the DOCX by section.
2. Map each requirement to source files, tests, or explicit backlog status.
3. Treat a requirement as **Implemented** only when code exists and is reachable.
4. Treat a requirement as **Partial** when the route/interface exists but the runtime is stubbed or in-memory.
5. Treat a requirement as **Backlog** when the design calls for production integration that is not yet implemented.

## Backend Service Boundary Matrix

| Design service | Required responsibility | Current repo mapping | Status |
| --- | --- | --- | --- |
| API Gateway / BFF | Frontend API aggregation, org context, auth validation | `backend/app/main.py`, `backend/app/api/router.py`, `backend/app/core/tenant.py` | Partial: org header validation exists; JWT/RBAC backlog |
| Connector Catalog Service | Manage connector templates and plugin metadata | `backend/app/services/catalog_service.py`, `backend/app/repositories/dataconnect.py`, `backend/app/api/routes/catalog.py` | Implemented for CRUD; SQLAlchemy repository available |
| Connection Profile Service | Manage connection profiles, secret refs, test/discover | `backend/app/services/connection_service.py`, `backend/app/services/connection_adapters.py`, `backend/app/api/routes/connections.py` | Implemented for adapter-based test/discovery; live source drivers remain connector-specific work |
| Pipeline Service | Create/update pipelines, naming policy, generated configs | `backend/app/services/pipeline_service.py`, `backend/app/services/pipeline_config.py`, `backend/app/services/topic_naming.py` | Implemented for config generation, state changes, deploy orchestration |
| Runtime Orchestrator Service | Kafka Connect REST APIs, status, restart, task reads | `backend/app/services/runtime_orchestrator.py`, `backend/app/api/routes/monitoring.py` | Implemented client/workflow; live deploy enabled with `DATACONNECT_RUNTIME_DEPLOY_ENABLED=true` |
| Transform Policy Service | Validate SMT compatibility, generate SMT fragments | `backend/app/services/transform_policy.py`, `backend/app/api/routes/transforms.py` | Implemented for supported SMT config generation |
| Schema Service | Infer schemas, store versions, detect drift | `backend/app/services/schema_service.py`, `backend/app/services/schema_inference.py`, `backend/app/services/schema_drift.py`, `backend/app/api/routes/schemas.py` | Implemented field-level drift classification and approval workflow |
| Quality Service | Manage quality policies/results, validation config | `backend/app/services/quality_service.py`, `backend/app/services/quality.py`, `backend/app/services/streaming_quality_processor.py`, `backend/app/services/kafka_runtime.py` | Implemented batch and streaming validation with quarantine producer abstraction |
| Lineage Service | Store assets/edges, serve lineage graph API | `backend/app/services/lineage_service.py`, `backend/app/services/lineage_event_consumer.py`, `backend/app/api/routes/lineage.py` | Implemented pipeline lineage materialization and runtime event consumer |
| Monitoring Service | Aggregate health, lag, throughput, errors | `backend/app/services/monitoring_service.py`, `backend/app/api/routes/monitoring.py` | Implemented status/run aggregation helpers; per-connector Prometheus metrics (`app/services/connector_prometheus_metrics.py`, docs/CONNECTOR_METRICS.md) |

## Core Design Coverage

| Design area | Files | Status |
| --- | --- | --- |
| Multi-tenant org scoping | `backend/app/core/tenant.py`, route dependencies, schemas | Partial: `X-Org-Id` enforced on tenant routes |
| Topic naming spec | `backend/app/services/topic_naming.py`, `backend/tests/test_topic_naming.py` | Implemented |
| Connector naming spec | `backend/app/services/topic_naming.py`, `backend/tests/test_topic_naming.py` | Implemented |
| SMT policy object and config fragment | `backend/app/services/transform_policy.py`, `backend/tests/test_transform_policy.py` | Implemented |
| Connector config generation | `backend/app/services/pipeline_config.py`, `backend/tests/test_pipeline_config.py` | Implemented for MVP |
| PostgreSQL model | `backend/app/db/base.py`, `backend/alembic/versions/0001_initial_dataconnect.py` | Implemented as schema/migration |
| FastAPI API specification | `backend/app/api/routes/*` | Partial: full route surface present, some operations stubbed |
| React/Redux frontend shell | `frontend/src/*` | Implemented shell with main screens |
| Kafka Connect runtime integration | `backend/app/services/runtime_orchestrator.py`, `backend/app/services/pipeline_service.py` | Implemented behind runtime deployment flag |
| Schema drift approval workflow | `backend/app/services/schema_service.py`, `backend/app/schemas/schema.py`, `backend/alembic/versions/0002_runtime_workflows.py` | Implemented |
| Streaming quality validation and quarantine | `backend/app/services/streaming_quality_processor.py`, `backend/app/services/kafka_runtime.py`, `backend/tests/test_streaming_quality_processor.py` | Implemented |
| Runtime lineage event consumer | `backend/app/services/lineage_event_consumer.py`, `backend/tests/test_lineage_event_consumer.py` | Implemented |
| Observability | `backend/app/services/monitoring_service.py` | Partial: aggregation helpers exist; metrics ingestion backlog |
| Security/RBAC/secrets | `backend/app/core/tenant.py`, schema fields | Backlog beyond secret references |

## Known Gaps Before Production

- Wire FastAPI route dependency injection to SQLAlchemy repositories for all CRUD paths.
- Add JWT validation and RBAC permissions from the design.
- Add live connector drivers for database metadata discovery and private file systems.
- Add Prometheus/OpenTelemetry metrics ingestion.
- Add Kafka/Connect/Kubernetes/Helm deployment manifests.

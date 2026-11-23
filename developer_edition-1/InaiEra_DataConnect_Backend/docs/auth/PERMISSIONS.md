# DataConnect permissions (task 24)

DataConnect authorises on **permissions** only (contract: [01-CONTRACT.md](01-CONTRACT.md)). The IDs are declared in
[`permissions.json`](../../permissions.json) at the repo root — the file the Admin Portal imports — and nowhere else:
`app/core/permissions.py` loads it, and `require("dataconnect:...")` refuses an undeclared ID at import time.

## Gate

| Layer | Where | Result |
| --- | --- | --- |
| Gateway principal on every route | `app/core/gateway.py` → `gateway_auth.protect_app` | 401 without `TRUST_ENVOY_HEADERS=true` + matching `x-inaiera-gateway-secret` + `x-user-name` |
| Public paths (no principal) | `gateway.public_paths()` | `/api/v1/dataconnect/health`, `/api/v1/dataconnect/health/*` (live, ready); `/api/v1/dataconnect/alerts/actions/*` (the signed Slack / Teams Acknowledge / Resolve links: the token is the credential, every token is refused without `DATACONNECT_ALERTS_ACTION_SECRET`); `/metrics` only with `DATACONNECT_METRICS_PUBLIC=true` |
| Per-route permission | `Depends(require("dataconnect:<area>:<action>"))` | 403 without it |
| Tenant | `app/core/tenant.py` | `principal.tenant_id` (`x-tenant-id`); `X-Org-Id` is no longer read |
| Audit actor | `app/core/actor.py` | `principal.username` (`x-user-name`); `X-Actor` is no longer read |
| `GET /api/v1/dataconnect/me` | `app/api/routes/me.py` | `principal.me()` → `{user, userId, tenant, permissions}`; principal required, no permission required |

`GET /access/capabilities` (the role/capability matrix) is removed; the UI reads `/me`.

Local development: `DATACONNECT_ENV=development` + `DATACONNECT_DEV_PERMISSIONS=all` (or a comma list), with
`TRUST_ENVOY_HEADERS` unset, serves every request as one fixed principal (`DATACONNECT_DEV_USER` /
`DATACONNECT_DEV_TENANT`). No request header is read for it. Startup refuses `DATACONNECT_DEV_PERMISSIONS` outside
development, and refuses the dev principal when `DATACONNECT_ENV` was never set explicitly.

## Capability → permission mapping

Every former `Capability` (the removed `app/core/roles.py`) and the permission that replaces it.

| Former capability | Permission | Notes |
| --- | --- | --- |
| `view_connections` | `dataconnect:connections:view` | |
| `manage_connections` | `dataconnect:connections:edit` | |
| `delete_connections` | `dataconnect:connections:edit` | delete folded into edit (same holders before) |
| `test_connections` | `dataconnect:connections:test` | |
| `discover_source_schemas` | `dataconnect:connections:test` | discovery is a live probe through the connection (same holders) |
| `view_pipelines` | `dataconnect:pipelines:view` | also `GET /operations/{id}` |
| `manage_pipelines` | `dataconnect:pipelines:edit` | |
| `delete_pipelines` | `dataconnect:pipelines:edit` | delete folded into edit |
| `deploy_pipelines` | `dataconnect:pipelines:deploy` | |
| `control_pipelines` | `dataconnect:pipelines:operate` | pause / resume / restart |
| `view_generated_config` | `dataconnect:pipelines:deploy` | the config a pipeline deploys; also filters `generated_config` out of pipeline and audit responses (`app/core/response_filters.py`) |
| `view_transforms` | `dataconnect:transforms:view` | |
| `manage_transforms` | `dataconnect:transforms:edit` | includes validate / preview |
| `view_topic_policies` | `dataconnect:pipelines:view` | topic policies are pipeline configuration |
| `manage_topic_policies` | `dataconnect:pipelines:edit` | |
| `view_schemas` | `dataconnect:schemas:view` | |
| `manage_schemas` | `dataconnect:schemas:edit` | **new ID** (not in the seed) |
| `approve_schema_drift` | `dataconnect:schemas:approve-drift` | |
| `view_quality_policies` | `dataconnect:quality:view` | |
| `manage_quality_policies` | `dataconnect:quality:edit` | |
| `view_quality_results` | `dataconnect:quality:view` | |
| `view_lineage` | `dataconnect:lineage:view` | |
| `manage_lineage` | `dataconnect:lineage:edit` | **new ID** (not in the seed); `POST /lineage/events` |
| `view_monitoring` | `dataconnect:monitoring:view` | |
| `view_connector_templates` | `dataconnect:templates:view` | |
| `manage_connector_templates` | `dataconnect:templates:manage` | |
| `view_settings` | `dataconnect:settings:view` | UI only |
| `manage_settings` | `dataconnect:settings:edit` | `GET /diagnostics/bundle` |
| `view_audit` | `dataconnect:audit:view` | |
| `view_alerts` | `dataconnect:alerts:view` | **new ID** (not in the seed); alerts, counts, rules, channels (webhook URLs never returned) |
| `ack_alerts` | `dataconnect:alerts:respond` | **new ID** (not in the seed); acknowledge / resolve on the Alerts page |
| `manage_alerts` | `dataconnect:alerts:manage` | **new ID** (not in the seed); rules and Slack / Teams channels (a webhook URL is a credential), send test, run rules now |
| (gateway tier "no-access") | `dataconnect:access:view` | UI/portal only: the app shows a "no access" screen without it |
| — | `dataconnect:dashboard:view` | UI only: the Dashboard page |

## Seed delta

The seeded roles (`seed/roles-permissions.json`) reproduce the old Admin / Integrator / Viewer behaviour exactly
**except** for the IDs this repo adds. To keep today's behaviour, grant them as follows:

| Permission | admin | integrator | viewer |
| --- | --- | --- | --- |
| `dataconnect:schemas:edit` | yes | yes | |
| `dataconnect:lineage:edit` | yes | yes | |
| `dataconnect:alerts:view` | yes | yes | yes |
| `dataconnect:alerts:respond` | yes | yes | |
| `dataconnect:alerts:manage` | yes | | |

`tests/personas.py` encodes the seeded sets plus this delta; `tests/test_rbac.py` proves the result.

## Route coverage

113 routes. `tests/test_route_permission_coverage.py` walks the real route table and fails if any route other than
the seven below lacks a `require(...)`, if a route requires more than one permission, or if a declared permission is
neither enforced by a route nor in the UI-only list (`access:view`, `dashboard:view`, `settings:view`).

No permission (7): `GET /health`, `GET /health/live`, `GET /health/ready` (public), `GET` / `POST /alerts/actions/{token}`
(public, signed token), `GET /metrics` (gateway, public only by opt-in), `GET /me` (principal required).

| Permission | Routes |
| --- | --- |
| `connections:view` | 4 (list/get connections, connection-config-schemas ×2) |
| `connections:edit` | 3 (create, update, delete) |
| `connections:test` | 7 (test-connection ×3, discover, discovery runs/objects/facets) |
| `pipelines:view` | 7 (list, get, status, topic-name-preview, operations, topic-policies list/get) |
| `pipelines:edit` | 6 (create, update, delete, topic-policies create/update/delete) |
| `pipelines:deploy` | 3 (deploy, deploy-readiness, generated-config) |
| `pipelines:operate` | 3 (pause, resume, restart) |
| `schemas:view` / `:edit` / `:approve-drift` | 4 / 6 / 2 |
| `quality:view` / `:edit` | 6 / 9 |
| `lineage:view` / `:edit` | 3 / 1 |
| `transforms:view` / `:edit` | 2 / 5 |
| `templates:view` / `:manage` | 3 / 3 |
| `monitoring:view` | 10 |
| `settings:edit` | 1 |
| `audit:view` | 1 |
| `alerts:view` / `:respond` / `:manage` | 7 / 2 / 8 |

## Outbound: DataConnect → UCS

`app/services/ucs_lineage_sync.py` (`ucs_service_headers`) calls UCS without Envoy in front, so it sends what Envoy
would: `X-Inaiera-Gateway-Secret` (`DATACONNECT_UCS_GATEWAY_SECRET`), `X-User-Name` (`DATACONNECT_UCS_LINEAGE_SYNC_USER`,
a service identity), `X-Tenant-Id`, and `X-User-Permissions` with exactly one permission: `ucs:lineage-events:create`,
because every call goes to `POST /api/v1/lineage/events` — lineage events and quality results alike
(`app/services/ucs_quality_sync.py` sends its results there as an OpenLineage `dataQualityAssertions` facet). UCS keeps
`ucs:quality-results:create` for its own `/quality/results` endpoints, which DataConnect does not call; sending it to
`/lineage/events` got every quality result a 403. The old `X-User-Roles: service` marker is gone; no role header is sent.

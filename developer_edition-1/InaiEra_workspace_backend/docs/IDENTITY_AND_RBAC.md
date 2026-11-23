# Identity and permissions (X-002, X-003, W-009, W-020)

## One identity: the control plane's immutable subject

A person is the token's `sub` (a UUID), everywhere: Workspace ownership, the `sub` of every delegated token, the runners' and DQ's actor
and audit columns, the frozen execution manifests. The username / email / display name are **attributes** that can change without changing who
the person is.

* The gateway must inject `x-user-id` (= the token's `sub`). Workspace refuses a request without it outside dev (`401 MISSING_SUBJECT`) and one
  whose value is not a UUID (`401 INVALID_SUBJECT`). In dev only, a gateway that does not send it yet falls back to the username with a warning
  (`REQUIRE_USER_SUBJECT` overrides either way). It is an `x-user-*` header, so the gateway strips any client-supplied one.
* **Gateway change needed (the shared GatewayAuth repo, read-only for this work):** add to `claim_to_headers` in
  `envoy/_gateway-auth.tpl.yaml`:
  ```yaml
  - header_name: "x-user-id"
    claim_name: "sub"
  ```
  Until the production gateway sends it, a production Workspace refuses every request: deploy the gateway change first (the local dev gateway,
  `InaiEra_workspace_frontend/dev-gateway.ts`, already sends it).
* Rows created before this keyed users by username. The first request that carries both the subject and the username re-keys that row **in place**
  (same row id, so workspaces, sessions, executions and Git credentials stay the person's; their Git tokens move to the subject's secret path).
  Caveat: if a username was reused by a different person before the original person's first login after the change, that person would adopt the
  row. Runner rows written earlier keep the ids they were written with.
* UCS still knows people by username (`X-User-Name`), because that is how its grants are keyed; Workspace keeps sending it there.

## Permissions, not roles (contract `docs/auth/01-CONTRACT.md`)

Workspace holds no role names. Roles, and what they grant, are data owned by the Admin Portal; the Data Plane ticket carries the user's
**permissions** and Envoy passes them as `x-user-permissions` (base64 JSON array). The gateway-auth library (v1.1) reads them into the
principal (`app/core/deps.py`); `x-user-roles` is kept for display/audit only and decides nothing.

* The IDs (`workspace:<area>:<action>`) are declared once, in `permissions.json` at the repo root (contract section 7), which the Admin Portal
  imports. `app/core/rbac.py` loads it; a route can only require a published ID. `contracts/rbac.contract.v1.json` is the generated
  contract: every ID and where it is enforced again downstream (DQ's `dq.*` vocabulary, the runners' identical IDs). It has no roles.
* Deny by default: `protect_app` (app/main.py) answers 401 on every route except the public list (`/healthz`, `/readyz`, `/config.json`,
  logout, Git webhooks and OAuth callbacks, the runner-only internal SQL job route — each verifies itself or returns nothing personal).
* Every Workspace route needs `workspace:access:view` plus its own permission; missing = 403. `GET /api/v1/me` returns
  `{ user, userId, tenant, permissions }` to any gateway-verified caller, so the UI can show "no access".
* Delegated tokens to the runners carry `permissions` (the caller's permissions that the token's scope needs) instead of roles; the runners
  check those IDs, and the dbt runner checks the one for the job's kind (dbt / SQL job / Python job).

| Former capability | Permission |
|---|---|
| (every caller) | `workspace:access:view`, `workspace:files:edit` |
| `catalog_browse` | `workspace:catalog:view` |
| `dataset_preview` | `workspace:datasets:view` |
| `sql_execute` | `workspace:sql:run` |
| `notebook` | `workspace:notebooks:run` |
| `dbt_edit` / `dbt_execute` / `dbt_production_deploy` | `workspace:dbt:edit` / `workspace:dbt:run` / `workspace:dbt:deploy` |
| (SQL / Python job kinds, formerly dbt_edit / dbt_execute) | `workspace:sql-jobs:edit|run`, `workspace:python-jobs:edit|run` |
| `flink_edit` / `flink_deploy` | `workspace:flink:edit` / `workspace:flink:deploy` |
| `dq_read` / `dq_rule_create` / `dq_rule_edit` / `dq_execute` / `dq_publish` / `dq_admin` | `workspace:dq:view` / `:create` / `:edit` / `:run` / `:publish` / `:manage` |
| `git_commit`, `git_push` | `workspace:git:write` |
| `connections_manage` | `workspace:connections:manage` |
| (the Settings page: engines, active assistant) | `workspace:settings:edit` |
| (former admin-only checks: codegen agent and key, context-egress governance, engine endpoints) | `workspace:settings:manage` |

`metadata_edit`, `certification` and `workspace_users_roles` had no endpoint or UI and are not published.

Adding a permission: add it to `permissions.json`, use it in a route (`require_permission`), run
`python scripts/export_rbac_contract.py contracts/rbac.contract.v1.json`, copy `permissions.json` to the frontend's `openapi/` and run
`npm run codegen:rbac`; if a runner enforces it, add it to that runner's `app/core/permissions.py`. The contract tests name whatever you missed.

## Logout (W-004)

`POST /api/v1/auth/logout` needs no authentication: it always deletes the session cookie (same attributes it was set with) and returns 200,
whatever credential — or none — it is given; the audit event is recorded only when the gateway vouches for a caller. For this to work end
to end the **gateway must let that one route through without a valid token** (Envoy: no `jwt_authn` requirement for `POST /api/v1/auth/logout`,
or `allow_missing_or_failed`); otherwise an expired token is refused at the gateway with a 401 before Workspace can clear the cookie. The
local dev gateway (`dev-gateway.ts`) already does this. The shared GatewayAuth template is read-only for this work.

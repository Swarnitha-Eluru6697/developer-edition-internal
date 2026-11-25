# InaiEra permission contract (shared by every repo)

This file is the single source of truth for how identity and authorisation work between the Control Plane
and every Data Plane application. Every repo-specific task file refers to it. Read it first.

## 1. The rule

**No application, library or gateway contains role names.** Roles are data, owned by the Admin Portal.
Applications check **permissions** only. A new role, or a change to what a role may do, must require
**no code change and no redeploy** in any Data Plane application.

## 2. Flow

```
Admin Portal (roles -> permissions, users -> roles)
  -> Keycloak (permissions = client roles of client "inaiera-dataplane"; Admin Portal roles = composite realm roles)
  -> Data Plane ticket (JWT) claims:  permissions: [...], tenant: "<org>", preferred_username, groups
  -> Envoy (verifies the ticket; strips client-supplied identity headers; sets headers ONLY from claims)
  -> Application (gateway-auth library: verify gateway secret, read identity + permissions, check permission)
```

## 3. Permission IDs

Format: `<app>:<area>:<action>`, lowercase, words separated by `-`.

| Part | Values |
|---|---|
| `<app>` | `ucs`, `dataconnect`, `mlstudio`, `agent`, `workspace` |
| `<area>` | a page or resource of that app, e.g. `clusters`, `pipelines`, `rbac`, `settings` |
| `<action>` | `view`, `edit`, `create`, `delete`, `run`, `deploy`, `approve`, `manage` (use the smallest set that fits) |

Examples: `mlstudio:clusters:view`, `mlstudio:train:run`, `dataconnect:pipelines:deploy`, `ucs:rbac:manage`,
`agent:settings:edit`, `workspace:dbt:run`.

Every app also has **`<app>:access:view`**: "may open this application at all". The portal shows an app's
menu entry only when the user holds it.

Each app repo publishes its full list in **`permissions.json`** at the repo root (schema in section 7). The
Admin Portal imports these lists; they are the only place permission IDs are declared.

## 4. Headers set by Envoy (and only by Envoy)

| Header | From claim | Format |
|---|---|---|
| `x-user-name` | `preferred_username` | string |
| `x-user-id` | `sub` | string (immutable user id) |
| `x-tenant-id` | `tenant` | string |
| `x-user-groups` | `groups` | base64-encoded JSON array |
| `x-user-permissions` | `permissions` | base64-encoded JSON array |
| `x-inaiera-gateway-secret` | (Envoy config) | shared secret, overwritten, never appended |

`x-user-roles` (from `realm_access.roles`) stays **for display/audit only**. Applications must not authorise on it.
Envoy removes every one of these headers from incoming client requests before setting its own.

Parsing rule for list headers (same in every library): accept base64-encoded JSON array, raw JSON array,
or comma-separated list. Unknown/invalid input = empty list.

## 5. What every application must do

1. **Deny by default.** Every route requires a gateway-verified principal. Without `TRUST_ENVOY_HEADERS=true`
   AND a matching `x-inaiera-gateway-secret` (constant-time compare against `ENVOY_INTERNAL_SECRET`), answer **401**.
2. **Public routes** are an explicit, short allow-list in one place: health/readiness probes
   (`/health`, `/healthz`, `/livez`, `/readyz`, `/api/health`), static assets, and inbound webhooks that verify
   their own signature. Nothing else.
3. **Authorise by permission**: each route/action declares the permission it needs; missing permission = **403**.
4. **The UI** shows/hides pages from the permissions the backend returns (`GET <api>/me` -> `{ user, tenant, permissions }`).
   UI hiding is convenience only; the API check is the real control.
5. **Never** read `x-user-role` (singular), never accept identity from any other header, never fall back to a
   privileged default. The least-privileged state is "no permissions".
6. Use the **gateway-auth library** (Python or Node), not a local re-implementation.

## 6. Standard acceptance tests (run inside the cluster; every app must pass)

```
# A. bypassing Envoy (no secret)               -> 401
curl -s -o /dev/null -w '%{http_code}' http://<app-backend>:<port>/<any protected route>
# B. wrong secret                              -> 401
curl ... -H 'x-inaiera-gateway-secret: wrong'
# C. right secret, no permission header        -> 403 (or 401 if no identity)
# D. right secret + identity + needed permission -> 200
# E. right secret + identity + only another app's permission -> 403
# F. forged x-user-role / x-user-roles with admin values, no permissions -> 403
# G. health probe without anything             -> 200
```

## 7. `permissions.json` schema (one per app repo)

```json
{
  "app": "mlstudio",
  "version": 1,
  "permissions": [
    { "id": "mlstudio:access:view",   "label": "Open ML Studio",        "description": "..." },
    { "id": "mlstudio:clusters:view", "label": "View compute clusters", "description": "..." }
  ]
}
```

## 8. Definition of done (whole platform)

- `grep` for role names (e.g. `admin`, `viewer`, `builder`, `platform-admin`, `ml-engineer`, `DataConnect Admin`)
  in application code returns nothing outside tests and seed data.
- A role created in the Admin Portal, given permissions, and assigned to a user takes effect at that user's
  next login, with no code change.
- All acceptance tests in section 6 pass for every app.

## 9. Library (InaiEra_dataplane_GatewayAuth, branch feature/permission-auth, tags v1.1.0-rc.1 / node-v1.1.0-rc.1)

Python: `from gateway_auth import protect_app, require_permission, get_principal, has_permission`
- `protect_app(app, public_paths=["/health", "/static/*"])` -> 401 on every other route unless via Envoy
- `Depends(require_permission("app:area:action"))` -> 401 / 403
- `principal = Depends(get_principal)`; `principal.has(p)`, `principal.permissions`, `principal.user_id`,
  `principal.username`, `principal.tenant_id`, `principal.me()` -> `{user, userId, tenant, permissions}`
- Install: `inaiera-gateway-auth @ git+https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git@v1.1.0-rc.1#subdirectory=python`
  (local testing: `pip install -e <path-to>/InaiEra_dataplane_GatewayAuth/python`)

Node: `import { gatewayAuth, requirePermission, hasPermission, meBody } from "@aadhyaanalyticsinc/gateway-auth"`
- `app.use(gatewayAuth({ publicPaths: ["/api/health"] }))` before every router; sets `req.principal`
- `requirePermission("app:area:action")` route middleware -> 401 / 403
- `hasPermission(req.principal, p)`, `meBody(req.principal)`
- Package `@aadhyaanalyticsinc/gateway-auth@1.1.0-rc.1` on GitHub Packages (npm.pkg.github.com).
  Local testing: `npm install --no-save <path-to>/InaiEra_dataplane_GatewayAuth/node`
  after `npm run build` there.

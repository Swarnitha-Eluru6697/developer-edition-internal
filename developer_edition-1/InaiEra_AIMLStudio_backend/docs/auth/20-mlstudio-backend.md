# App task 20 (from the role-free authorisation pack, TASKS-APPS.md). Read 01-CONTRACT.md first.

## 20 — InaiEra_AIMLStudio_backend: gateway-auth + permissions, no role names

Problems today (verified in the Azure Data Plane):
- `src/middleware/role.ts` reads `X-User-Role` (singular). Envoy strips that header, so every user is `ml-viewer`
  even with `ml-platform-admin` in the ticket.
- No gateway-secret check: a request that bypasses Envoy is accepted (`/api/whoami` returns 200 from another pod).
- Role names hard-coded: `ALL_ROLES`, `ROLE_TIER`, and role checks in routes.

Changes:
1. Add `permissions.json` (contract §7) covering every page and action. Starting point (pages from the roles
   document): `mlstudio:access:view`, `mlstudio:dashboard:view`, `mlstudio:model-overview:view`,
   `mlstudio:model-catalog:view`, `mlstudio:train:run`, `mlstudio:datasets:view`, `mlstudio:datasets:upload`,
   `mlstudio:pipelines:view`, `mlstudio:endpoints:view`, `mlstudio:experiments:view`, `mlstudio:deploy:promote`,
   `mlstudio:alerts:view`, `mlstudio:alerts:edit`, `mlstudio:clusters:view`, `mlstudio:base-models:sync`,
   `mlstudio:gpu:train-large`, `mlstudio:models:delete`, `mlstudio:models:share`, `mlstudio:compute:approve`.
   Add any further actions the code performs.
2. Use the gateway-auth Node library: app-wide `gatewayAuth({ publicPaths: ["/api/health"] })` before all routers;
   each route: `requirePermission("mlstudio:<area>:<action>")`.
3. Delete `ALL_ROLES`, `ROLE_TIER`, the `Role` type and every `req.role` comparison; replace with permission checks.
4. `GET /api/whoami` (or `/api/me`) returns `{ user, userId, tenant, permissions }` from the principal.
5. Calls to UCS keep sending UCS's gateway secret (`UCS_GATEWAY_SECRET`); no role mapping to UCS groups by
   role name — if UCS needs groups, pass the user's identity/permissions as the UCS contract defines.

Acceptance: contract §6 tests A–G against `http://mlstudio-backend:4000` (protected route e.g. `/api/models`,
health `/api/health`). A user whose ticket has `mlstudio:clusters:view` gets 200 on the clusters API; without it 403.
`grep -rn -E "ml-viewer|ml-engineer|ml-platform-admin|gpu-quota-approver|X-User-Role" src` returns nothing outside tests.


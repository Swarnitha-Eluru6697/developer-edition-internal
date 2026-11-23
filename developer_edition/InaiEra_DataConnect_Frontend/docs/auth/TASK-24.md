## 24 — InaiEra_DataConnect_Backend + InaiEra_DataConnect_Frontend

DataConnect already uses gateway-auth (Python) and refuses requests without the gateway secret.

Backend:
1. Add `permissions.json`: `dataconnect:access:view`, `dataconnect:dashboard:view`, `dataconnect:monitoring:view`,
   `dataconnect:connections:view|edit|test`, `dataconnect:pipelines:view|edit|deploy|operate`,
   `dataconnect:schemas:view|approve-drift`, `dataconnect:quality:view|edit`, `dataconnect:lineage:view`,
   `dataconnect:transforms:view|edit`, `dataconnect:templates:view|manage`, `dataconnect:settings:view|edit`,
   `dataconnect:audit:view`.
2. Upgrade gateway-auth to v1.1; replace every role-name check ("DataConnect Admin", "Integrator", "Viewer",
   `dataconnect-admin`, ...) with `require_permission(...)`; keep the app-wide gateway principal (public: health only).
3. `GET /api/v1/dataconnect/me` -> `{ user, userId, tenant, permissions }`. Tenant comes from the principal
   (`x-tenant-id`), not from a client header like `X-Org-Id`.

Frontend:
1. Remove the `role` prop and every role-name condition; use `permissions` from `/me`.
2. Keep federation (`dataconnect_mfe`, `./DataConnectApp`) and the shared-React fix (`src/federation/*` aliases).
3. Do not send `X-Org-Id` / `X-Actor` for authorisation; identity comes from Envoy.

Acceptance: contract §6 A–G against `/api/v1/dataconnect/...` (health `/api/v1/dataconnect/health`). No role names
in either repo outside tests.


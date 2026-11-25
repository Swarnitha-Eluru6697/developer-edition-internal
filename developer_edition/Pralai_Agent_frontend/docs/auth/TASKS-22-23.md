# App task files (20-26). Read 01-CONTRACT.md first.

## 22 — Pralai_agent_backend: deny by default + permissions, no role names

Problems today: `GET /api/agents`, `/api/runs`, `/api/templates` answer 200 without the gateway secret.
`gateway_identity.get_principal` exists but routers do not depend on it; identity is only checked when an agent
opts in (`enforce_authenticated_user`). Role names hard-coded in `app/gateway_identity.py` (`ROLES`: `no-access`,
`builder`, `security-admin`, `platform-admin`, tiers).

1. Add `permissions.json`. Starting point: `agent:access:view`, `agent:overview:view`, `agent:studio:edit`,
   `agent:templates:view`, `agent:templates:manage-platform`, `agent:rag:manage`, `agent:integrations:manage`,
   `agent:runs:view`, `agent:traces:view`, `agent:monitoring:view`, `agent:guardrails:manage`, `agent:alerts:manage`,
   `agent:webapps:use`, `agent:settings:edit`, `agent:agents:run`, `agent:agents:publish`. Add further actions as needed.
2. Upgrade to gateway-auth v1.1 (Python). Enforce the principal app-wide:
   `protect_app(app, public_paths=["/livez", "/readyz", "/embed.js", <webhook paths that verify signatures>])`. Includes `/mcp`.
3. Each router/route: `Depends(require_permission("agent:<area>:<action>"))`.
4. Delete `ROLES`, `role_from_gateway_headers`, tier logic; `agent_guard.py` and any `x-user-groups`/role-name
   checks move to permissions. Keep the legacy `OIDC_*` header path disabled (remove it if unused).
5. `GET /api/me` returns `{ user, userId, tenant, permissions }`.
6. Keep: `db.py` creates the `vector` extension only if missing.

Acceptance: contract §6 A–G against `http://agenticworkflow-backend:8000` (protected `/api/agents`, health `/livez`).
`curl http://agenticworkflow-backend:8000/api/agents` (no secret) -> 401. No role names in `app/` outside tests.

## 23 — Pralai_Agent_frontend: pages from permissions

1. Remove the persona/role switcher and every role-name condition.
2. Load `${BASE}/api/me` once; show pages/actions from `permissions` (IDs from the backend's `permissions.json`).
3. Keep Module Federation: `name: 'agenticworkflow_mfe'`, `exposes: { './AgenticWorkflowApp': ... }`,
   `shared: ['react','react-dom']`, `target: 'esnext'`, `cssCodeSplit: false`; keep `apiBase` handling in `src/api.ts`.
4. `nginx.conf`: static files only — remove the `/api`, `/mcp`, `/embed.js` `proxy_pass` blocks.
5. The vite dev proxy may keep dev-only identity injection (`withGatewayHeaders`) but it must never be in the build.

Acceptance: build has `remoteEntry.js` exposing `./AgenticWorkflowApp`; a user with only `agent:webapps:use` sees
only Web Apps; no role names in `src`.


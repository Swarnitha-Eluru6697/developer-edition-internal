# Workspace frontend — contract package

This is the **contract version** of the InAiEra Workspace frontend. It demonstrates the integration points that any production UI must implement, deliberately leaving polish, accessibility, and full-feature coverage to a real frontend team.

What it locks down:

- **JWT handoff from control plane** (`src/api/auth.ts`) — captures a one-time `#token=` **fragment** on first load (a `?token=` query string is never accepted), exchanges it at `POST /api/v1/auth/exchange` for an HttpOnly session cookie, strips it from the URL, and redirects to the control plane on 401. The token is never held in JS-readable storage.
- **Untrusted notebook output** (`src/features/notebook/components/SandboxedHtml.tsx`, `htmlOutput.ts`) — a cell's `text/html` output (a DataFrame, a plot, anything a kernel or a shared notebook emits) is never injected into the app's DOM. It renders in an iframe with an empty `sandbox` (no scripts, no same-origin, no popups/forms/top navigation) and a CSP that blocks every network load, so `<img onerror>`, SVG handlers and `javascript:` links cannot run or reach the app's origin and its authenticated API. `tests/features/notebookOutputs.test.tsx` also fails if any source file uses `dangerouslySetInnerHTML`, `innerHTML =`, `document.write`, `eval` or `new Function`.
- **RTK Query API client** (`src/api/client.ts`) — typed wrappers for every `/api/v1/*` endpoint exposed by the backend.
- **WebSocket middleware** (`src/ws/middleware.ts`) — connects to `/ws/sessions/{id}` over the `workspace.v1` subprotocol, pings every 25s, reconnects with exponential backoff, re-dispatches incoming envelopes as Redux actions of the form `ws/<envelope.type>` so slices can reduce on them.
- **Execution slice** (`src/store/executionSlice.ts`) — accumulates streamed schema + row batches for the active execution; switches state on `execution.succeeded`/`execution.failed`/`execution.cancelled`.
- **Monaco editor wrapper** (`src/components/CodeEditor.tsx`) — Cmd/Ctrl-Enter to run, Cmd/Ctrl-S to save; language-agnostic so it serves SQL and notebook cells alike.
- **App shell** (`src/components/App.tsx`) — the three-pane layout matching the diagram: file tree · editor · results.

Type definitions in `src/types/index.ts` mirror the backend Pydantic schemas. In a real build these would be generated from the FastAPI OpenAPI spec via `openapi-typescript`.

## Build & serve

In production the frontend is built (`npm run build`) and the resulting `dist/` is baked into the same container image as the backend. The FastAPI app mounts it at `/` (static), so the user hits `https://orgid.app.workspace.inaiera.com/` and gets the SPA, which then calls back to `/api/v1/*` on the same host.

For local dev:

```bash
cd frontend
npm install
npm run dev  # vite serves at http://localhost:5179
# In another terminal:
docker compose -f ../docker/docker-compose.yml up
```

## What's deliberately not here

- A real router with deep links into files, executions, branches
- The notebook UI (cells, outputs, kernel state) — Phase 1 includes notebooks but the cell view is significant additional surface
- Catalog-driven autocomplete provider for SQL (the hook is stubbed)
- A theme that isn't bare-bones dark
- Tests

## Runtime configuration and permissions

* **Control-plane login URL (W-006).** The page where a user logs in again after a 401 is not a build-time constant. At start-up the SPA reads
  `GET /config.json` (`{ "controlPlaneLoginUrl": "https://…/login" }`): the Workspace backend serves it from `CONTROL_PLANE_LOGIN_URL`, or
  derives it from `CONTROL_PLANE_ISSUER` (`<issuer>/login`), so a staging, private-cloud or white-label deployment only sets that. Anywhere else
  the SPA is hosted, `public/config.json` is the same file and can be replaced per deployment without a rebuild. Fallbacks: the build-time
  `VITE_CONTROL_PLANE_LOGIN_URL`, then `https://www.app.inaiera.com/login`. Only absolute http(s) URLs are accepted; a missing or unreachable file never blocks start-up.
* **Permissions.** The UI shows or hides pages and actions from the caller's permissions, loaded once from `GET /api/v1/me`
  (`{ user, userId, tenant, permissions }`; src/rbac/hooks.ts `usePermissions` / `useHasPermission`). Loading, errors and unknown IDs mean
  "no permissions". Without `workspace:access:view` the app shows a "no access" screen. The IDs are generated from `openapi/permissions.json`
  (a copy of the backend's `permissions.json`; `npm run codegen:rbac`, checked by `npm run codegen:rbac:check` and tests/permissions.test.ts).
  There are no roles in this repo: roles are data in the Admin Portal. Locally, dev-gateway.ts gives the dev token every permission.

## Inside the Control Portal (federated remote)

The portal loads `workspace_mfe/WorkspaceApp` (src/WorkspaceApp.tsx) from `remoteEntry.js` and draws a narrow icon rail at the far
left; everything to its right is the Workspace's own chrome. Props (`WorkspaceAppProps`):

| Prop | Meaning |
| --- | --- |
| `apiBase` | Same-origin tunnel path to the Workspace API (e.g. `/control_plane/api/dataplane/workspace-api`). |
| `orgId`, `canEdit` | Shared remote contract; not used yet (the API enforces access itself). |
| `roleBadges` | `ReactNode`, display only: the portal's strip of Workspace role names (the user's own highlighted). Drawn at the right of the header on one line (shrinks / scrolls sideways when narrow). Never inspected — access stays permission-based (`GET /api/v1/me`). |
| `viewPermissions` | `string[] \| null`, "view as role": the permission IDs of the role the user picked in the portal's role strip; `null` / `undefined` = "All roles" (no narrowing; also what older portals send). The UI shows what `held ∩ viewPermissions` opens — never a permission the user lacks — and updates in place when it changes; a page no longer allowed falls back to Workspace. Applied in src/rbac/hooks.ts only (src/rbac/viewAs.ts holds it in React context). UX only: never sent to the API, which still authorises on the ticket. Opening the app at all (`workspace:access:view`) is checked on the full set. |
| `accountMenu` | Deprecated, older portals only: the host's account menu, drawn at the right of the header when given. |

Passing `roleBadges` (anything but `undefined`) means "hosted by the portal": the header leaves out its notification bell and user
avatar, because the portal rail carries both. Standalone (`npm run dev`, `roleBadges` undefined) keeps the bell and the avatar. The
"InAiEra Workspace · Data Platform" mark stays at the top of the Workspace sidebar, beside the portal rail's InaiEra logo.

# App task 21 (from the role-free authorisation pack, TASKS-APPS.md). Read 01-CONTRACT.md first.

## 21 — Pralai_AIMLStudio_frontend: pages from permissions

1. Remove every role name and role-based page/menu condition (persona/role switchers, `role === ...` checks).
2. On start, call `${apiBase}/api/whoami` (or `/me`) and keep `permissions` in state; show a page/action only
   if the user holds its permission (same IDs as the backend's `permissions.json`). Unknown = hidden.
3. Keep Module Federation exactly: `name: "mlstudio_mfe"`, `filename: "remoteEntry.js"`,
   `exposes: { "./MLStudioApp": "./src/MLStudioApp.tsx" }`, `shared: ["react","react-dom"]`,
   `build: { target: "esnext", cssCodeSplit: false }`; keep `MLStudioApp` props (`apiBase`, `embedded`, `initialPath`).
   Portal chrome (Nov 2026): `roleBadges?: ReactNode` — the portal's display-only role strip, drawn at the right of
   the top bar; when given, ML Studio's own bell and avatar are not drawn (the portal rail has them). `accountMenu`
   is still accepted for older portals. The app's logo stays at the top of its own sidebar.
   View as role (Nov 2026): `viewPermissions?: string[] | null` — the permission IDs of the role the user picked in
   the portal's role strip. `PermissionsProvider` shows from `me.permissions ∩ viewPermissions` (`null`/`undefined`
   = the full set); it never widens, switching re-renders without a remount, and a page no longer allowed redirects
   to the first allowed one. UX only — never sent to the backend; no role names are read.
4. `nginx.conf`: static files only (no `proxy_pass`). Envoy routes the API.
5. Send no identity/role headers from the browser.

Acceptance: `npm run build` produces `dist/assets/remoteEntry.js` exposing `./MLStudioApp`; a user without
`mlstudio:clusters:view` does not see Clusters, a user with it does; no role names in `src` (outside tests).


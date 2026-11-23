# InaiEra DataConnect — Frontend (Micro-Frontend)

The **DataConnect** UI, built as a **Module-Federation micro-frontend (MFE)**. It
is not a standalone app in production — the Control Portal federates it in over the
tunnel and mounts it inside its own shell. It exposes a single component,
`DataConnectApp`, which renders the full DataConnect experience (connections,
pipelines, schemas, transforms, quality, lineage, monitoring, templates,
settings).

- **Runtime:** React 18 + TypeScript, Vite 6, `@originjs/vite-plugin-federation`.
- **Exposed remote:** `dataconnect_mfe` → `./DataConnectApp` (emits `remoteEntry.js`).
- **Dev server / static host:** Express (`server.ts`) on port **5001**.

This is a separate repository from `InaiEra_DataConnect_Backend`, not a
monorepo subtree — see [`docs/REPOSITORY_MODEL.md`](docs/REPOSITORY_MODEL.md)
for why, and for how CI checks the two stay API-compatible without sharing a
version number.

---

## CI & tests

`.github/workflows/ci.yml` runs on every push/PR: `eslint`, `tsc --noEmit`,
the Playwright suite, `vite build`, then a Docker build smoke test.

```bash
npm run lint        # eslint .
npm run typecheck   # tsc --noEmit
npm test            # Playwright: tests/unit (Node) + tests/e2e (browser, mocked backend)
npm run test:unit   # just tests/unit
npm run test:e2e    # just tests/e2e
npm run test:live   # tests/live: the REAL stack, nothing mocked (see below; needs Docker Compose up)
```

`tests/e2e` drives the real Vite dev server with the backend replaced by
`page.route()` mocks (`tests/e2e/fixtures.ts`), so the suite needs no live
Postgres/Kafka stack. `tests/unit` exercises `src/api.ts` directly in Node,
no browser.

### Live end-to-end suite (`tests/live`, FE-04) — no mocks

The mocked suite cannot show that a connector workflow works against a real Kafka Connect, so `tests/live` drives this UI against the **real**
FastAPI backend, PostgreSQL, Kafka and Kafka Connect (the Docker Compose stack in `InaiEra_DataConnect_Backend/infra`). It runs only when
`DATACONNECT_LIVE_E2E=1` (`npm run test:live` sets it), so a plain `npm test` stays hermetic.

```bash
scripts/live-e2e.sh            # starts the stack if needed (docker compose up -d), waits for it, runs the suite
scripts/live-e2e.sh --no-up    # the stack is already up
DATACONNECT_LIVE_KEEP=1 scripts/live-e2e.sh   # leave the pipeline, topics and source database in place afterwards, as proof
```

One serial workflow, each step driven through the UI and then checked against the real system underneath (the Connect REST API, the topics, the source
database — never against the UI itself):

1. **Connection** — create a PostgreSQL connection to a throwaway source database, *Test connection* really connects.
2. **Discovery** — *Discover* lists the source's real table.
3. **Quality policy** — a min/max rule that routes failures to quarantine.
4. **Pipeline** — from the JDBC Source template, enforcement on.
5. **Deploy** — the readiness review, then Deploy: the connector is RUNNING in Kafka Connect and all six source rows are on the topic.
6. **Quarantine** — the two out-of-range rows are on the quarantine topic, the four good ones on the prepared topic; the Quality page shows 2/6 failed.
7. **Failure and recovery** — the source login is disabled for real, the task FAILS in Connect and the UI shows it; after the cause is fixed, *Restart*
   brings it back, the rows written meanwhile arrive once each, and enforcement carries on.
8. **Schema drift** — a real column is added to the source table, the connector emits it, the UI detects the drift from those real records, holds it for
   approval and approves it.

The suite owns the tenant `org_live_e2e` and a database `live_e2e_src`, purges both before it starts and removes them afterwards, and gives every run its
own connector version (Kafka Connect remembers a source connector's read position by name). Overrides: `DATACONNECT_COMPOSE_PROJECT` (default `infra`),
`DATACONNECT_API`, `DATACONNECT_CONNECT_URL`. It needs `docker` on the PATH. Two defects that only this suite could find are fixed: the *Infer schema*
drawer never sent the tenant header, and the backend's *Restart* left a FAILED task failed.

---

## How it's hosted

In production the Control Portal (`InaiEra_control_plane_frontend`) is the
Module-Federation **host**. It loads this remote from a **same-origin relative**
URL so the session cookie rides the request through the tunnel:

```
Control Portal (host)
  └─ federates  dataconnect_mfe/DataConnectApp
        via  /control_plane/api/dataplane/dataconnect/assets/remoteEntry.js
        (nginx → BFF → tunnel → Envoy → this MFE's static server :5001)
```

`vite.config.ts`:

```
federation({
  name: "dataconnect_mfe",
  filename: "remoteEntry.js",
  exposes: { "./DataConnectApp": "./src/DataConnectApp.tsx" },
  // Object form, not the bare-name array shorthand — see "Module Federation
  // contract tests" below for why requiredVersion matters.
  shared: {
    react: { requiredVersion: "^18.3.1" },
    "react-dom": { requiredVersion: "^18.3.1" },
  },
})
```

`DataConnectApp` accepts props from the host so it uses the tunnelled API base. **`apiBase` is required when it is mounted by a
host** (a production / federated build): without it it shows a configuration error and makes no requests, instead of guessing a
backend. The user, tenant and permissions are **not** props: they come from the backend's `GET /me` (the signed ticket, via Envoy).

```tsx
<DataConnectApp
  apiBase="/control_plane/api/dataplane/<dpId>/api/v1/dataconnect"  // same-origin, relative
  onOperation={…}
  roleBadges={<PortalRoleBadges app="dataconnect" />}
/>
```

| Prop | Type | Notes |
|---|---|---|
| `apiBase` | `string` | Same-origin tunnel base to the backend. Required when federated. |
| `embedded` | `boolean` | Hide DataConnect's own sidebar; its logo then opens the top bar instead. |
| `onOperation` | `(op) => void` | Write telemetry, for the host's activity bell. |
| `onError` | `(error) => void` | A screen threw while rendering (a contained fallback is shown either way). |
| `roleBadges` | `ReactNode` | The portal's display-only strip of DataConnect role names, drawn at the right of the top bar on one line (scrolls sideways on a narrow screen, never wraps). DataConnect never inspects it — access still comes from `GET /me`. **Passing it (even `null`) means "hosted by the portal"**: the portal's rail carries the notification bell and the profile, so DataConnect draws no avatar of its own. Leave it undefined when standalone. |
| `accountMenu` | `ReactNode` | *Deprecated* — older portals only. Rendered at the right of the top bar in place of DataConnect's avatar. A current portal passes `roleBadges` instead. |
| `viewPermissions` | `string[] \| null` | "View as role": the permission IDs of the one role the user picked in the portal's role strip. `null` / `undefined` = all their roles (no narrowing — what older portals send). Pages, tabs and buttons are then shown from `GET /me`'s permissions **∩** these — it only narrows, never adds a permission `/me` lacks. Switching it re-renders in place (no remount); an open page the role does not allow moves to the first one it does. UX only: never sent to the backend, which still authorises on the ticket. Applied once, in `AppContext`'s `can` (`effectivePermissions` in `src/permissions.ts`). |

DataConnect's own logo ("InaiEra · DataConnect") stays the first thing at the top-left of its chrome — the top of its sidebar, or
the left end of the top bar when `embedded` — so it sits right beside the portal rail's logo. Its in-app alerts (toasts, the
Monitoring page) are unchanged in every mode.

### Cross-origin loading (`server.ts`'s CORS policy)

The production path above is same-origin (the browser never sees this as a
cross-origin request, so no CORS header is needed at all). Some deployments
aren't same-origin though — a locally-run host loading this remote from a
different port during development, for instance — and `server.ts` used to
handle that by sending `Access-Control-Allow-Origin: *` unconditionally,
justified only by "this port is only reachable internally." That's a
network-placement assumption, not an access control a CORS header can
actually enforce: it silently stops holding the moment a port gets published,
a proxy is misconfigured, or the service is deployed somewhere more exposed
(as it is by default in `InaiEra_DataConnect_Backend/infra/docker-compose.yml`,
which publishes this container's port to the host for local dev).

`server.ts` now reads an explicit allow-list instead:

```bash
DATACONNECT_CORS_ORIGINS="https://portal.example.com,http://localhost:3000" npx tsx server.ts
```

- **Unset** (the default): no `Access-Control-Allow-Origin` header is sent at
  all — the host must load this remote same-origin.
- **A comma-separated list of origins**: only a request whose `Origin` header
  exactly matches one of them gets that origin reflected back (plus
  `Vary: Origin`, so a cache in front of this server can't leak one origin's
  CORS-approved response to another).
- **`"*"`**: every origin is allowed, logged loudly at startup as a reminder
  that it was chosen deliberately. Since this server only ever serves public,
  non-secret JS bundles (never the DataConnect API itself — that's a
  separate, always-authenticated origin), a wildcard here isn't a data leak
  the way it would be for an API, but an explicit list is still preferred so
  it's not left "wide open by default."

Same `DATACONNECT_` prefix as the backend's own `cors_origins` setting
(`app/core/config.py`), but comma-separated rather than that side's
JSON-array string — see `server.ts`'s own comment for why.

### Module Federation contract tests

The Control Portal host doesn't live in this repo, so `tests/e2e/federation-mount.spec.ts`
can only verify this remote's own half of the contract in isolation (it
mounts standalone, a build emits a loadable `remoteEntry.js`).
`tests/e2e/federation-host-contract.spec.ts` goes further: it builds a
synthetic host page — real `import()` of the built `remoteEntry.js`,
`init(shareScope)`, `get('./DataConnectApp')`, the same three calls any
Module Federation host makes — and serves it from a plain static file
server (dev mode never emits `remoteEntry.js`, so the usual Vite dev server
other e2e specs use won't do). It covers:

- the remote entry loads and resolves `./DataConnectApp` by name;
- React is actually consumed through the host's shared scope (a call-counted
  fake `get()`), not the remote's own bundled copy — the real question
  behind "shared React runtime";
- the mounted component's CSS lands in the host document's `<head>`;
- host-supplied props (`apiBase`, `onOperation`, `embedded`, `roleBadges`, `accountMenu`, `viewPermissions`) take effect — hosted (`roleBadges` given) draws the role strip and no avatar, standalone keeps the avatar; `viewPermissions` narrows the pages to /me ∩ that list (never wider), and switching it updates them in place;
- two independent mounts on one host page don't leak navigation state into
  each other (nothing here depends on `window.location` or a singleton);
- an unavailable remote fails as a catchable rejection, not a page crash;
- an incompatible host-offered React version is rejected in favor of the remote's own
  bundled copy, checked across a version matrix (accepted: `18.3.1`, `18.3.9`, `18.99.0`;
  rejected: `18.2.0`, `17.0.2`, `19.0.0`, `16.14.0` — the range is `requiredVersion`
  in `vite.config.ts`);
- CSS isolation in both directions: the remote's styles don't restyle host elements that
  reuse its class names, and a host with sloppy global CSS (`button{…}`, `body{line-height}`,
  `div{letter-spacing}`) doesn't restyle the remote;
- the remote doesn't read or write the host's global state (a host store on `window`,
  redux-persist's `localStorage` key) or add store-like globals;
- navigating inside the remote leaves the host's URL, hash and history length alone;
- a screen that throws while rendering shows a contained message (not an empty box),
  reports through the optional `onError` prop, and recovers when the user navigates.

That last one caught a real gap: `shared: ["react", "react-dom"]` (the
bare-name array shorthand) declares no `requiredVersion`, so
`@originjs/vite-plugin-federation`'s runtime accepted *any* React version a
host's shared scope offered, unchecked — a broken or incompatible host build
would have been wired straight into this remote's component tree instead of
falling back to this remote's own bundled React. Fixed by declaring `shared`
in its object form with `requiredVersion` set from `package.json`, so a
version outside the accepted range now falls back to the local copy instead
of being trusted blindly (see the "an incompatible host-offered React
version…" test above, and the `getSharedFromRuntime`/`satisfy()` semver
check it exercises in the built `__federation_fn_import-*.js` chunk).

#### Known gap: not yet tested inside the real Control Portal

All of the above runs against a **synthetic host page** that makes the same three
federation calls a real host makes. Nothing in CI mounts this remote inside the actual
InaiEra Control Portal shell (its bundle and build aren't available to this repo), so
anything specific to the real host — its own shared-scope contents, its Redux store and
providers, its router, its global stylesheet, its error handling around the remote — is
only approximated here. Run a manual smoke test against a Control Portal build after any
change to `vite.config.ts`'s `federation()` block, the shared React range, `src/styles.css`'s
root rules or the props `DataConnectApp` accepts. To close the gap, point a CI job at a
Control Portal build (or a staging URL) and run these same scenarios against it.

A note on the React range: `requiredVersion` is `^18.3.1`, so a host on React 18.2.x is
*rejected* and this remote falls back to its own bundled React. Two React copies in one tree
break hooks, so such a host would see an "invalid hook call" rather than a clean failure.
Widen the range (e.g. `^18.2.0`) if 18.2 hosts must be supported.

### Who's authoritative for the backend URL

Three cases, and only one of them is ever runtime-editable:

- **Embedded** (host passes `apiBase` — required, see below): the host is authoritative, full stop.
  `saveCfg()` (what Settings' "Save & reconnect" calls) is a hard no-op here
  — not just skipped `localStorage` persistence — so nothing can override
  the host's config at the state layer, even transiently.
- **Standalone, dev build** (`npm run dev`, no `apiBase`): the only mode with defaults — the local
  backend (kept in `src/runtimeConfig.ts` and nowhere else). Settings can edit
  and persist a backend URL to `localStorage`, for iterating against
  a local backend without rebuilding. This is gated on Vite's
  `import.meta.env.DEV`, a build-time constant — true only for code actually
  served by the dev server.
- **Federated / production build** (`vite build` output — what `Dockerfile` ships and a host mounts): `apiBase` is
  **required** from the host. If it is missing or blank, DataConnect renders a **configuration error** (naming what is missing),
  builds nothing, makes no request, and reports it once through the host's `onError`. It never falls back to the development
  backend, and any `localStorage` value from an earlier dev session is never read. The
  resolution rule is `src/runtimeConfig.ts` (`resolveRuntimeConfig`) and the gate is `src/components/ConfigGate.tsx`. Settings shows
  a disabled, read-only view.

---

## Permissions (no roles in this app)

DataConnect shows pages and buttons from the permissions the backend returns at `GET /me`
(`{ user, userId, tenant, permissions }`, contract `docs/auth/01-CONTRACT.md`). Roles live in the Admin Portal; this app
contains no role name, no role prop and no role switcher. `useApp().can("dataconnect:pipelines:edit")` answers from `/me`
(nothing is allowed until it answers, or if it fails); each page needs its `dataconnect:<page>:view` permission, and a user
without `dataconnect:access:view` sees a "no access" screen. The backend enforces every permission (403); hiding is convenience.

The permission IDs are generated from the backend's `permissions.json` into `src/types/permissions.generated.ts`
(`npm run gen:access-contract`; `npm run check:access-contract` in CI). No request sends an identity, tenant or role header —
Envoy sets those from the ticket. Local development: run the backend with `DATACONNECT_DEV_PERMISSIONS=all` (see the backend's
`app/core/gateway.py`). The e2e suite drives the UI as test personas by mocking `/me` (`tests/e2e/fixtures.ts`).

---

## State management — **Redux Toolkit + RTK Query**

All server state (connections, pipelines, schemas, transforms, quality,
lineage, runtime/monitoring, templates, health) and shared client state
(backend config, toast notifications) live in a Redux store, built with
`@reduxjs/toolkit` and `react-redux`.

- `src/store/apiSlice.ts` — one RTK Query `createApi` slice (`dataConnectApi`)
  with an endpoint per backend call. Pages consume it through the generated
  hooks (`useGetPipelinesQuery`, `useCreateConnectionMutation`, …), so two
  pages that need the same data (e.g. Dashboard and Pipelines both reading the
  pipeline list) share one cached, deduped subscription instead of each
  fetching independently — and every mutation invalidates the right cache
  tags so the rest of the app updates without a manual reload. Auto-refresh
  (e.g. the Pipelines table) uses RTK Query's `pollingInterval` rather than a
  hand-rolled `setInterval`.
  **Cancellation:** every `queryFn` forwards RTK Query's own per-request
  `AbortSignal` into the matching `api.ts` client call, which passes it
  straight to `fetch()` — a stale search, an old poll tick, or a page you've
  navigated away from now actually cancels the in-flight HTTP request rather
  than just being ignored once it resolves. This only takes effect promptly
  because `keepUnusedDataFor: 0` is set on the slice: RTK Query's 60s default
  would keep an unsubscribed query (and its request) alive for a full minute
  after the last component watching it unmounted, which defeats the point —
  the tradeoff is that navigating back to a page refetches instead of
  showing briefly-cached data, which is the right side of that tradeoff here.
  **Retries:** `api.ts`'s `request()` retries a transient upstream failure
  (502/503/504, or the fetch itself throwing) up to twice with full-jitter
  exponential backoff — but only for idempotent methods (GET, PUT, DELETE).
  POST and PATCH get exactly one attempt: idempotency, not "is this a read",
  is what makes a retry safe, and neither a POST-created resource nor this
  app's arbitrary-partial-field PATCH updates carry that guarantee. A 4xx or
  a plain 500 is never retried regardless of method. See `api.ts`'s own
  comment for the exact budget (2 retries, 250ms base / 2s cap) and how a
  backoff sleep still honors the same cancellation signal described above.
  **Polling:** `src/hooks/usePolling.ts` is the one place any page's
  auto-refresh interval gets computed (Pipelines.tsx's is the only current
  consumer) — paused entirely while the tab is hidden
  (`document.visibilitychange`, not a focus/blur guess), backed off with
  jitter while the polled queries are failing and reset the moment they
  recover, with an immediate refetch the instant the tab regains visibility
  rather than waiting out the rest of a paused interval. It only ever
  computes the plain number RTK Query's own `pollingInterval` option takes,
  so "two components polling the same query share one cache entry and one
  poll loop" is inherited from RTK Query for free, not reimplemented.
  **Errors:** `ApiError` (`src/api.ts`) mirrors the backend's standard error
  envelope (`app/schemas/errors.py`'s `ErrorEnvelope`) — every RTK Query
  hook's `error` (typed as `QueryError`, `src/store/apiSlice.ts`) carries not
  just `status`/`message` but `code` (a stable, machine-readable identifier —
  `"connector_config_error"`, `"kafka_connect_error"`, `"backend_unavailable"`,
  ... — see the backend's `app/core/errors.py` for the fixed set),
  `fieldErrors` (which specific fields were wrong, for a form to point at
  directly instead of parsing a flattened string), `requestId` (for
  reporting an issue traceably), `retryable` (the backend's own judgment on
  whether trying again might help, not re-derived from the status code
  here), and `details` (UI-safe structured context). A response that
  predates the envelope, or isn't from this backend at all, falls back to
  the old bare-string `detail` shape (or `statusText`) rather than losing
  the message — see `isErrorEnvelope()` in `api.ts`.
- `src/store/configSlice.ts` / `src/store/notificationsSlice.ts` — small
  `createSlice` reducers for the backend URL/org id and the toast stack,
  the two pieces of state genuinely shared across the whole app.
- `src/store/store.ts` — one store per mounted `<DataConnectApp>` (it's a
  federated micro-frontend, so a module-level singleton would leak state
  across remounts/hosts). Config and org id aren't build-time constants —
  they're host props that can change at runtime — so `queryFn` reads the
  live API client through the redux-thunk `extra` argument rather than a
  fixed `fetchBaseQuery` URL; changing the config rebuilds that client and
  calls `dataConnectApi.util.resetApiState()` so every active query refetches
  under the new tenant/backend.
- `src/context/AppContext.tsx` — still exposes a `useApp()` hook for
  backward-compatible access to config/toasts/health from components, but
  it's now a thin facade over the Redux store (`useSelector`/`useDispatch`),
  not the source of truth.

The one thing that deliberately stays **out** of Redux is the drawer modal's
open node — it holds a live React element with closures, and non-serializable
values don't belong in a Redux store. It's ephemeral, single-consumer UI
state, not shared async data, so it's still local Context (`ModalProvider` in
`AppContext.tsx`).

---

## Type safety — strict mode, project-wide

`tsconfig.json` (the config Vite and your editor use) has `strict: true`,
`noImplicitAny: true`, `strictNullChecks: true`, and `noUncheckedIndexedAccess:
true` — for the whole `src/` tree, pages included, not just the API layer.
`npm run typecheck` (plain `tsc --noEmit`) is the single gate; there's no
separate scoped config to keep in sync.

Genuinely dynamic backend JSON with no formal schema — transform SMT configs
(`ConnectorTemplate.supported_transforms`), quality rules (`QualityPolicy.rules`),
schema field lists (`SchemaVersion.schema_definition`), Kafka Connect's raw
passthrough objects (`ConnectorStatus.connector`, `PipelineStatus.runtime`) —
is typed `any` explicitly at each such spot in the pages that touch it, with a
comment saying why. That's a deliberate, visible escape hatch at real dynamic
boundaries, not a blanket project-wide one.

- **Types come from the backend, not by hand.** `npm run gen:api-types` pulls
  `InaiEra_DataConnect_Backend`'s live OpenAPI schema (default
  `http://localhost:8001/openapi.json`, override with `DATACONNECT_OPENAPI_URL`)
  through `openapi-typescript` into `src/types/dataconnect-api.generated.ts`
  (committed, regenerate after a backend schema change — don't hand-edit it).
  `src/types/api.ts` re-exports friendly names off of it (`Pipeline`,
  `ConnectionCreate`, `QualityPolicy`, …) plus the handful of shapes the
  backend deliberately leaves as passthrough JSON (`RuntimeFlow`,
  `TransformPreviewResult`, …), so a schema rename is a one-file fix instead
  of a search-and-replace.
- **`src/api.ts`** — every method's request/response is typed against
  `src/types/api.ts`. This is what caught a real, live bug during the initial
  typing pass: the UI's quality-policy "Execute" action called a
  `/quality-policies/{id}/execute` route that doesn't exist on the backend
  (confirmed 404 against a running instance) — it's now pointed at
  `/quality-policies/{id}/preview`, the synchronous endpoint whose response
  shape (`QualityPreviewResult`) is exactly what the UI already expected.
  `pipelines.update` similarly moved from `PUT` (never actually routed) to
  the real `PATCH`.
- **`src/store/apiSlice.ts`** — every RTK Query endpoint's arg/result type
  comes from the same `src/types/api.ts`, no `any`.
- **Pages** (`src/pages/**`) are strict too. Most of the work here was adding
  missing parameter/prop types, guarding against the `| undefined` that
  `noUncheckedIndexedAccess` and RTK Query's `data?: T` surface (e.g. a
  fetched list defaults to `[] ` before use instead of being accessed
  directly), and casting `as keyof typeof X` at lookups into the fixed-key
  badge/color maps in `constants.ts`. The genuinely dynamic JSON spots (listed
  above) keep an explicit, commented `any` rather than being force-fit into a
  shape that doesn't really exist on the backend.

---

## Directory structure

```
src/
  DataConnectApp.tsx     the exposed remote component (wraps AppProvider + router)
  main.tsx               standalone dev entry
  context/AppContext.tsx useApp() facade over the Redux store + the modal-only Context
  api.ts                 typed fetch client (uses apiBase from the host; same-origin)
  types/
    dataconnect-api.generated.ts  generated from the backend's OpenAPI schema — regenerate, don't edit
    api.ts                        friendly type aliases + hand-modeled passthrough shapes
  store/
    apiSlice.ts           RTK Query endpoints (server state) — wraps api.ts via queryFn
    configSlice.ts        backend URL / org id
    notificationsSlice.ts toast stack
    store.ts              per-instance store factory + live-client wiring
    hooks.ts               typed useAppDispatch / useAppSelector
  constants.ts  styles.css
  pages/                 Connections, Pipelines, Schemas, Transforms, Quality,
                         Lineage, Monitoring, Templates, Settings, Dashboard
  components/            shared UI
vite.config.ts           Module-Federation REMOTE config
server.ts                Express static server (serves dist + remoteEntry.js on :5001)
Dockerfile               build → static server image
index.html
```

---

## Run locally

```bash
npm install
# standalone dev (its own shell):
npm run dev
# build the federated remote:
npm run build      # → dist/ incl. assets/remoteEntry.js
# serve the built remote the way production does:
npx tsx server.ts  # → http://localhost:5001  (…/assets/remoteEntry.js)
```

To develop against the real Control Portal host, point the host's
`VITE_DATACONNECT_REMOTE` at this server's `remoteEntry.js`.

---

## Build & deploy (data plane)

Built as a two-stage container (`Dockerfile`): the build stage runs `npm ci`
(full deps, from `package-lock.json`), builds the federated remote with Vite,
and compiles `server.ts` → `server.js` (`npm run build:server`, plain JS, no
TypeScript needed to run it). The runtime image installs only
`runtime/package.json`'s dependency (`express`, pinned to the exact version
`package-lock.json` resolves, via its own `runtime/package-lock.json`) and
runs `node server.js` — no Node dev tooling, no `tsx`/`typescript`, no
build-only packages (React, Vite, ...) that the static server never uses. In
the reference deployment it's a data-plane service; Envoy routes
`/dataconnect/*` to it, and the Control Portal federates in
`./assets/remoteEntry.js` through the tunnel.

```bash
docker build -t dataconnect-mfe .
```

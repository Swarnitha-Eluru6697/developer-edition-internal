# Gateway-Auth Adoption and the Data-Plane Envoy Configuration

| | |
|---|---|
| **Version** | 1.0 — 2026-10-04 |
| **Covers** | (1) adopting gateway-auth in every data-plane app, (2) the corrected Envoy config, (3) the complete app route table and the recipe for adding more apps |
| **Companion** | `keycloak_cp/docs/DEPLOYMENT-KUBERNETES.md` — the full two-plane deployment runbook. This document is the Gate 1 / Gate 2 detail that runbook's §13.1 and §16 point at. |
| **Status** | The Envoy config in `envoy/envoy.dataplane.yaml` parses and its route and filter ordering are checked. **It has not been run against a live Envoy.** The ML Studio changes in §4.4 are specified, not applied. |
| **Updated** | 2026-10-07 — **all three items are implemented.** The Node package is renamed and publishes from CI (§4.3); ML Studio adopted the library (§4.4) and its tests pass; `/mlstudio-api/` is enabled in `envoy/envoy.dataplane.yaml`. |

---

## 1. What this document is for

Three things, in dependency order:

| # | Item | Section | Blocks |
|---|---|---|---|
| 1 | Every data-plane app adopts `gateway-auth` | §4 | ML Studio going behind Envoy at all |
| 2 | Envoy runs the corrected config — strip, claim mapping, secret injection | §5 | Gate 1 being real rather than decorative |
| 3 | Route table covers every app, and adding app #4 is cheap | §3, §6 | Catalog and ML Studio reaching the Control Portal |

They are not independent. Item 2 without item 1 means ML Studio sits behind a gate it does not check. Item 1 without item 2 means every app rejects every request, because nothing injects the secret they verify.

---

## 2. Scope corrections

Two source trees are **out of scope** and must not be treated as sources of truth.

### 2.1 `keycloak_cp/` is not a code source

It is the original proof-of-concept: `control-plane/user-management-service`, `org-validation-service`, `agent-comms-service`, `app-shell`, and `data-plane/authorization-gateway`, `product-mfe`, `countries-service`. Every one of those has been superseded by a real repository:

| POC component | Superseded by |
|---|---|
| `control-plane/user-management-service` | `InaiEra_control_plane_backend` |
| `control-plane/agent-comms-service` | `InaiEra_controlplane_Tunnelbroker_backend` |
| `control-plane/app-shell` | `InaiEra_control_plane_frontend` |
| `control-plane/org-validation-service` | folded into the control-plane backend |
| `data-plane/tunnel-agent` | `InaiEra_dataplane_TunnelAgent_backend` |
| `data-plane/envoy/envoy.dataplane.yaml` | **`envoy/envoy.dataplane.yaml` in this repo** (§5) |
| `data-plane/authorization-gateway`, `product-mfe`, `countries-service` | not deployed — POC demos |

What keycloak_cp still usefully holds: the Keycloak realm JSON and `deploy/ec2/render-realms.sh`, and the deployment runbook under `docs/`. Those should move to a deployment repository when one exists. **Nothing under `keycloak_cp/control-plane/` or `keycloak_cp/data-plane/` is built or deployed.**

### 2.2 `~/Downloads/ml-studio-app-v2.37.1` is not the product

A separate mock application. It is not what ships, and its behaviour differs from the real ML Studio in ways that have already caused incorrect work when copied from — most notably its alias semantics, where it treats `challenger` as "deployed, canarying traffic". In the real system `challenger` is a **promotion candidate that has never served a request**. The product is `Pralai_AIMLStudio_backend` + `Pralai_AIMLStudio_frontend`. Do not consult the mock for behaviour.

---

## 3. The app registry

This is the organising idea. Everything about a data-plane app that Envoy, the Control Portal and gateway-auth need is six fields. Adding an app means adding a row and the artefacts it names — nothing else changes.

### 3.1 The schema

| Field | Meaning |
|---|---|
| `app` | short slug, used for both path prefixes |
| `web.service` / `web.port` | the Service serving the federated MFE bundle |
| `api.service` / `api.port` | the Service serving the backend |
| `federation.name` | the Module Federation remote name (`<app>_mfe`) |
| `federation.exposes` | the exposed component |
| `roles` | this app's role vocabulary, in realm-role terms (§4.4.1) |
| `api.timeout` | Envoy route timeout — long only where the app genuinely needs it |

### 3.2 The apps today

**Three applications.** *Catalog and UCS are the same application* — the Unified Catalog Service, with two deployed components (a static MFE bundle and a FastAPI backend). They are not two apps, and the route table should not read as though they are.

| App | Components | Web Service | API Service | Federation | API timeout |
|---|---|---|---|---|---|
| **UCS (Catalog)** | `ucs-web` + `ucs-catalog` | `ucs-web:80` | `ucs-catalog:8000` | `catalog_mfe` → `./CatalogApp` | 120s |
| **DataConnect** | `dataconnect-mfe` + `dataconnect-backend` | `dataconnect-mfe:5001` | `dataconnect-backend:8000` | `dataconnect_mfe` → `./DataConnectApp` | 120s |
| **ML Studio** | `mlstudio-web` + `mlstudio-backend` | `mlstudio-web:80` | `mlstudio-backend:4000` | `mlstudio_mfe` → `./MLStudioApp` | 1800s |

ML Studio's 1800s is not generosity — dataset uploads run to hundreds of megabytes and training triggers are slow. The other two do not need it and should not have it, because a long timeout on a hung upstream holds a connection open rather than failing fast.

### 3.3 The path convention

```
/<app>/        ->  that app's static MFE bundle     prefix_rewrite: "/"
/<app>-api/    ->  that app's backend API           prefix_rewrite: "/"
```

Both rewrite to `/`, so **each app's own path structure is preserved below its prefix**. The SPA calls `${apiBase}/api/v1/...` and the backend sees `/api/v1/...` exactly as it does standalone. No app changes its internal routing to sit behind Envoy.

Concretely:

| App | Static | API | Backend sees |
|---|---|---|---|
| UCS | `/catalog/` | `/catalog-api/` | `/api/v1/...`, `/health` |
| DataConnect | `/dataconnect/` | `/dataconnect-api/` | `/api/v1/dataconnect/...` |
| ML Studio | `/mlstudio/` | `/mlstudio-api/` | `/api/...` |

Route order matters: Envoy takes the first match, so `/catalog-api/` must be listed **before** `/catalog/` or the bare prefix swallows it. The config in `envoy/envoy.dataplane.yaml` is ordered correctly; preserve that when editing.

### 3.4 Two legacy aliases, and how to retire them

The Control Portal bakes its remote and API URLs into the bundle at build time (Vite inlines `VITE_*`), so changing a path means rebuilding the host. Two of today's paths predate the convention:

| Legacy path | Points at | Why it exists | Retire by |
|---|---|---|---|
| `/ucs/` | `ucs_catalog` | The host passes `apiBase=/control_plane/api/dataplane/ucs` | Rebuilding `InaiEra_control_plane_frontend` with `apiBase=.../catalog-api` |
| `/api/v1/dataconnect` | `dataconnect_backend` (no rewrite) | The backend serves this prefix natively and the MFE calls it directly | Rebuilding `Pralai_DataConnect_Frontend` to route through `apiUrl()` with `/dataconnect-api` |

Both are in the config today so nothing breaks during the migration. Delete each one in the same change that rebuilds its frontend — not before, and not long after. An alias left in place becomes a second supported path nobody remembers to test.

### 3.5 The three places an app's identity appears

A new app is only wired correctly when all three agree. A mismatch in any one produces a failure that points somewhere else:

| Where | What | Symptom if wrong |
|---|---|---|
| Envoy route table | `/<app>/` and `/<app>-api/` | 404 through the tunnel; the host's `import()` fails with a module-parse error |
| Control Portal `vite.config.ts` | remote URL + `shared: ['react','react-dom']` | Blank panel, or hooks break because two React copies loaded |
| The app's own code | `gateway-auth` middleware + role vocabulary | 401 "request did not come through the gateway", or everyone silently gets the least-privileged role |

---

## 4. Item 1 — Adopting gateway-auth

### 4.1 The contract

Envoy validates the ticket, then turns its claims into headers and stamps proof of passage:

| Header | From claim | Encoding |
|---|---|---|
| `x-user-name` | `preferred_username` | plain string |
| `x-user-roles` | `realm_access.roles` | **base64 of a JSON array** — Envoy's rendering of a multi-valued claim |
| `x-user-groups` | `groups` | base64 of a JSON array |
| `x-tenant-id` | `tenant` | plain string |
| `x-inaiera-gateway-secret` | Envoy (`append_action: OVERWRITE`) | plain string |

The base64 encoding is the detail that bites. `WyJyZWFkZXIiLCJhZG1pbiJd` decodes to `["reader","admin"]`. A hand-written role parser that splits on commas sees one unrecognised token and resolves to the least-privileged role — so a real Keycloak token produces *no access* while a hand-made curl with `X-User-Roles: admin` works. That asymmetry has wasted real time. `split_multi` / `splitMulti` in this package handles base64-JSON, raw JSON and a plain comma list, which is precisely why role parsing is not something each app should write.

### 4.2 Adoption status

| App | Package | Status |
|---|---|---|
| **UCS** | `inaiera-gateway-auth` (Python) | **Adopted.** `app/identity.py` → `principal_from_headers()` uses `verify_gateway_secret`, `extract_identity`, `split_multi`. Keeps its own privileged bearer path (`principal_from_bearer`) on top — correct; that path verifies a real Keycloak JWT for `/api/v1/admin`. |
| **DataConnect** | `inaiera-gateway-auth` (Python) | **Adopted.** `app/core/gateway.py` → `make_gateway_dependency` + a `gateway_gate` that enforces app-membership and read/write. Mounted once: `app.include_router(api_router, prefix=settings.api_prefix, dependencies=[Depends(gateway_gate)])`. **This is the reference implementation** — §4.4 mirrors it in Node. |
| **ML Studio** | `@aadhyaanalyticsinc/gateway-auth` (Node) | **Adopted 2026-10-07.** `src/middleware/gateway.ts` → `createGatewayMiddleware` + a `gatewayGate` enforcing app-membership and read/write, mounted as `app.use("/api", gatewayGate)` with `/api/health` registered before it. `roleMiddleware` and its `X-User-Role` header are deleted. 15 tests in `tests/gateway.test.ts`; 65 pass overall. |

### 4.3 Node distribution — done: GitHub Packages

`pip` supports `#subdirectory=` on a git dependency; **npm does not**. So the Python package installs in one line from a pinned tag, and the Node package had no equivalent. **Resolved 2026-10-06: publish `@inaiera/gateway-auth` to the GitHub Packages npm registry** — the same "packages in Git" decision that put container images in GHCR rather than ECR/ACR.

**Renamed, because GitHub Packages requires a package's scope to match the account that owns it:**

```
@inaiera/gateway-auth   ->   @aadhyaanalyticsinc/gateway-auth
```

`node/package.json` carries the new name, `publishConfig`, `files: ["dist"]`, a `repository` block and a `prepublishOnly` that rebuilds and retests. Publishing runs from `.github/workflows/publish-node.yml` on a `node-v*` tag, using the token GitHub issues to the run — no stored credential. The workflow refuses to publish when the tag and `package.json` version disagree.

**Publishing:**

```jsonc
// node/package.json
{
  "name": "@aadhyaanalyticsinc/gateway-auth",
  "version": "1.0.0",
  "publishConfig": { "registry": "https://npm.pkg.github.com" },
  "repository": { "type": "git", "url": "git+https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git" }
}
```

```bash
cd node
npm run build && npm test
echo "//npm.pkg.github.com/:_authToken=$GH_WRITE_TOKEN" >> ~/.npmrc
npm publish
```

**Consuming**, in ML Studio and any other Node service:

```
# .npmrc — committed; it carries no secret
@aadhyaanalyticsinc:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${NPM_TOKEN}
```

```jsonc
// package.json
"dependencies": { "@aadhyaanalyticsinc/gateway-auth": "1.0.0" }
```

The image build then needs the token the same way the Python builds already need theirs — as a BuildKit secret, consumed and discarded in one layer:

```dockerfile
RUN --mount=type=secret,id=npm_token \
    NPM_TOKEN=$(cat /run/secrets/npm_token) npm ci \
 && rm -f ~/.npmrc
```

```bash
docker build --secret id=npm_token,env=NPM_TOKEN ...
```

Pin an exact version, not a range. The whole reason for leaving vendoring behind is that a vendored copy records nothing about which upstream commit it came from; a caret range reintroduces the same ambiguity at install time.

**Why not the two alternatives:** a separate repository means two repos kept in lockstep on a contract that is supposed to be identical across languages. Vendoring was already tried and already failed — vendored directories went uncommitted, so a clean clone broke the build, and nothing recorded the source commit.

**Python stays on the pinned git tag.** GitHub Packages has no PyPI-style registry, and the existing `inaiera-gateway-auth @ git+…#subdirectory=python` line works. The two languages therefore use different mechanisms for the same library — which is fine, and worth stating so nobody "fixes" the Python side to match.

### 4.4 ML Studio adoption — implemented

Done on 2026-10-07. The change was contained because `roleMiddleware` was the **only** thing setting `req.role` — `requirePermission`, `UCS_GATED_ACTIONS`, `requireUcsModelWrite` and every route read it and needed no change. What actually shipped:

| File | Change |
|---|---|
| `src/middleware/gateway.ts` | **New.** Role vocabulary, `createGatewayMiddleware`, `gatewayGate`, and a `roleOf(req)` accessor |
| `src/middleware/role.ts` | `roleMiddleware` deleted; `Role`/`ROLE_TIER` now imported; every `LOCAL_ACTION_TIER` value shifted up one |
| `src/server.ts` | `/api/health` registered before `app.use("/api", gatewayGate)`; `/api/whoami` now returns the verified identity |
| `src/ucs/config.ts` | `ROLE_TO_UCS_GROUP` keyed by realm roles; the UCS group names are unchanged, so UCS bindings needed no edit |
| `src/routes/trainingRuns.ts` | The one direct `req.role` comparison updated |
| `package.json`, `.npmrc`, `Dockerfile`, `Dockerfile.cuda` | Dependency added; both images install it with an `npm_token` build secret |
| `tests/gateway.test.ts` | **New.** 15 cases |

One detail worth recording: the shared package declares `req.role` as `string`, because every service has a different vocabulary. ML Studio's own `declare global` block conflicted with it. Rather than scatter casts, `gateway.ts` exports a single `roleOf(req)` that narrows it once.

#### 4.4.1 First: the role vocabulary does not match the realm

This is the part that is real work rather than mechanical, and it is easy to miss.

ML Studio's internal roles are `ml-viewer`, `ml-engineer`, `gpu-quota-approver`, `ml-platform-admin`. The roles that actually arrive in `X-User-Roles` are the **Keycloak realm roles the control plane derives from Admin Portal permissions**:

| Admin Portal permission | Realm role |
|---|---|
| view | `reader` |
| DataConnect edit | `data-engineer` |
| MLOps edit | `ai-ml-engineer` |
| Catalog edit | `catalog-admin` |
| edit all | `admin` |

**None of ML Studio's four names appear in that list.** Wire the package up with the current vocabulary and `resolveRole` matches nothing, every user falls to the default, and the app is uniformly read-only for everybody — with no error anywhere.

So the vocabulary has to be restated in realm terms, the way DataConnect's already is:

```ts
// ML Studio's OWN vocabulary, expressed in realm-role names.
// "no-access" is never issued in a token — it is only the resolver's
// least-privileged default when none of OUR roles match (e.g. a user who
// only has data-engineer).
const ROLES = {
  allRoles:    ['no-access', 'reader', 'ai-ml-engineer', 'admin'] as const,
  tiers:       { 'no-access': 0, reader: 1, 'ai-ml-engineer': 2, admin: 3 },
  defaultRole: 'no-access' as const,
};
```

Two consequences that must be handled, not glossed over:

**(a) `LOCAL_ACTION_TIER` thresholds shift by one.** Today operator actions are tier `1` and the one admin action (`base-models:sync`) is tier `2`, against `ROLE_TIER = { 'ml-viewer': 0, 'ml-engineer': 1, 'gpu-quota-approver': 1, 'ml-platform-admin': 2 }`. Under the new scheme operator is `2` and admin is `3`. Every value in `LOCAL_ACTION_TIER` moves `1 → 2` and `2 → 3`. Getting this wrong is silent: thresholds that are too low grant operator actions to readers.

**(b) `gpu-quota-approver` has no realm equivalent.** It is tier 1 today, the same as `ml-engineer`, so folding it into `ai-ml-engineer` changes nothing about who can do what *right now*. But if GPU quota approval is meant to be a separable duty, it needs its own realm role, derived by the control plane from an Admin Portal permission. **This is an open decision — see §7.** Until it is made, fold it in and leave a comment at the site saying why.

**(c) The role name sent to UCS changes.** `ucsAllows()` calls `checkAuthz(role, \`ml-studio-${role}\`, action, object)`. The role string becomes `ai-ml-engineer` rather than `ml-engineer`, so the corresponding UCS RBAC bindings must exist under the new names or every UCS-gated action fails closed. Check this against UCS's `rbac_bindings` before deploying, not after.

#### 4.4.2 New file — `src/middleware/gateway.ts`

Mirrors DataConnect's `app/core/gateway.py` one-for-one.

```ts
/**
 * Gate 1 for ML Studio — the standardized north-south trust boundary, via
 * the shared @inaiera/gateway-auth package (same contract as UCS and
 * DataConnect).
 *
 * Envoy has already validated the user's ticket, stripped anything the client
 * sent, injected the identity headers from the token, and stamped the gateway
 * secret. This module:
 *   1. verifies the gateway secret (constant-time; fails closed if unset),
 *   2. extracts identity (X-User-Name / X-User-Groups / X-Tenant-Id),
 *   3. resolves ML Studio's OWN role from X-User-Roles (realm roles),
 *   4. enforces: no recognised role -> 403 (this app is not part of your role).
 *
 * It deliberately does NOT decide whether a specific action is permitted.
 * That stays in role.ts's requirePermission — local tiers for app actions,
 * UCS authz/check for anything with a real catalog object behind it.
 */
import { Request, Response, NextFunction } from "express";
import pkg from "@aadhyaanalyticsinc/gateway-auth";   // scope per §4.3
const { createGatewayMiddleware } = pkg;

export type Role = "no-access" | "reader" | "ai-ml-engineer" | "admin";
export const ALL_ROLES: readonly Role[] = ["no-access", "reader", "ai-ml-engineer", "admin"];
export const ROLE_TIER: Record<Role, number> = {
  "no-access": 0, reader: 1, "ai-ml-engineer": 2, admin: 3,
};

const gatewayMiddleware = createGatewayMiddleware<Role>({
  roles: { allRoles: ALL_ROLES, tiers: ROLE_TIER, defaultRole: "no-access" },
  // Matches UCS_TENANT_ID / the metalake, so a token without a tenant claim
  // still addresses the right catalog namespace.
  defaultTenantId: process.env.UCS_TENANT_ID || "default",
});

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const WRITE_ROLES = new Set<Role>(["ai-ml-engineer", "admin"]);

export function gatewayGate(req: Request, res: Response, next: NextFunction) {
  gatewayMiddleware(req, res, () => {
    const role = req.role as Role;
    if (role === "no-access") {
      return res.status(403).json({
        error: "app_not_in_role",
        message: "ML Studio is not part of your role. Ask your organization admin " +
                 "to grant a role that includes MLOps (e.g. AI/ML Engineer).",
        app: "ml-studio", role,
      });
    }
    if (WRITE_METHODS.has(req.method) && !WRITE_ROLES.has(role)) {
      return res.status(403).json({
        error: "read_only_role",
        message: `Your role (${role}) is read-only in ML Studio. Creating, editing or ` +
                 "deleting requires the AI/ML Engineer or Admin role.",
        app: "ml-studio", role,
      });
    }
    next();
  });
}
```

#### 4.4.3 `src/server.ts` — the mount, and the health-check trap

`createGatewayMiddleware` has **no `openPaths` option**. There is no path-skipping logic in it at all, and TypeScript rejects the option while plain JS ignores it silently. A blanket `app.use(gatewayGate)` therefore puts `/api/health` behind the gate, it 401s, and every Kubernetes readiness probe fails — which presents as a crash-looping deployment, not as an auth problem.

Today's order is already wrong for this: `app.use(roleMiddleware)` sits on line 30, **before** `/api/health` on line 32. Fix the order in the same change.

```diff
- import { roleMiddleware } from "./middleware/role";
+ import { gatewayGate } from "./middleware/gateway";

  const app = express();
  app.use(cors());
  app.use(express.json());
- app.use(roleMiddleware);

- app.get("/api/health", (_req, res) => res.json({ ok: true }));
+ // Probe routes BEFORE the gate. There is no open-path escape hatch in
+ // createGatewayMiddleware, so anything registered after it is gated.
+ app.get("/api/health", (_req, res) => res.json({ ok: true }));
+
+ app.use("/api", gatewayGate);
+
  app.get("/api/whoami", (req, res) => res.json({ role: req.role }));
```

`/api/whoami` stays gated and is the single best end-to-end check that Gate 1 and Gate 2 agree — it reports the role the app actually resolved from the verified token.

Also extend it to return the identity now that there is one:

```ts
app.get("/api/whoami", (req, res) => res.json({
  role: req.role,
  username: req.gatewayIdentity.username,
  tenantId: req.gatewayIdentity.tenantId,
  groups:   req.gatewayIdentity.groups,
}));
```

The other 13 `app.use("/api", xRouter)` mounts need no change — they sit after the gate and keep reading `req.role`.

#### 4.4.4 `src/middleware/role.ts` — what changes and what does not

| Keep unchanged | Change |
|---|---|
| `LOCAL_ACTION_TIER` keys | its **values**: `1 → 2`, `2 → 3` (§4.4.1a) |
| `UCS_GATED_ACTIONS` | — |
| `ucsAllows()`, `authzCache`, the 5 s TTL | — (but see §4.4.1c on the role string sent to UCS) |
| `requirePermission()` | import `Role` / `ROLE_TIER` from `gateway.ts` instead of defining them |
| `requireUcsModelWrite()` | — |

**Delete `roleMiddleware` entirely.** Do not keep it behind a flag. A dev-only header switcher that is one environment variable away from being live is the same vulnerability with an extra step. If standalone development needs a role switcher, make it a separate `server.dev.ts` entrypoint that is not built into the production image.

The fail-closed behaviour of `ucsAllows()` — UCS unreachable denies — is correct and must stay. It is the same principle as the gateway-secret check.

#### 4.4.5 Environment

```
TRUST_ENVOY_HEADERS=true
ENVOY_INTERNAL_SECRET=<the per-data-plane secret Envoy injects>
```

`ENVOY_INTERNAL_SECRET` must be byte-identical to Envoy's and to UCS's and DataConnect's. ML Studio additionally sets `UCS_GATEWAY_SECRET` to the same value for its **outbound** calls to UCS — those go pod-to-pod and never traverse Envoy, so ML Studio has to present the secret itself.

Empty does not mean disabled. `verifyGatewaySecret` returns false when either side is blank, so an unset secret rejects everything with a message that reads like a permission denial.

#### 4.4.6 Tests to add

The Python package's `tests/test_middleware.py` already uses ML Studio's role names as its example vocabulary; port the same cases to Node:

| Case | Expect |
|---|---|
| No gateway secret | 401 "did not come through the gateway" |
| Wrong gateway secret | 401, same message |
| Correct secret, no `X-User-Name` | 401 "missing identity headers" |
| `TRUST_ENVOY_HEADERS` unset | 401 |
| `X-User-Roles: ai-ml-engineer` | role `ai-ml-engineer` |
| `X-User-Roles: reader,admin` | role `admin` (highest tier wins) |
| `X-User-Roles: data-engineer` (another app's role) | role `no-access` → 403 `app_not_in_role` |
| **`X-User-Roles: WyJhZG1pbiJd`** (base64 `["admin"]`) | role `admin` — proves the Envoy encoding is handled |
| `GET` as `reader` | allowed |
| `POST` as `reader` | 403 `read_only_role` |
| `X-User-Role: admin` (legacy singular) | **ignored** — must not affect the resolved role |
| `/api/health` with no headers at all | 200 |

The last two are the regression tests for this whole change. Neither passes today.

### 4.5 Checklist for any app adopting gateway-auth

1. Add the dependency — pinned tag (Python) or GitHub Packages version (Node, after §4.3).
2. Declare **this app's** role vocabulary in realm-role names, with `defaultRole` at the **lowest** tier.
3. Write the ~15-line gate: no-recognised-role → 403, plus the app's read/write policy.
4. Mount it on the API router only — **never** `app.use()` / an app-wide dependency — and register probe routes before it.
5. Set `TRUST_ENVOY_HEADERS=true` and `ENVOY_INTERNAL_SECRET`.
6. Add the route pair to `envoy/envoy.dataplane.yaml` (§6).
7. Run the §5.5 ladder against the new service.

Steps 2 and 3 are the only per-app code. The secret check, identity extraction and base64 role parsing come from the package unchanged — which is the entire point of it existing.

---

## 5. Item 2 — The corrected Envoy configuration

The config is `envoy/envoy.dataplane.yaml` in this repository. It replaces `keycloak_cp/data-plane/envoy/envoy.dataplane.yaml`, which should be deleted.

### 5.1 What was wrong with the previous config

| Defect | Consequence |
|---|---|
| No strip of inbound `x-user-*` / `x-inaiera-gateway-secret` | Identity headers were whatever the caller sent |
| No `claim_to_headers` | Nothing set identity from the verified token |
| No gateway-secret injection | UCS and DataConnect reject **every** request: 401 "request did not come through the gateway" |
| `local_jwks` with keys pasted inline | Every Keycloak key rotation silently breaks the data plane |
| No `/catalog`, `/catalog-api`, `/mlstudio` routes | Three of the four micro-frontends unreachable |
| Catch-all `/` → a POC service | A typo'd path silently reaches the wrong service |
| Admin interface on `0.0.0.0:9901` | Unauthenticated config dump (including the secret) and `/quitquitquit` to any pod |

Taken together: Gate 1 validated a signature and did nothing else. Gate 2 could not function because its precondition was never met.

### 5.2 The filter-ordering trap

> Envoy applies `request_headers_to_remove` from the route configuration in the **router** filter, which runs **after** the HTTP filter chain. Stripping `x-user-*` there would delete the headers `jwt_authn` had just set from verified claims — removing the real identity while protecting nothing against spoofing.

The strip must run in a filter placed **before** `jwt_authn`. The config uses the `header_mutation` HTTP filter for this, which is what this repository's own README prescribes:

```yaml
http_filters:
  - name: envoy.filters.http.header_mutation     # 1. strip what the caller sent
  - name: envoy.filters.http.jwt_authn           # 2. verify, then set from claims
  - name: envoy.filters.http.router              # 3. inject the secret, route
```

`header_mutation` requires Envoy 1.25+. The deployment targets 1.31. On an older Envoy, a Lua filter in the same position does the same job:

```yaml
- name: envoy.filters.http.lua
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
    default_source_code:
      inline_string: |
        function envoy_on_request(handle)
          local h = handle:headers()
          for _, n in ipairs({"x-user-name","x-user-groups","x-user-roles",
                              "x-tenant-id","x-inaiera-gateway-secret","x-user-role"}) do
            h:remove(n)
          end
        end
```

`x-user-role` (singular) is in the strip list because ML Studio still reads it. That closes the **external** escalation path ahead of §4.4 landing. It does **not** close the in-cluster one — anything that can reach port 4000 directly still picks its own privileges, which is why the network policy and the code fix are both still required and why `/mlstudio-api/` stays commented out in the config.

### 5.3 Secret handling — why not `%ENV(...)%`

The Helm fragment `_gateway-auth.tpl.yaml` injects the secret with `value: "%ENV(ENVOY_INTERNAL_SECRET)%"`. Envoy's documented environment-variable header formatter is **`%ENVIRONMENT(VAR_NAME)%`**. If `%ENV(...)%` is not a valid formatter, Envoy either rejects the config or emits the literal string — and the second failure mode is the dangerous one: every request then carries a wrong secret and every app 401s, which looks like a secret mismatch rather than a config bug. `UNVERIFIED` against a live Envoy; worth confirming before anyone relies on the fragment.

`envoy.dataplane.yaml` sidesteps the question by substituting at render time:

```bash
envsubst < envoy.dataplane.yaml > /etc/envoy/envoy.yaml
```

Run it in an initContainer that reads `ENVOY_INTERNAL_SECRET` from the mounted Key Vault secret and writes the rendered file to an `emptyDir`. The template stays in a ConfigMap and in Git; the secret never does.

Variables: `CP_HOST`, `KEYCLOAK_REALM`, `ENVOY_INTERNAL_SECRET`.

### 5.4 `remote_jwks`, not `local_jwks`

The previous config mirrored the realm's signing keys inline, which removes a runtime dependency at the cost of a silent outage on every key rotation. The data plane already dials the control plane outbound for the tunnel, so fetching JWKS over the same path adds no new network dependency and no new firewall rule. Keep `local_jwks` only as a documented break-glass fallback — and if a deployment genuinely cannot reach the control plane for JWKS, the mirroring must be a monitored scheduled job, not a step in a runbook.

### 5.5 Verifying the config

```bash
# 1. The config parses and the listener comes up
envoy --mode validate -c /etc/envoy/envoy.yaml

# 2. Filter order is strip -> jwt -> router
kubectl exec -n inaiera-dp deploy/envoy -- \
  curl -s localhost:9901/config_dump | jq -r '
    .configs[] | select(.["@type"]|test("ListenersConfigDump")) |
    .dynamic_listeners,.static_listeners' | grep -o '"name": "envoy.filters.http[^"]*"'

# 3. Every cluster is healthy
kubectl exec -n inaiera-dp deploy/envoy -- curl -s localhost:9901/clusters | grep health_flags

# 4. JWT counters distinguish "ticket rejected" from "upstream down"
kubectl exec -n inaiera-dp deploy/envoy -- curl -s localhost:9901/stats | grep jwt_authn
```

Then the behavioural checks:

```bash
# Bad ticket -> 401 at Gate 1
curl -si https://$CP_HOST/tunnel_broker/api/dataplane/$DATA_PLANE_ID/catalog-api/health \
     -H "X-Data-Plane-Ticket: not-a-jwt" | head -1          # 401

# Good ticket -> 200, and identity comes from the TOKEN, not the forged headers
curl -s https://$CP_HOST/tunnel_broker/api/dataplane/$DATA_PLANE_ID/catalog-api/health \
     -H "X-Data-Plane-Ticket: $TOKEN" \
     -H "X-User-Name: attacker" -H "X-User-Roles: admin" \
     -H "X-Inaiera-Gateway-Secret: guessed"
```

The second must reflect the real user. If `attacker` gets through, the strip filter is missing or is ordered after `jwt_authn`.

And the ladder against an app directly, from inside the cluster — each rung names its own cause:

```
curl -i http://ucs-catalog:8000/api/v1/requests
  401 "request did not come through the gateway"   -> no secret sent

curl -i ... -H "X-Inaiera-Gateway-Secret: wrong"
  401 same message                                  -> wrong secret

curl -i ... -H "X-Inaiera-Gateway-Secret: $S"
  401 "missing identity headers"                    -> secret OK, no user

curl -i ... -H "X-Inaiera-Gateway-Secret: $S" \
           -H 'X-User-Name: alice' -H 'X-User-Roles: admin' -H 'X-Tenant-Id: prod_metalake'
  200                                               -> working
```

> A plain curl with no headers getting a 401 is the fail-closed path working, not a broken stack. This has been misdiagnosed more than once — do not conclude anything from an unauthenticated probe.

---

## 6. Item 3 — Adding an app

Everything above is arranged so this is a checklist rather than a design exercise. For a new app `foo`:

**1. Envoy** — add one route pair and one cluster pair to `envoy/envoy.dataplane.yaml`. The `-api` route goes **first**:

```yaml
- match: { prefix: "/foo-api/" }
  route: { cluster: foo_backend, prefix_rewrite: "/", timeout: 120s }
- match: { prefix: "/foo/" }
  route: { cluster: foo_web, prefix_rewrite: "/", timeout: 15s }
```

```yaml
- name: foo_backend
  connect_timeout: 5s
  type: STRICT_DNS
  lb_policy: ROUND_ROBIN
  load_assignment:
    cluster_name: foo_backend
    endpoints: [{ lb_endpoints: [{ endpoint: { address: { socket_address: { address: foo-backend, port_value: 8000 } } } }] }]
- name: foo_web
  connect_timeout: 5s
  type: STRICT_DNS
  lb_policy: ROUND_ROBIN
  load_assignment:
    cluster_name: foo_web
    endpoints: [{ lb_endpoints: [{ endpoint: { address: { socket_address: { address: foo-web, port_value: 80 } } } }] }]
```

Nothing else in the Envoy config changes. The JWT provider, the claim mapping, the strip filter and the secret injection are all virtual-host-wide and already apply.

**2. The app** — §4.5's checklist.

**3. The MFE remote** — `@originjs/vite-plugin-federation`, `name: "foo_mfe"`, `filename: "remoteEntry.js"`, `exposes: { "./FooApp": "./src/FooApp.tsx" }`, `shared: ["react","react-dom"]`, `build: { target: "esnext", cssCodeSplit: false }`. **React 18.x**, matching every other repo, or the shared singleton splits and hooks break.

The exposed component takes `apiBase` and `embedded` from the host, uses an internal **MemoryRouter** (a second BrowserRouter fights the host for the address bar), and has a `height: 100%` root — `100vh` inside the host's panel doubles the chrome and clips content.

**4. The host** — add the remote to `InaiEra_control_plane_frontend/vite.config.ts` with a **relative** URL so the session cookie rides it:

```ts
foo_mfe: '/control_plane/api/dataplane/foo/assets/remoteEntry.js'
```

Add an ambient `declare module` in `src/remotes.d.ts` (the module has no local file, so TypeScript needs it) and a page that `lazy()`-imports it behind an error boundary.

**5. The static server** — serve `dist/` and nothing else. `remoteEntry.js` gets `Cache-Control: no-cache` and a `try_files $uri =404` so a miss is an honest 404; hashed `/assets/` get `immutable`. A missing chunk served as `index.html` with a 200 reaches the host as HTML-parsed-as-JavaScript and surfaces as an opaque syntax error pointing nowhere near the bad deploy. `InaiEra-unified-catalog-frontend/nginx.dataplane.conf` is the template to copy.

**6. Network policy** — allow Envoy → `foo-web` and Envoy → `foo-backend`, and nothing else inbound. Gate 2's secret check only means something if Envoy is the sole path in.

**7. Roles** — if the app needs a permission that no existing realm role covers, that is a control-plane change: an Admin Portal permission, a derivation rule in `platformRoles`, and a realm role. Do not invent an app-local role name that no token will ever carry.

**The two things that silently break a new remote**, both worth re-checking before calling it done:

1. **A hardcoded `/api` in the remote's fetch client.** Embedded, that hits the *host's* origin, not the data plane. Every call must go through an `apiUrl()` that prefixes the host-supplied base.
2. **A `100vh` root.** Inside the host's panel it doubles the chrome and clips content.

---

## 7. Open decisions

| # | Decision | Needed by | Default if nobody decides |
|---|---|---|---|
| 1 | ~~Node package distribution~~ — **done.** Renamed to `@aadhyaanalyticsinc/gateway-auth`, publishes from CI on a `node-v*` tag | — | — |
| 2 | ~~`gpu-quota-approver`~~ — **done.** Folded into `ai-ml-engineer` (it sat at the same tier); GPU quota approval is now an `admin` action, with a comment at the site | — | — |
| 3 | ~~UCS bindings under the new role names~~ — **not needed.** `ROLE_TO_UCS_GROUP` translates ML Studio's roles into the existing UCS group names, which did not change | — | — |
| 4 | When do the two legacy path aliases get retired? (§3.4) | With each frontend rebuild | They persist as untested second paths |
| 5 | Is `%ENV(...)%` valid in the Helm fragment? (§5.3) | Before anyone renders from the fragment | `envoy.dataplane.yaml` does not depend on it |
| 6 | Where do the deployment docs live once `keycloak_cp` is retired? (§2.1) | Before the infra handoff | They stay in a repository nobody builds from |

---

## Appendix — Role vocabulary reference

**Realm roles** (what actually arrives in `X-User-Roles`), derived by the control plane from Admin Portal permissions:

| Realm role | Granted by | UCS | DataConnect | ML Studio |
|---|---|---|---|---|
| `reader` | view | read | read (`reader`) | read (`reader`) |
| `data-engineer` | DataConnect edit | read | **read + write** | no-access → 403 |
| `ai-ml-engineer` | MLOps edit | read | no-access → 403 | **read + write** |
| `catalog-admin` | Catalog edit | **admin** | no-access → 403 | no-access → 403 |
| `admin` | edit all | **admin** | **read + write** | **read + write** |

`no-access` is never issued in a token. It exists only as each app's least-privileged resolver default, and resolving to it is what produces the `app_not_in_role` 403 — the honest answer to "this product is not part of your role", rather than a silent empty screen.

Each app keeps its own `RoleConfig`: a user legitimately carries roles for products they are not currently using, and `resolveRole` ignores the ones this app does not recognise. That is correct behaviour, not an error — which is why `defaultRole` must always be the lowest tier and never the highest.

**UCS additionally** treats `ROLE_PLATFORM_ADMIN` as a comma-separated list, so both `admin` and `catalog-admin` resolve as UCS administrators, and keeps a separate privileged bearer path (`principal_from_bearer`) for `/api/v1/admin` that verifies a real Keycloak JWT rather than trusting headers. That second path is correct and should not be replaced by gateway-auth — it answers a different question.

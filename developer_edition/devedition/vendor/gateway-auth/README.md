# InaiEra Data-Plane Gateway Auth

The **standardized north-south trust boundary** for every data-plane service. It
is a shared **library** (Node + Python) plus an **Envoy config fragment**, not a
running service. Each app behind Envoy imports it to:

1. **refuse every request that did not come through Envoy** (gateway secret,
   deny by default, a short explicit list of public paths),
2. **read the caller's identity and permissions** from the headers Envoy sets
   from the signed Data Plane ticket, and
3. **authorise by permission** (`<app>:<area>:<action>`), never by role name.

Roles exist only in the Admin Portal. They reach the data plane as permissions in
the ticket, so a new role, or a change to what a role may do, needs **no code
change and no redeploy** in any app. The full rules are in the platform contract
(`01-CONTRACT.md`); this README covers what the library does.

> **Plane:** data plane only. The **control plane never uses it**: the BFF *mints*
> the ticket; it never validates it.

> **v1.1:** `RoleConfig` / `resolveRole` / `createGatewayMiddleware` /
> `make_gateway_dependency` (role tiers) are **deprecated** and will be removed in
> the next release. They still work and now also carry `permissions`.

---

## What's in here

```
node/                 @aadhyaanalyticsinc/gateway-auth  (Express / any Node service)
  src/
    gatewaySecret.ts    verifyGatewaySecret: constant-time secret check
    parse.ts            splitMulti: list headers (base64 JSON / JSON / comma list)
    identity.ts         extractIdentity, hasPermission
    middleware.ts       gatewayAuth (deny by default), requirePermission, meBody
    resolveRole.ts      deprecated role tiers
    index.ts            public exports
  tests/                vitest

python/               inaiera-gateway-auth  (FastAPI / any Python service)
  gateway_auth/
    core.py             verify_gateway_secret, extract_identity, has_permission, split_multi
    fastapi.py          protect_app (deny by default), require_permission, get_principal
    __init__.py         public exports
  tests/                pytest

envoy/
  _gateway-auth.tpl.yaml  reusable route fragment (jwt_authn + claim_to_headers + secret injection)
  envoy.dataplane.yaml    the data-plane Envoy config (strip -> verify -> route)
```

---

## The headers (set by Envoy, and only by Envoy)

| Header | From claim | Format | Used for |
| --- | --- | --- | --- |
| `x-user-name` | `preferred_username` | string | display, audit |
| `x-user-id` | `sub` | string (immutable user id) | ownership, audit |
| `x-tenant-id` | `tenant` | string | tenant scoping |
| `x-user-groups` | `groups` | base64-encoded JSON array | optional |
| `x-user-permissions` | `permissions` | base64-encoded JSON array | **authorisation (the only input)** |
| `x-user-roles` | `realm_access.roles` | base64-encoded JSON array | display/audit only, **never authorise on it** |
| `x-inaiera-gateway-secret` | Envoy config (`append: false`) | shared secret | proof the request came through Envoy |

Envoy removes every one of these (and the legacy `x-user-role`) from the incoming
client request before setting its own. The library never reads `x-user-role`.

List headers accept base64-encoded JSON array (what Envoy sends), a raw JSON array,
or a comma list. Anything else is an empty list.

The rules every app gets from the library:

| Request | Answer |
| --- | --- |
| no / wrong gateway secret, or `TRUST_ENVOY_HEADERS` not `true` | **401** |
| right secret, no identity (`x-user-name`) | **401** |
| identity, but not the permission the route needs | **403** |
| identity with the permission | passes |
| a public path (health probe, static asset, self-verifying webhook) | passes, no checks |

---

## How a service uses it

### Python / FastAPI (DataConnect, UCS, Agent, Workspace)

```python
from fastapi import Depends, FastAPI
from gateway_auth import protect_app, require_permission, get_principal

app = FastAPI()
protect_app(app, public_paths=["/health", "/static/*"])        # every other route: 401 unless via Envoy

@app.get("/api/clusters", dependencies=[Depends(require_permission("mlstudio:clusters:view"))])
def clusters(): ...                                             # 403 without the permission

@app.get("/api/me")
def me(principal = Depends(get_principal)):
    return principal.me()          # {"user", "userId", "tenant", "permissions"}
```

`principal.has("x:y:edit")` checks one permission inside a handler.
Public-path entries are exact paths, or a prefix ending in `*`.

### Node / Express (ML Studio)

```ts
import { gatewayAuth, requirePermission, meBody } from "@aadhyaanalyticsinc/gateway-auth";

app.use(gatewayAuth({ publicPaths: ["/api/health"] }));        // before every router
app.get("/api/clusters", requirePermission("mlstudio:clusters:view"), handler);
app.get("/api/me", (req, res) => res.json(meBody(req.principal!)));
```

`hasPermission(req.principal, "x:y:edit")` checks one permission inside a handler.

### Two env vars every service sets

```
TRUST_ENVOY_HEADERS=true
ENVOY_INTERNAL_SECRET=<the per-data-plane secret Envoy injects>
```

---

## Envoy side

`envoy/_gateway-auth.tpl.yaml` standardizes, per app route, the `jwt_authn`
provider, the `claim_to_headers` mapping (the table above), and the
`request_headers_to_add` gateway-secret injection (`append:false`). Only the
upstream cluster name varies per app.

> **Deployment requirement: ingress header strip.** A `header_mutation` filter
> placed **before** `jwt_authn` must strip every client-supplied identity header,
> so only Envoy can set them (route-level `request_headers_to_remove` runs after
> the filter chain and would delete the verified values instead).
> `envoy/envoy.dataplane.yaml` does this:
> ```yaml
> - name: envoy.filters.http.header_mutation
>   typed_config:
>     "@type": type.googleapis.com/envoy.extensions.filters.http.header_mutation.v3.HeaderMutation
>     mutations:
>       request_mutations:
>         - remove: "x-user-name"
>         - remove: "x-user-id"
>         - remove: "x-user-permissions"
>         - remove: "x-user-groups"
>         - remove: "x-user-roles"
>         - remove: "x-tenant-id"
>         - remove: "x-inaiera-gateway-secret"
>         - remove: "x-user-role"
> ```

The Envoy fragment is **configuration** — infra applies it when rendering the
data-plane Envoy config; there is nothing to "deploy" from this repo for the Envoy
part.

---

## Build / test / distribute

```bash
# Node
cd node && npm install && npm run build && npm test

# Python
cd python && pip install -e . && pytest
```

**Distribution:** this is a library, consumed as a **pinned git dependency**. Do not
vendor it — see the note below.

Python — one line in the service's `requirements.txt` (or `pyproject.toml`
`dependencies`):

```
inaiera-gateway-auth @ git+https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git@v1.1.0#subdirectory=python
```

This repo is private, so the image build needs a read credential. Pass it as a
BuildKit secret and rewrite the URL inside a single `RUN`, so the token never
lands in a layer:

```dockerfile
RUN --mount=type=secret,id=gh_token \
    git config --global url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/" \
 && pip install --no-cache-dir -r requirements.txt \
 && rm -f /root/.gitconfig          # same layer — the token must not persist
```

Build with `docker build --secret id=gh_token,env=GH_TOKEN .`. The image needs
`git` installed at build time.

**Node:** npm does not support `#subdirectory=` on a git dependency, so the Node
package is published to the org's GitHub Packages npm registry as
`@aadhyaanalyticsinc/gateway-auth` (`.github/workflows/publish-node.yml`, run by
pushing a tag `node-v<version>` that matches `node/package.json`). Consumers add
an `.npmrc` with `@aadhyaanalyticsinc:registry=https://npm.pkg.github.com` and a
read token, passed to image builds as a BuildKit secret like the Python one.

> **Why not vendoring.** It was tried and it failed in both directions: UCS's
> `vendor/` was never committed, so `COPY vendor ./vendor` broke the build from a
> clean clone, while both services carried `build/`, `*.egg-info/` and `tests/`
> copies — 14 files on disk for a 3-file package — with nothing recording which
> upstream commit they came from. A pinned tag in one line records the version,
> installs the package only, and cannot drift silently.

---

## When does this code change?

Rarely, and never for routine growth:

- **New org** → no change (org-agnostic; a new data plane just sets its own secret/keys as config).
- **New app** → no change here (the app declares its own roles/policy in its own repo).
- **Changes only** when the platform-wide contract changes (a new standard
  header/claim, a change in how Envoy encodes a claim, or a security fix).

---

## Related repos

- **Tunnel Agent (data plane):** `InaiEra_dataplane_TunnelAgent_backend` — forwards to Envoy, which fronts services that use this package.
- **Consumers:** DataConnect backend and UCS — each declares this package as a pinned git dependency and adds one Envoy route. ML Studio has **not** adopted it yet: `backend/src/middleware/role.ts` still trusts a client-supplied `X-User-Role` header with no gateway-secret check.

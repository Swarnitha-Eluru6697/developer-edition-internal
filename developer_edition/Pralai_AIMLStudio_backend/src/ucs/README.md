# UCS integration — what's real, what's verified, what needs a live Gravitino

This integration is built against the actual uploaded UCS codebase
(`services/catalog`, 86 Python files: routers, services, schemas, DB
models, Alembic migrations) — not an assumed contract. See
`CHANGELOG.md`'s v2.12.0 entry for the full account; this file is the
short version for anyone touching this code later.

## How ML Studio authenticates to UCS (permissions, no roles, no bearer token)

Every call carries `X-Inaiera-Gateway-Secret` (`UCS_GATEWAY_SECRET`) and
`X-Tenant-Id` (`UCS_TENANT_ID`), plus a principal built by `ucs/config.ts`:

- **On behalf of a user** (`authz/check`, lineage graph/runs/impact, object
  profile, explorer tree): `X-User-Name`, `X-User-Id`, `X-User-Groups` of the
  gateway-verified caller. Their ML Studio permissions are not forwarded; UCS
  needs only a gateway principal for these.
- **Lineage producer** (`POST /lineage/events`): `X-User-Name` =
  `UCS_SERVICE_USERNAME`, `X-User-Permissions: ucs:lineage-events:create`.
- **Everything under `/api/v1/admin`** (register catalog/model/version, create
  schema, RBAC role, bind/revoke): `X-User-Name` = `UCS_SERVICE_USERNAME`,
  `X-User-Permissions: ucs:catalogs:manage,ucs:rbac:manage`.

No ML Studio role is mapped to a UCS group and no role marker is sent.

## What's live-verified (a real running instance of the uploaded code, in this environment)

- Full lineage round-trip: `POST /lineage/events` (real OpenLineage
  RunEvent) → `GET /lineage/{fqn}/graph|runs|impact` all correctly
  reflecting the same event.
- `authz/check` allow/deny semantics, including the writer/owner tier
  distinction (`CREATE_MODEL_VERSION` vs. `CREATE_MODEL` — a writer
  binding can version an existing model but not register a new one).
- The full ML Studio flow: create a training run (real UCS `write` check)
  → complete it (real UCS `write` check, model + version registered
  attempt, real lineage event emitted) → query it back through
  `GET /api/models/:id/governance`.

## What's spec-correct but NOT verified end-to-end

Model/catalog registration (`POST /admin/models`, `/admin/models/versions`,
`/admin/catalogs`) and RBAC role/group creation (`POST /admin/rbac/*`) all
depend on **Gravitino** — a separate Java service UCS calls out to over
HTTP. Gravitino can't run in this sandbox (no Docker, no JVM stack). This
was confirmed to be an infrastructure gap, not a code defect: UCS's own
error traceback for a real registration attempt showed
`httpx.ConnectError: All connection attempts failed` — the request format
was correct and reached UCS; only the downstream Gravitino call failed.

`scripts/bootstrapUcs.ts` (provisions the model catalog + one RBAC role
per ML Studio tier + group bindings) is written and type-checks, but is
in the same boat — needs a real Gravitino to actually run against.

## Running this for real

1. Point `UCS_BASE_URL` at a real UCS deployment with Gravitino reachable.
2. Set `UCS_GATEWAY_SECRET` to UCS's gateway secret and `UCS_SERVICE_USERNAME`
   to the service name UCS should see.
3. Run `npx ts-node src/scripts/bootstrapUcs.ts` once, to provision the
   catalog + roles + bindings `authz/check` needs to return anything but
   "no matching grant".
4. Everything else (training runs, promotions, Model Overview's Lineage
   tab) works from there with no further setup.

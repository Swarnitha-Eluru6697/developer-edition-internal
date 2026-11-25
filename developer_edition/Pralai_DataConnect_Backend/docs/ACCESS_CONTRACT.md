# Access contract (permissions)

Who may do what is **not** defined in DataConnect. Roles live in the Admin Portal and are turned into permissions in
the user's ticket; DataConnect checks permissions only (platform contract: [auth/01-CONTRACT.md](auth/01-CONTRACT.md)).
The permission IDs DataConnect understands are declared once, in [`permissions.json`](../permissions.json); the
mapping from the old capabilities, the route coverage and the seed delta are in [auth/PERMISSIONS.md](auth/PERMISSIONS.md).

The backend enforces a permission on every route (403), and `tests/test_route_permission_coverage.py` fails if a route
declares none. The frontend only decides what to show, hide or disable.

## `GET /api/v1/dataconnect/me`

Requires a gateway-verified principal (401 otherwise) but no particular permission.

```json
{ "user": "alice", "userId": "6b1f…", "tenant": "org_1001", "permissions": ["dataconnect:access:view", "dataconnect:pipelines:view"] }
```

- `permissions` — every permission the caller holds (sorted). A user with none gets `[]`, not an error.
- `tenant` — the tenant every request of this caller is scoped to (from `x-tenant-id`; `X-Org-Id` is ignored).

`GET /access/capabilities` (the old role/capability matrix) no longer exists.

## How the frontend uses it

`Pralai_DataConnect_Frontend` loads `/me` once per mount and gates pages/buttons with `can("dataconnect:…")`. Its typed
list of permission IDs, `src/types/permissions.generated.ts`, is **generated** from this repo's `permissions.json` by
`npm run gen:access-contract`; `npm run check:access-contract` fails when the committed file differs.

## How drift is caught

| Where | Check | Fails when |
| --- | --- | --- |
| Backend `tests/test_route_permission_coverage.py` | every route requires exactly one declared permission; every declared permission is enforced or UI-only | a route is added without a check; a permission is declared but never used |
| Backend `tests/test_gateway_contract.py` | contract §6 A–G on `GET /connections`, `/me` body, tenant from the principal | the gate or `/me` regress |
| Backend `tests/test_rbac.py` | the seeded role personas (`tests/personas.py`) keep their behaviour; each GET route 403s without its own permission | a route checks the wrong permission |
| Backend `app/core/permissions.py` | `require()` refuses an undeclared ID at import | a typo in a route |
| Backend CI `contract-check` job | runs the frontend's `npm run check:access-contract` against this repo's `permissions.json` | the frontend's generated list differs |
| Frontend `tests/unit/access-contract.spec.ts` | every permission the UI checks is declared; no role name under `src/` | the UI uses an undeclared ID or a role name |

```bash
# frontend, with this repo checked out next to it
DATACONNECT_PERMISSIONS_FILE=../Pralai_DataConnect_Backend/permissions.json npm run gen:access-contract
DATACONNECT_PERMISSIONS_FILE=../Pralai_DataConnect_Backend/permissions.json npm run check:access-contract
```

## UI-only permissions

`dataconnect:access:view` (open the app), `dataconnect:dashboard:view` and `dataconnect:settings:view` gate client pages
only; no server route enforces them. Everything a page shows still comes from routes that check their own permission.

## Adding a permission

1. Add it to `permissions.json` (contract §7) and enforce it with `Depends(require("dataconnect:<area>:<action>"))`.
2. Frontend: regenerate (`npm run gen:access-contract`), use it as `can("dataconnect:<area>:<action>")`.
3. Admin Portal: grant it to the roles that should have it — no DataConnect redeploy is needed for role changes.

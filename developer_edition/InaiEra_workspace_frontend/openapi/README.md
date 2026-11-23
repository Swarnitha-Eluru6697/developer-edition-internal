# Versioned backend OpenAPI snapshots

`workspace.v<version>.json` is a checked-in snapshot of the Workspace
backend's OpenAPI document, named after the backend's own `info.version`
(currently `0.1.0`). It is the single source of truth `npm run codegen:api`
generates `src/api/generated/workspace-schema.ts` from (WS-FE-014) — codegen
runs against this committed file, not a live server, so `npm run build` is
reproducible without the backend running, and the generated types only change
when someone deliberately re-pins a new backend contract.

The snapshot is byte-identical to the backend repo's own committed contract,
`InaiEra_workspace_backend/openapi/workspace.json`, which that repo's
`scripts/export_openapi.py` writes deterministically (sorted keys, 2-space
indent) from `app.main:app` — no running server, database or Redis needed.

## Coverage

Every REST route the SPA calls is typed, including the dbt / FlinkSQL / Data
Quality proxy routes (`/api/v1/dbt/*`, `/api/v1/flink/*`, `/api/v1/dq/*`):
the backend declares response models for them in `app/schemas/proxy.py`
(the bytes are still forwarded untouched; the models are documentation that
tolerates extra upstream fields), and documents the pass-through request
bodies inline, which `src/api/contract.ts`'s `JsonRequestBody<path, method>`
reads. `src/types/index.ts` is therefore aliases into the generated schema.

Still hand-written, by design:
- WebSocket envelopes/payloads — `src/ws/types.ts` (OpenAPI covers REST only).
- The handful of routes whose backend handler returns a bare dict with no
  `response_model` (git push/pull/branch/from-git, `/executions/{id}/result`)
  — see the header of `src/types/index.ts`; alias them once the backend
  declares models.

## Updating the contract

After a backend API change:

```sh
# 1. backend repo — regenerate its committed contract (CI: api-contract.yml)
cd InaiEra_workspace_backend
python scripts/export_openapi.py openapi/workspace.json
#    (or, with only Docker:)
docker run --rm -v "$PWD":/src -w /src --entrypoint python \
  inaiera-workspace:local scripts/export_openapi.py openapi/workspace.json

# 2. frontend repo — copy it in and regenerate the types
cp ../InaiEra_workspace_backend/openapi/workspace.json openapi/workspace.v0.1.0.json
npm run codegen:api
npm run typecheck   # contract changes surface here as compile errors
```

Commit the backend `openapi/workspace.json`, this snapshot, and
`src/api/generated/workspace-schema.ts` together. If the backend bumps
`info.version`, name the snapshot `workspace.v<new-version>.json`, delete the
old one, and update the path in `package.json`'s `codegen:api` script.

## CI drift checks

- Backend `.github/workflows/api-contract.yml`: exports the schema from
  `app.main:app` and fails if it differs from `openapi/workspace.json`.
- Frontend `.github/workflows/ci.yml` ("API contract drift" step, also
  `npm run codegen:check` locally): regenerates the types from this snapshot
  and fails if `src/api/generated/workspace-schema.ts` changes.

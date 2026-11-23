# Repository model

This repo (`InaiEra_DataConnect_Frontend`) and
`AadhyaAnalyticsInc/InaiEra_DataConnect_Backend` are two separate,
independently owned repositories — not a monorepo. Each has its own release
cadence, its own toolchain (Node/eslint/Playwright here, Python/ruff/mypy
there), and neither's CI depends on checking out the other's code as a
`backend/`/`frontend/` subdirectory of a shared tree.

## What this means for CI

- **This repo owns its own workflow** (`.github/workflows/ci.yml`): lint
  (`eslint`), type check (`tsc --noEmit`), the Playwright test suite
  (`tests/unit` + `tests/e2e`), a production build, and a Docker build smoke
  test — all against this repo's own code only.
- **Compatibility with the backend is checked from the backend repo's CI**,
  not here: its `contract-check` job regenerates this repo's
  `src/types/dataconnect-api.generated.ts` from the backend's current
  OpenAPI schema and fails if the result differs from what's committed here.
  That's the real, current signal of "these two are still compatible" for an
  API-coupled pair like this, rather than an arbitrary version-number pin
  between a Python service and a JS frontend that don't otherwise share a
  versioning scheme.
- To keep that check meaningful: after any backend API change, run
  `npm run gen:api-types` (see `package.json` — it points at
  `DATACONNECT_OPENAPI_URL` or `http://localhost:8001/openapi.json` by
  default) and commit the regenerated file.

The full rationale — including why a monorepo was considered and rejected,
and what would need to change if that ever flips — lives in the backend
repo's `docs/REPOSITORY_MODEL.md`, which is the canonical copy; this file is
a pointer plus the parts specific to owning this repo's own CI.

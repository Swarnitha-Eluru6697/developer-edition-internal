# Repository model

DataConnect ships as **two separate, independently owned repositories**, not
a monorepo:

- `AadhyaAnalyticsInc/InaiEra_DataConnect_Backend` (this repo) — FastAPI,
  Python 3.11, code at the repo root (`app/`, `tests/`, `alembic/`, ...).
- `AadhyaAnalyticsInc/InaiEra_DataConnect_Frontend` — React/TypeScript/Vite
  Module Federation remote, also at its own repo root (`src/`, `tests/`, ...).

This is a deliberate choice, not an accident of history: the two deploy on
different cadences (the frontend is a federation remote loaded by a separate
Control Portal host; the backend is a data-plane service), have unrelated
release surfaces, and neither needs the other's tooling (Python/ruff/mypy vs.
Node/eslint/Playwright) available at the same commit. A monorepo would force
both onto one CI run and one version history for no benefit either side
actually needs.

This repo's `.github/workflows/ci.yml` previously assumed a monorepo layout —
`working-directory: backend` / `working-directory: frontend` under one
checkout — which never matched either repo's real, flat structure. Neither
half of that assumption was ever correct: this repo has no `backend/`
subdirectory, and it never checked out the frontend repo's code at all (the
default `GITHUB_TOKEN` in a workflow run can't clone a sibling repository).
That's fixed now; see below for what replaced it.

## What "separate repos" means operationally

1. **Each repo owns its own build/test workflow**, testing only itself:
   - This repo's `ci.yml`: `ruff check` / `ruff format --check` / `mypy`,
     then `pytest --cov` against a real Postgres service container, then a
     Docker build smoke test of this repo's own image.
   - The frontend repo's `ci.yml`: `eslint`, `tsc --noEmit`, the Playwright
     suite (`tests/unit` + `tests/e2e`), `vite build`, then a Docker build
     smoke test of its own image.
   - Neither workflow references the other repo's directories, dependencies,
     or toolchain.

2. **An integration pipeline checks the two are still compatible**, without
   requiring them to share a version number (they don't, and forcing lockstep
   semver between a Python backend and a JS frontend that already both have
   their own independent `version` fields would be theater, not a real
   compatibility guarantee). Instead, compatibility is defined by the actual
   coupling between them: the frontend's `src/types/api.ts` is built on top
   of `src/types/dataconnect-api.generated.ts`, generated from this backend's
   OpenAPI schema via `npm run gen:api-types` (see the frontend repo's
   `package.json`). Two repos are "compatible" exactly when that generated
   file, as committed in the frontend repo, matches what this backend's
   *current* schema would produce.

   The `contract-check` job in this repo's `ci.yml` verifies that directly:
   it dumps this backend's live OpenAPI schema (no server or database
   needed — `app.openapi()` is derived from route definitions at import
   time), checks out the frontend repo alongside it, regenerates
   `dataconnect-api.generated.ts` from that schema, and fails the build if
   the result differs from what the frontend has committed. A failure means
   someone changed a backend route/schema without the frontend regenerating
   and committing its types — a real, current incompatibility, not a
   hypothetical one.

   This requires a `FRONTEND_REPO_TOKEN` secret in this repo (a fine-grained
   PAT, or a GitHub App installation token, with read access to
   `InaiEra_DataConnect_Frontend`) — the default `GITHUB_TOKEN` can only
   check out the repo the workflow is running in. Add it under this repo's
   **Settings → Secrets and variables → Actions**.

## If this ever needs to become a monorepo instead

It doesn't today, but if the two are ever merged: move both trees under
`backend/` and `frontend/` subdirectories in one repo, replace both `ci.yml`
files with a single workflow using path filters (`on.push.paths`) so a
backend-only change doesn't re-run frontend jobs and vice versa, and replace
the `contract-check` job above with a same-checkout step (no second
`actions/checkout`, no `FRONTEND_REPO_TOKEN` needed) that runs
`gen:api-types` directly against the local backend source. Don't do both
models at once — the version of `ci.yml` in each repo right now is written
entirely for the two-repo model above.

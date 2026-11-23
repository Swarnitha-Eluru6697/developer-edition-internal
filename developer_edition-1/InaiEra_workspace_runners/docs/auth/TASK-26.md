# Task 26 — Workspace (backend, frontend, runners): role-free authorisation

Copied from the InaiEra role-free authorisation pack (TASKS-APPS.md section 26). Read 01-CONTRACT.md first.

## 26 — InaiEra_workspace_backend + InaiEra_workspace_frontend + InaiEra_workspace_runners

Workspace is already capability-based (`contracts/rbac.contract.v1.json`, generated into the frontend by
`scripts/generate-rbac.mjs`). Keep the capabilities; move the role -> capability matrix out of the code.

Backend:
1. Rename capabilities to the contract format and publish them in `permissions.json` (e.g. `workspace:access:view`,
   `workspace:files:edit`, `workspace:sql:run`, `workspace:notebooks:run`, `workspace:dbt:edit|run`,
   `workspace:sql-jobs:edit|run`, `workspace:python-jobs:edit|run`, `workspace:flink:edit|deploy`,
   `workspace:dq:view|edit|run|publish`, `workspace:git:write`, `workspace:settings:edit`).
2. Remove the role list and role -> capability matrix from `contracts/rbac.contract.v1.json` and code; the
   principal's `permissions` (gateway-auth v1.1) are the capabilities.
3. Delegated tokens to the runners: put the needed permission in the token (`scope` already exists) instead of
   roles; runners check the scope/permission, not role names.
4. Require `x-user-id` (contract §4) outside dev as today.

Frontend: remove the generated role matrix and the role simulator from production builds; gate UI by `permissions`
from `/api/v1/me`.

Runners (`inaiera-dbt-runner`, `inaiera-flinksql-runner`): replace "roles against the same capability matrix"
checks with the permission carried in the delegated token.

Acceptance: contract §6 A–G for the Workspace API; a user with `workspace:dbt:run` can run dbt, without it 403;
no role names in any of the three repos outside tests/seed.

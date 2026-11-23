# The platform gate (X-007)

The findings of this review that hurt most were mismatches **between** components — a secret one service mounts and its callee never reads,
a Service URL that resolves to nothing, a chart key the application ignores, two services that disagree on a role. A single repository's unit
tests cannot see those. The gate checks them, in two layers.

## 1. Static gate (no stack needed): `scripts/platform_gate.sh`

Runs every repo's own suite, then the cross-repo contracts, chart lint and the generated-file drift checks. Sibling repositories are read from
next to this one (or `RUNNERS_REPO`, `DQ_REPO`, `FRONTEND_REPO`, `DAGSTER_REPO`); a missing one is skipped loudly.

| Check | Where | What it catches |
|---|---|---|
| Every production chart renders | `tests/test_platform_contract.py` | a chart that no longer renders (workspace, dbt runner, Flink runner, DQ, Dagster); the Flink cluster chart needs `helm dependency build` and is checked by the CI job |
| Rendered env vs the service's settings | same | a chart key the application never reads (found by reading each service's `Settings` and `os.environ` use from source) |
| Shared secrets, both ends | same | a signing secret a caller mounts that the callee never mounts, or a plain-text secret, or one Secret reused for two purposes. **Found a real one:** DQ's chart left `INBOUND_SERVICE_JWT_SECRET` empty by default while Workspace's chart requires its end of it |
| Service URLs vs Services | same | Workspace's `DBTRUNNER_URL` / `FLINKSQL_RUNNER_URL` / `DQ_SERVICE_URL` must name a Service the target chart creates, on the port the URL implies |
| Shared vocabularies | same | the quality-contract version and topic names DQ and the Flink runner must agree on; the org id; the audiences |
| RBAC contract | `tests/test_rbac_contract.py` | roles/capabilities drifting between Workspace, the runners, DQ's role table and the frontend's generated copy; each DQ route's required capability |
| Delegation | `tests/test_delegated_tokens.py`, `tests/test_dq_delegation.py` | scopes, audiences and keys disagreeing across Workspace, DQ and the runners |
| Committed contracts current | `scripts/export_rbac_contract.py --check`, `scripts/export_openapi.py --check` | a stale contract file |
| Frontend | `npm run codegen:rbac:check`, typecheck, vitest | the generated RBAC module not matching the contract |

CI: `.github/workflows/platform-contract.yml` checks the repositories out side by side and runs the contract tests. It needs a repository secret
`PLATFORM_REPOS_TOKEN` with read access to the sibling repositories (written, not yet run on GitHub).

## 2. Live scenarios: `scripts/e2e/platform_e2e.py` (a running stack)

Drives the real gateway -> Workspace -> dbt runner / Flink runner / DQ / UCS stack and cleans up after itself. `--list` shows the scenarios.

| Scenario | What it proves |
|---|---|
| `identity` | the subject is the identity through the gateway; decisions come from the ticket's permissions only (a reader reads, another app's permissions open nothing), enforced by Workspace AND DQ; a browser token is refused by every runner; a legacy user row is re-keyed to its subject in place and a rename keeps the row |
| `flink` | a real deployment: delegation, idempotent retry, running in the real cluster, the runner's actor and manifest name the subject, cancel, idempotent cancel |
| `dbt` | a real run: idempotent retry (same key, same run), cancel then a clean 409 on a finished run, a second run built to completion, its manifest names the subject |
| `dq` | bind + apply to a real quality job; the runner knows its DQ artifact; DQ froze an immutable manifest; applying again is idempotent |
| `restart` | restart Workspace mid-deployment: it recovers, the deployment (owned by the runner) still reaches running, the client's retry is idempotent |
| `outages` | Redis, UCS, Kafka stopped and the Flink runner frozen/stopped: `/readyz` says not ready, unrelated routes keep working, audit-mode policy neither blocks nor slows, a frozen runner is a 504 within the bound, a stopped one a 502 immediately, everything recovers without a restart |
| `policy` | UCS enforcement against the real UCS for `flink.deploy` only: denied without a grant (403, runner never asked, denial in the audit log), fails closed (503) with UCS down, audit-only actions keep working, RBAC still answers first, then back to audit |
| `secrets` / `scannercheck` | no secret value of any service appears in logs, audit/metadata tables, manifests or generated SQL — and a planted canary is detected, so a clean result means something |

Not covered by the scenarios, by design: the Flink REST endpoint is embedded in the runner container locally, so "Flink REST down" is
simulated by freezing/stopping the runner (the platform sees the same thing); Kafka is exercised as an outage of the runner's bus, not a
Kafka-sourced job's behaviour.

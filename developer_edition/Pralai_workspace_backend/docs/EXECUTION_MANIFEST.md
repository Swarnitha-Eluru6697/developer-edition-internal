# Execution manifest (X-004)

Every execution freezes what it will run **when it is submitted**, stores it immutably next to the execution, and runs
**only that**. A retry, a restart-resume or a takeover by another replica executes the exact manifest; a user who wants
newer source, newer rules or a different configuration creates a **new** execution, which freezes its own manifest.

## The shared shape (version `"1"`)

| Field | Meaning |
|---|---|
| `manifest_version` | `"1"` |
| `org_id`, `workspace_id` | tenant and workspace the execution belongs to (`workspace_id` is null when there is none) |
| `subject_id` | who asked: the user id, or the scheduler identity (`dagster`, `scheduler`) |
| repository, requested ref, commit | the Git source: URL **without credentials**, the ref that was asked for, the exact commit executed |
| `project_path` / SQL | the dbt project folder; for Flink, the SQL text itself and its sha256 (`sql_hash`) |
| `dq_artifacts` | which DQ artifacts (id, version, sha256) govern the execution |
| `engine`, `runtime_version` | the engine (`trino`, `pg_lake`, ...) and the versions that execute it |
| `target_resource_id` | the resource the execution writes to, when known up front |
| `idempotency_key` | the client's key: a retry with the same key returns the same execution |

The hash of the canonical JSON (`sha256`) is stored in a separate column and re-checked **before every use**; a database
trigger refuses to change the manifest (or its hash, or the frozen commit/`sql_hash`) once set.

## Where it lives

| Product | Stored in | Frozen by | Executed from | Audit route |
|---|---|---|---|---|
| **dbt runner** (also Dagster runs) | `runs.manifest`, `runs.manifest_sha256`, `runs.git_sha` | `RunService.submit` (DBT-006/007, X-004) | `dispatch_run` / the Job, from the manifest only | `GET /api/v1/projects/{id}/runs/{run_id}/manifest` |
| **Flink runner** (incl. Git imports) | `flink_deployments.manifest`, `manifest_sha256`, `sql_hash` | `JobService.deploy` (X-004) | `_run_deploy` (also a reconciler resume), from the manifest only | `GET /api/v1/jobs/{job_id}/deployments/{deployment_id}/manifest` |
| **Dagster** | no manifest of its own: its `git_commit_sensor` pins the commit it saw (`git_sha`) and an `Idempotency-Key` into the dbt runner's manifest (DG-007, X-004) | | | |

### dbt manifest keys

`manifest_version`, `org_id`, `workspace_id`, `subject_id`, `trigger`, `git` = `{url, ref, sha, source}` (repository,
requested ref, commit; `source` is `"ref"` or `"request"` when Dagster pinned it), `project_path`, `engine`, `select`,
`runtime_version` = `{dbt_core, runner}`, `target_resource_id` (null: the datasets a build writes are known only after it
ran), `idempotency_key`, `dq_artifacts` (list of `{model, id, version, sha256, content_hash, renderer_version}`) and
`dq.blocks` (the rules themselves, each with its own sha256).

`sql_hash` for dbt: the commit content-addresses the SQL, and the Job additionally reports Git's tree id of the project
folder it built (`runs.source_tree_sha`, shown by the audit route) as evidence recorded when the build finished.

### Flink manifest keys

`manifest_version`, `org_id`, `workspace_id`, `subject_id`, `job` = `{id, name, kind, generation}`, `source` (the Git
import: `{git_url, git_ref, sql_path, commit_sha, provider}`, or null for SQL typed in by hand), `sql` =
`{text, sha256}`, `sql_hash`, `engine` (`sink_engine`), `parallelism`, `from_savepoint`, `runtime_version` =
`{flink, runner}`, `target_resource_id` (`ucs_fqn`), `dq_artifacts` (null = not recorded), `idempotency_key`.

A job's `source` is set only by Workspace's Git-import routes (the only callers that really cloned the repository);
Workspace strips a client-supplied `source` from the pass-through create/update routes. Editing a job's SQL without a new
source clears the recorded source (the SQL is no longer from that commit).

## Not covered

* **DQ service executions**: since the DQ repository became editable, every DQ *apply* freezes the same manifest shape
  (`service: "dataquality"`, stored with its sha256 in `dq_artifact_applications`, immutable by trigger, readable through
  `GET /api/v1/bindings/{id}/applications[/{application_id}/manifest]` on the DQ service) and the Flink runner is told which DQ
  artifact a quality job came from (`dq_artifacts`, with the manifest hash), which it freezes into every deployment manifest.
  Still not covered: DQ's own rule *validation* and Python-job (GX/Soda) artifact pulls have no manifest (nothing is pushed or run).
* Flink SQL typed by hand has no repository; its manifest has `source: null` and the SQL text + hash.
* `target_resource_id` is only known up front for Flink quality jobs.

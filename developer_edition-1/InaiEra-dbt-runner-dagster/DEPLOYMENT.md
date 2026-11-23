# Dagster Phase 2 — Deployment Guide

This document covers deploying the Dagster orchestration layer alongside
`inaiera-dbt-runner`. It assumes you have the updated source code from
the `runners-dev` workspace and the Dagster package delivered in this zip.

---

## Who schedules dbt projects — read this first

| Where | Scheduler of record | Dagster's role |
|---|---|---|
| **Local docker-compose** (section 2) | **Dagster**: the runner runs with `SCHEDULER_ENABLED=false`; per-repo code locations with `project_schedule_sensor`, `git_commit_sensor` and `dbt_run_monitor_sensor` | schedules and runs |
| **Production / Kubernetes Helm** (section 3) | **The dbt Runner's own scheduler** (`SCHEDULER_ENABLED=true`, the default) | manual runs from the Launchpad only. The chart deploys one generic location whose job has **no sensors**, so Dagster schedules nothing there |

Do **not** set the runner's `scheduler.enabled=false` in production. With the Helm chart's generic
Dagster location nothing would replace it, and scheduled projects would silently stop running (DG-001).
The runner chart now refuses `scheduler.enabled=false` unless `scheduler.externalOrchestrator` names the
replacement. Moving production to Dagster needs per-repo locations and sensors deployed in Kubernetes;
see "Moving production scheduling to Dagster" at the end of section 3.

---

## Architecture Overview (local docker-compose; production differs, see above)

```
┌─────────────────────────────────────────────────────────────┐
│  Dagster code servers  (gRPC :4001+, one per Git repo)      │
│  ├── project_schedule_sensor  (every 30 s)                  │
│  │     GET /api/v1/projects/scheduled → evaluate cron       │
│  │     POST /api/v1/projects/{id}/run  → submit             │
│  └── git_commit_sensor  (every 60 s)                        │
│        GET …/internal/projects/git-triggered (DG-008)       │
│        GET …/internal/projects/{id}/head → detect new SHA   │
│        (the Runner runs git ls-remote, DG-009)              │
│        POST /api/v1/projects/{id}/run (select=modified+)    │
│                                                             │
│  Dagster daemon  (sensor eval, run queue, heartbeat)        │
│  Dagster webserver  (:3000) — UI + GraphQL                  │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP (internal only)
┌───────────────────────────▼─────────────────────────────────┐
│  inaiera-dbt-runner  (:8082)                                │
│  SCHEDULER_ENABLED=false  (Dagster owns scheduling)         │
│  ├── reconciler_loop  (leader-elected)                      │
│  └── outbox_loop      (leader-elected)                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Code changes in `inaiera-dbt-runner`

These are already applied in the runners repo (`inaiera-dbt-runner/`); the files under
`patches/app_*.patch` are the earlier draft and are superseded by what is listed here.

| File | Change |
|---|---|
| `app/core/config.py` | New setting `scheduler_enabled` (env `SCHEDULER_ENABLED`, default `true`). |
| `app/main.py` | Leaves `scheduler_loop` out of the leader loops when `SCHEDULER_ENABLED=false`. The reconciler and outbox always run. |
| `app/api/projects.py` | Seven internal routes, scoped to this deployment's org and authenticated with Dagster's own short-lived scoped JWT (`X-Internal-Service-Token` header, signed with `SCHEDULER_JWT_SECRET`, DBT-003): `GET /api/v1/internal/projects` and `/scheduled` and `/{id}` (scope `dbt.projects.read`), `POST /api/v1/internal/projects/{id}/run` (`dbt.runs.trigger`), `GET /api/v1/internal/runs/{id}` (`dbt.runs.read`), and the two cancel routes (`dbt.runs.cancel`, DG-017). The static `INTERNAL_SERVICE_TOKEN` is refused on them. |
| `docker/docker-compose.yml` | Passes `SCHEDULER_ENABLED` through (default `true`). |

The user-facing `/projects/{id}/run` and `/projects/{id}/runs/{id}` routes are unchanged and still
need a login token. The static internal token can now list and trigger runs, so use a long random
value for it outside local development.

### What a run executes is frozen at submission (DBT-006, DBT-007)

A run used to remember only its selector: the Git branch and the DQ schema rules were read when the run was finally
dispatched, so a run that waited in the queue could build a newer commit under newer DQ rules than anyone saw when it
was submitted. Now `RunService.submit` freezes an immutable manifest on the run row (`runs.manifest`, `manifest_sha256`,
`git_sha`):

* the commit the project's ref points to (`git ls-remote`; a branch, a tag, or a full SHA), the dbt folder, engine
  and selector;
* every DQ schema block the project holds at that moment: its YAML, a sha256 of it, and the DQ artifact version,
  binding, content hash and renderer version it was applied with.

Dispatch, a retry, a requeue after a crash and adoption by another replica read **only** the manifest. The Job's Secret
carries `GIT_SHA` and the frozen DQ blocks, the Job checks out exactly that commit (and refuses to build any other),
and a result that reports a different commit is recorded as a failure. A new commit or new DQ rules need a **new run**.
The manifest holds no Git credential (the URL is stored without its user info; the clone credential still comes from
the project's URL at dispatch, so a rotated token keeps working, and a project repointed at another repository is
refused). A database trigger (migration `0009_run_manifest`) refuses to change a manifest, its hash or `git_sha` once
set, and the runner re-checks the hash before using it.

* **Submission fails if the ref cannot be resolved**: `422 GIT_REF_NOT_FOUND` (no such branch, tag or SHA) or
  `502 GIT_UNREACHABLE` (Git host unreachable, bad credential, or no answer within `GIT_RESOLVE_TIMEOUT_SECONDS`,
  default 30). No run is queued. This also applies to Dagster's `POST /internal/projects/{id}/run` (the op fails and
  retries on its normal schedule) and to the built-in scheduler (the occurrence is not consumed, so the next tick
  tries again).
* **The API pod now reads refs from the Git host** at submission (`git ls-remote`; refs only, never code). Its
  egress must reach the Git host: 443 is allowed by the chart's default `networkPolicy.api.egressPorts`; add 22 for ssh
  remotes. The Git server must allow fetching a commit by SHA (GitHub, GitLab and Bitbucket Cloud do); otherwise the Job
  falls back to fetching the ref's history and requires the commit to be in it. A commit that was force-pushed away
  fails the run clearly instead of building something else.
* **Audit**: `GET /api/v1/projects/{id}/runs/{run_id}/manifest` returns the manifest (commit, DQ artifact versions,
  content hashes and the rules themselves) and its hash. `git_sha` on the run is now set at submission.
* **Rolling deploys**: a run queued by an older runner version has no manifest; it is frozen (and the commit
  resolved) at its first dispatch, before it can execute, and failed with a clear message if that is impossible.
  A Job created by an older runner has no `RUNNER_GIT_SHA` and still clones its ref.

### The commit the git sensor saw is the commit that is built (DG-007)

`git_commit_sensor` used to detect commit A and use it only as the run key and a tag; the request to the Runner carried
no commit, so the Runner resolved the branch for itself and a run labelled A could build B if the branch advanced
between the sensor tick and the run's submission. Now:

* the sensor resolves the commit **exactly** (the full ref name: `main` no longer matches `refs/heads/feature/main`;
  a branch beats a tag of the same name; an annotated tag is peeled to its commit; a full SHA is used as is) and puts
  it in the run config: `submit_run_op.config.git_sha`, as well as the `git_sha` tag and the run key;
* `submit_run_op` sends it as `git_sha` in `POST /api/v1/internal/projects/{id}/run`; the Runner freezes exactly that
  commit in the run's manifest (`manifest.git.source = "request"`) without consulting the ref, and the Job checks out
  that SHA (DBT-006);
* the Runner returns the commit it froze, and `submit_run_op` **fails the step** if it is not the one requested, or if
  the Runner does not report one. That is what an older Runner (which ignores `git_sha` and builds the branch tip) looks
  like, so deploy the Runner **before or with** Dagster. Dagster has no cancel scope, so the older Runner's run still
  executes; cancel it from the Workspace if needed;
* only the scheduler's internal route accepts `git_sha` (a full 40-character hex SHA, else `422`). The user-facing
  `POST /projects/{id}/run` refuses it with `422 GIT_SHA_NOT_ACCEPTED`, so a user cannot run a commit that is not on
  the project's ref. Cron and manual runs pin nothing: the Runner resolves the ref at submission and freezes it.

The Runner does **not** check that a pinned SHA is on the project's branch (it trusts Dagster's scoped identity, which
could already trigger runs); the manifest records `source: "request"` so an audit can tell. A SHA that does not exist in
the repository fails the run at checkout with a clear message.

### The shared execution manifest and idempotent submission (X-004)

The run manifest now follows the platform-wide shape (see `InaiEra_workspace_backend/docs/EXECUTION_MANIFEST.md`):
`manifest_version: "1"`, `org_id`, `workspace_id`, `subject_id` (the user, or `dagster` / `scheduler`), `trigger`,
`git` (repository without credentials, requested ref, commit, source), `project_path`, `engine`, `select`,
`runtime_version` (`dbt_core`, `runner`), `target_resource_id`, `idempotency_key`, `dq_artifacts` and the DQ rules
themselves. (`version` in the first manifests frozen on 2026-10-14 is read as `manifest_version`.)

* **Idempotent submit.** `submit_run_op` sends `Idempotency-Key: dagster:<dagster run id>:submit_run_op`. A retried step
  (or a re-delivered launch) of the same Dagster run gets the dbt run the first call created, not a second build; a new
  Dagster run has a new key. The same key with a different request (`select` / `git_sha`) is refused with
  `422 IDEMPOTENCY_KEY_REUSED`. Keys are scoped to the project; the user-facing trigger route honours the header too.
* **Evidence of what was built.** The Job reports Git's tree id of the project folder it built (before DQ rules touch
  the working tree); it is stored as `runs.source_tree_sha` and returned by the manifest route. With the commit it
  content-addresses the SQL that ran.
* Migration `0010_run_idempotency_evidence` adds `idempotency_key`, `idempotency_request_hash` and `source_tree_sha`
  and a unique `(project_id, idempotency_key)` index.

### Least privilege inside the Dagster release (DG-003, DG-020)

**Credentials (DG-003).** Dagster's identity at the dbt Runner is the key that signs its scoped tokens (`SCHEDULER_JWT_SECRET`;
plus the runner URL and org id). It can enumerate projects and trigger runs, so it is mounted into **the code server only**.
Sensors are evaluated, and runs are executed, inside the code server's process; the daemon and the webserver only talk to it over
gRPC and need just the Dagster database and the code server's address. (The runner's static `INTERNAL_SERVICE_TOKEN` is held by no
Dagster pod at all, DBT-003.) In the chart: `commonEnv` (every pod) vs `runnerEnv` (code server). In the local compose stack: no
`env_file`; `docker/dagster/.env` is still read by compose for the `${VAR}` values, and only `dagster-code` is given the three runner
variables. `helm test` runs a short-lived pod with the same credential to prove connectivity; it is deleted when it succeeds.
Not split: the Dagster **database** password is shared by all three (each writes to the same Dagster metadata store); separate roles
would be a further hardening step.

**Network (DG-020).** With `networkPolicy.enabled` (the default) every Dagster pod is **default-deny** in both directions, and each
component gets exactly its flows:

| Component | Ingress | Egress |
|---|---|---|
| webserver | the gateway namespace (`webserver.ingress.auth.fromNamespace`) only; nothing while the ingress is off | DNS, Postgres, code server gRPC `:4000` |
| daemon | none | DNS, Postgres, code server gRPC `:4000` |
| code server | gRPC `:4000` from the webserver and the daemon pods only | DNS, Postgres, the dbt Runner (pod port 8080). **No Git**: the git sensor asks the Runner for each project's commit (DG-009) |
| `helm test` pod | none | DNS and the dbt Runner, by the same rule as the code server |

Tune with `networkPolicy.postgres.to` / `.ports` (default: the `dp-shared` namespace, 5432), `networkPolicy.dbtRunner.to` / `.ports`
(default: the runner's namespace, pod port 8080) (there is no `networkPolicy.git` any more). A CNI that enforces NetworkPolicy (Calico, Cilium, ...) is
required; elsewhere the policies are silently ignored. The policies are verified here by rendered-manifest tests (selectors cross-checked
against the pods the chart really deploys) but not enforced on a live cluster (none available locally): run `helm test <release>` after
installing, which exercises the runner rule through the same policy.

### The project registry is org-shared; `workspace_id` is a vouched-for label (DBT-012)

Every project registered on the dbt runner is visible to, and runnable by, every caller of the org with the dbt capability and scope
(Dagster's scheduler identity sees them all). **`workspace_id` is a grouping label, not an ownership or isolation boundary**; access is
decided by org + role capability. The runner accepts a `workspace_id` on registration only when it equals the `workspace_id` claim of
the signed delegated token, which Workspace sets after checking the caller owns that workspace; anything else is
`403 WORKSPACE_NOT_AUTHORIZED`. External CI registers without a workspace. Details: `InaiEra_workspace_backend/docs/RESOURCE_SCOPING.md`.

### Cancelling a run (DBT-005)

In Kubernetes mode a run is cancelled in two steps, so it never reads `cancelled` while its Job may still be
executing: `running` → `cancelling` → `cancelled`. The cancel request marks the run `cancelling` and asks the
cluster to delete the Job; the run becomes `cancelled` only once the Job is confirmed gone (or already finished).
A delete that fails, or a Job that is still terminating, leaves the run `cancelling` and the runner's reconciler
(elected leader, every `RUN_RECONCILER_POLL_INTERVAL_SECONDS`) keeps retrying, so a runner restart loses nothing.
A run queued without a Job, or any run in subprocess (dev) mode, has nothing to confirm and is `cancelled` at once.

* `cancelling` is **not** a terminal status: `dbt_run_monitor_sensor` keeps watching a run until it is really `cancelled`
  (DG-018) and only then reports it.
* **Cancelling from Dagster (DG-017):** see the next section.
* A run stuck in `cancelling` for longer than `RUN_CANCEL_STUCK_SECONDS` (default 300) logs a `run_cancel_stuck`
  error (repeated every 15 minutes), counts `run_cancel_stuck`, and `GET /readyz` reports
  `checks.cancellation = {"cancelling": n, "stuck": m}` with `run_cancellation_stuck` in `degraded`. Alert on that.
* Migration `0008_run_cancelling` adds the status and `runs.cancel_requested_at`; rolling it back turns any run
  still `cancelling` into `cancelled`.

### Stopping a Dagster run stops the dbt run (DG-017)

Terminating a Dagster run used to kill only the polling step: the dbt Kubernetes Job went on running (and modifying
data) with nobody watching. The Runner now has two cancel routes for the orchestrator, with a scope of their own
(`dbt.runs.cancel`: a token that may only start runs cannot stop them, and the other way round; single use, org-bound,
the static token is refused). Both end in the same DBT-005 transition as the user route (`running` -> `cancelling` ->
`cancelled` only once the cluster confirms the Job is gone):

* `POST /api/v1/internal/runs/{run_id}/cancel` (a finished run is 409, an unknown run or another org's is 404)
* `POST /api/v1/internal/projects/{project_id}/runs/cancel` with `{"idempotency_key": ...}`, for a submission whose
  answer Dagster never saw (it was terminated while the request was in flight, so it has no run id)

The Dagster client has `cancel_run(run_id, wait_seconds=)` and `cancel_run_by_key(project_id, key)`, and Dagster uses them:

| What happens | The Runner's run |
|---|---|
| `submit_run_op` is interrupted while the request is in flight (the Dagster run is terminated, or Ctrl-C) | found by its `dagster:<run id>:submit_run_op` key (retried for up to ~5 s while the Runner finishes the request) and cancelled |
| `submit_run_op` finds the Runner froze a different commit than the sensor saw (DG-007), or does not report one | cancelled: it must not go on to build the wrong commit |
| `submit_run_op` cannot write the monitor tags (DG-018) | cancelled: nothing would watch it |
| a submitted run is still going after `DBT_RUN_MAX_WAIT_SECONDS` (default 2 h) | cancelled by `dbt_run_monitor_sensor` (next section) |
| the run reaches `succeeded` / `failed` / `cancelled` by itself | never touched |

Cancelling is best effort and never hides the error that caused it: a failing cancel logs
`dagster.cancel_downstream_failed` and the step still fails with its original error.

**Since DG-018 a Dagster run no longer represents an in-flight dbt run** (it ends as soon as the Runner accepted the
run), so there is nothing in Dagster to terminate while a dbt run is building. To stop one, cancel it through the
Runner (`POST /projects/{id}/runs/{run_id}/cancel`, the Workspace UI): the monitor then reports `cancelled` and the
`dbt_run_result` run fails. Terminating a Dagster run still cancels in the submit window (first rows above).

---

### A submitted dbt run does not hold a Dagster run slot (DG-018)

The submit job used to be `submit_run_op` -> `poll_run_op`, so the Dagster run (a slot of the run coordinator,
`max_concurrent_runs`, and a run-worker process) stayed busy for as long as the Kubernetes Job took, up to the 2 h polling
ceiling, only to wait. Ten dbt builds at once used all ten slots. Now:

1. **`submit_run_op` is the whole job.** It POSTs the run to the Runner, records the run id in its metadata, tags its own
   Dagster run (`inaiera/dbt_monitor=pending`, `inaiera/dbt_run_id`, `inaiera/dbt_location`) and ends, within seconds. If it
   cannot write the tags it cancels the run it just created and fails (nothing would watch it).
2. **`dbt_run_monitor_sensor`** (one per code location, every 20 s) lists that location's runs still `pending` and asks the
   Runner for each one's status (one GET each; the state is the run tags, i.e. Dagster's own database: nothing to lose on
   a restart, no cursor that grows). When a run is `succeeded` / `failed` / `cancelled` it requests a **`dbt_run_result`**
   run with `run_key = dbt-result::<runner run id>` (a repeated tick or a replay never reports twice). A Runner that cannot
   be reached leaves everything pending and is tried again; a run the Runner no longer knows (404) is reported as failed.
3. **`dbt_run_result`** (one HTTP call, a slot for seconds) records the outcome metadata the polling step used to record
   (`status`, `duration_seconds`, `models_*`, `git_sha`, `error`), marks the submitting run `reported`, and **fails if the
   dbt run failed or was cancelled**, so a bad dbt run is still a failed Dagster run, in the Dagster run list and for any
   alerting on failed runs.
4. A run still going after **`DBT_RUN_MAX_WAIT_SECONDS`** (default 7200, the old polling ceiling) is cancelled by the
   monitor (DG-017) and reported, and its result run fails with "did not finish".

What you see in Dagster per dbt run: a `<project>` run (submitted, green within seconds, with the Runner's run id), then
a `dbt_run_result` run when it finishes (green or red, tagged with the Runner run id and the submitting Dagster run).
The generic location the Helm chart deploys gets the same monitor, and nothing else: it still has no sensor that starts a
run and no schedule, so the runner's own scheduler must stay on (DG-001).

Not changed: how a run is started, the Runner, its cancel semantics, and `scheduled_fire_time` / `git_sha` / idempotency.


### One cron occurrence starts one dbt run, whoever asks (DG-006)

Dagster's `run_key` (`<project>::<instant>`) is the first guard against a double fire, but it lives in Dagster's own
database: pruned history, a reset sensor cursor, a re-created location or a second orchestrator all get past it. The
Runner is the second, authoritative guard, and it is now actually engaged:

* The instant travels sensor -> run config -> `submit_run_op` -> the trigger body as `scheduled_fire_time` (timezone-aware
  ISO-8601; `POST /api/v1/internal/projects/{id}/run`). Only the scheduler route accepts it; the user-facing route
  answers `422 SCHEDULED_FIRE_TIME_NOT_ACCEPTED`. It must come with `trigger=scheduled` (default) and not be more than
  5 minutes in the future (`422 OCCURRENCE_NOT_SCHEDULED` / `OCCURRENCE_IN_THE_FUTURE`).
* The Runner inserts `(project_id, scheduled_fire_time)` into `scheduled_occurrences` **in the same transaction** that
  freezes and creates the run, and links them. The unique constraint makes this atomic across replicas and retries; a
  failed submission (e.g. the Git ref cannot be resolved) rolls the claim back, so the retry of that occurrence works.
* A second request for the same instant returns **the run the first one created** (HTTP 202, same body), whatever
  Dagster run or Idempotency-Key it carries; nothing new is started. It is **not** a 409: that would fail the Dagster
  step for a run that is, correctly, already in flight. A replay asking for something different (another `select` /
  `git_sha`) is refused with `422 OCCURRENCE_REUSED`; an occurrence whose run was since purged by retention answers
  `409 OCCURRENCE_ALREADY_FIRED` (it fired and must not fire again).
* The Runner's own scheduler uses the same table as its watermark, so running both orchestrators cannot fire one instant
  twice either. Runs without an occurrence (manual, git-sensor, generic job) are unchanged.

## 2. Local Development

The Dagster stack does **not** start its own dbt Runner. It talks to the one from the runners repo
on host port 8082 (via `host.docker.internal`).

```bash
# 1. Start the dbt Runner with Dagster owning the schedule (runners repo)
cd <runners repo>/inaiera-dbt-runner
SCHEDULER_ENABLED=false docker compose -f docker/docker-compose.yml up -d

# 2. Create your local env file once (it is git-ignored)
cd <this repo>
cp docker/dagster/.env.local docker/dagster/.env

# 3. Start Dagster (builds the image on the first run)
docker compose -f docker/dagster/docker-compose.yml up -d --build

# 4. Open the Dagster UI
open http://localhost:3001   # local compose publishes the UI on 3001 (DAGSTER_PORT)
```

`docker/dagster/.env` (copied from `.env.local`) holds `DBT_RUNNER_BASE_URL`, `SCHEDULER_JWT_SECRET` and
`ORG_ID`. Dagster signs a short-lived scoped JWT per request with `SCHEDULER_JWT_SECRET`, which must equal the
runner's `SCHEDULER_JWT_SECRET` (local default `local-dev-dbtrunner-scheduler-secret`); `ORG_ID` must equal the
runner's. Dagster does not hold the runner's static `INTERNAL_SERVICE_TOKEN` any more.

### Trying it

1. Register a dbt project in the Workspace (Jobs -> dbt), or via `POST /api/v1/projects/register`.
2. Give it a cron schedule (Schedule tab, e.g. `*/2 * * * *`) and enable it.
3. In the Dagster UI open **Automation -> Sensors**. `project_schedule_sensor` and
   `git_commit_sensor` should both be **Running** (one pair per Git repo). Within 30 s the schedule
   sensor starts the project's own job; follow it under **Runs** or on the job's page.

### How projects appear in the Dagster UI (local compose)

The **Jobs** page groups jobs by *code location*. Here that is one **folder per Git repo**, named
after the repo, with **one job per dbt project** (named after its folder in the repo):

```
inaiera-dbt-test             customers, products, ...   one job per dbt project
                             sensors: project_schedule_sensor, git_commit_sensor (both target every job)
```

Each job can also be launched by hand from its **Launchpad** (the project is already filled in).
There is no generic location; `definitions.py` (the generic `dbt_scheduled_run` job) is used only
by the Helm chart.

The `dagster-code` container runs `location_manager.py`: every 30 s (`SYNC_INTERVAL_SECONDS`) it lists
the runner's projects, starts/restarts/stops one gRPC code server per repo (ports 4001+), rewrites the
generated `workspace.yaml` (volume `dagster-workspace`) and asks the webserver to reload. A newly
registered project therefore shows up as a job within about 30 s; until then its sensor skips it
without losing its schedule. Job names allow only letters, digits and `_`, so `my-project` appears as
`my_project`. The Helm chart in `docker/helm` still deploys the single generic location, which has no
sensors; there the runner's own scheduler does the scheduling (section 3).

### Reloads are checked, retried and alerted on (DG-014)

After it rewrites `workspace.yaml` or restarts a location, the manager asks the webserver to reload (GraphQL
`reloadWorkspace` / `reloadRepositoryLocation`) and **checks the answer**: the HTTP status, GraphQL `errors`, the union
member Dagster answers with (it reports most failures as HTTP 200 with `PythonError` / `RepositoryLocationNotFound` /
`UnauthorizedError` in `data`) and, for a location, that it actually loaded. A failure that could succeed on retry
(webserver down or restarting, 5xx, a load error) is retried up to `LOCATION_RELOAD_ATTEMPTS` (4) times with backoff
1 s, 2 s, 4 s (cap 8 s, `LOCATION_RELOAD_DEADLINE_SECONDS` 60 for one call); a request that can never work (HTTP 4xx) is
not retried. A reload that still fails is **owed**: it is retried at the start of every 30 s sync pass until it works
(an unknown-location answer becomes a workspace reload, which also covers every location). One exhausted attempt ends
the pass, so a down webserver costs one call's attempts, not one per location.

Failures are visible three ways: an error log `location_manager.reload_failed` (with `kind`, `location`, `attempts`,
`error`), and Prometheus metrics on port `LOCATION_MANAGER_METRICS_PORT` (default 9102, `0` disables; in the container
network only, not published by compose):

| Metric | Meaning |
|---|---|
| `dagster_location_reload_total{kind,result}` | reloads asked for after retries; `kind` = `workspace` / `location`, `result` = `success` / `failure` |
| `dagster_location_reload_pending{kind}` | reloads still owed (0 when the UI is up to date) |
| `dagster_location_reload_last_success_timestamp_seconds{kind}` | Unix time of the last successful reload |

Suggested alerts: `dagster_location_reload_pending > 0` for 5 minutes (the Jobs page is stale), and
`increase(dagster_location_reload_total{result="failure"}[15m]) > 0` as an early warning. A webserver restart makes a
brief failure normal, which is why the first alert waits. The Helm chart does not run the location manager (single
generic location), so none of this applies there.

### Verifying the sensors

In the Dagster UI: **Automation -> Sensors -> Tick history**. A tick that says "skipped: empty
result" means the sensor ran fine and had nothing to do.

### Tearing down

```bash
docker compose -f docker/dagster/docker-compose.yml down      # keep volumes
docker compose -f docker/dagster/docker-compose.yml down -v   # also delete Dagster's database
```

To give scheduling back to the runner, restart it without the flag:
`docker compose -f docker/docker-compose.yml up -d dbtrunner` (from `inaiera-dbt-runner`).

---

## 3. Production — Kubernetes (Helm)

### Prerequisites

- Kubernetes 1.26+
- Helm 3.12+
- A Postgres for the Dagster metadata (production: an AWS RDS instance, with the dbt Runner's DB; or, for small / evaluation
  installs, `postgresql.enabled=true` and the chart deploys one in-cluster: StatefulSet + PVC, single replica, no backups)
- Container registry access (`registry.inaiera.com`)
- `kubectl` context pointing at the target cluster/namespace

### 3a. Push images

```bash
# Tag and push the dbt Runner image
docker build -t registry.inaiera.com/products/dbt-runner:0.x.0 \
  -f docker/Dockerfile .
docker push registry.inaiera.com/products/dbt-runner:0.x.0

# Tag and push the Dagster image
docker build \
  -t registry.inaiera.com/products/dbt-runner-dagster:0.1.0 \
  -f docker/dagster/Dockerfile.dagster .
docker push registry.inaiera.com/products/dbt-runner-dagster:0.1.0
```

### 3b. Create Kubernetes Secrets

```bash
NAMESPACE=inaiera-prod
ORG_ID=<the org id this data plane serves>   # names the dbt Runner Service: dbt-runner-<org id>

# Dagster metadata DB password
kubectl create secret generic dagster-pg-secret \
  --namespace=$NAMESPACE \
  --from-literal=password='<your-rds-password>'

# The runner's static internal token: it opens ONLY POST /api/v1/validate (Workspace's codegen loop). Held by
# the runner and Workspace, NOT by Dagster. Created for the runner chart's internalServiceToken.secretRef.
kubectl create secret generic dbtrunner-internal-token \
  --namespace=$NAMESPACE \
  --from-literal=token='<strong-random-token>'

# Dagster's scoped scheduler key (DBT-003): the SAME value goes into the dbt Runner's schedulerJwt secret.
# A random value of its own: NOT the runner's INTERNAL_SERVICE_TOKEN, which Dagster must not hold.
kubectl create secret generic dbtrunner-scheduler-jwt \
  --namespace=$NAMESPACE \
  --from-literal=secret='<strong-random-key-different-from-every-other-secret>'

# dbt Runner DB password (if not already created)
kubectl create secret generic dbtrunner-pg-secret \
  --namespace=$NAMESPACE \
  --from-literal=password='<dbtrunner-rds-password>'
```

### 3c. dbt Runner scheduling: leave the built-in scheduler ON

The dbt Runner chart (`docker/helm/dbtrunner`, runners repo) sets `SCHEDULER_ENABLED` from
`scheduler.enabled`, which defaults to **`true`**:

```yaml
scheduler:
  pollIntervalSeconds: 30
  enabled: true              # production: the runner's own cron scheduler (RUN-DBT-004)
  externalOrchestrator: ""   # only ever set together with enabled=false (see the end of section 3)
```

Do not apply `patches/helm_dbtrunner_*.patch`: they are the superseded Phase 2 draft that defaulted the
scheduler to *off* and are kept only for history. The built-in scheduler fires one run per cron
occurrence: a leader-elected loop plus `UNIQUE(project_id, scheduled_fire_time)`, so several replicas or
a restart never double-run an occurrence (after downtime, missed occurrences are coalesced into the latest
one rather than replayed).

### 3d. Deploy (or upgrade) the dbt Runner

`schedulerJwt.secretRef` is the Secret holding Dagster's scoped scheduler key (DBT-003); the name below is the chart's default.

```bash
helm upgrade --install dbtrunner docker/helm/dbtrunner \
  --namespace=$NAMESPACE \
  --set tenant.orgId=$ORG_ID \
  --set schedulerJwt.secretRef.name=dbtrunner-scheduler-jwt \
  --set image.tag=0.x.0 \
  --set database.host=<rds-endpoint>.rds.amazonaws.com \
  --set database.passwordSecretRef.name=dbtrunner-pg-secret \
  --set database.passwordSecretRef.key=password \
  --wait
```

### 3e. Create a `dagster-values.yaml`

```yaml
# dagster-values.yaml  — production overrides
image:
  repository: registry.inaiera.com/products/dbt-runner-dagster
  tag: "0.1.0"

deploymentTarget: prod

# Production: an external database (host REQUIRED; the chart refuses to render with neither this nor postgresql.enabled=true).
# For a small / evaluation install set `postgresql.enabled: true` instead (DG-004): the chart then deploys a single in-cluster Postgres
# (postgresql.internal.persistence.size / storageClass), initialised with the password from the SAME Secret below, which must exist.
postgresql:
  enabled: false
  external:
    host: <dagster-rds-endpoint>.rds.amazonaws.com
    port: 5432
    database: dagster
    user: dagster
    passwordSecretRef:
      name: dagster-pg-secret
      key: password

# Same org id as the dbt Runner release. Dagster derives the runner's address from it:
# http://dbt-runner-<orgId>.<namespace>.svc.cluster.local (Service port 80), the Service the runner chart
# really creates. There is no URL to type, and none to get wrong. `helm test` (3g) proves it.
tenant:
  orgId: "<org id>"

dbtRunner:
  # baseUrl: ""        # leave unset; set only for a runner reachable some other way
  # namespace: ""      # only if the runner is in another namespace than this release
  schedulerJwtSecretRef:        # Dagster's own key for the runner's /internal routes (DBT-003)
    name: dbtrunner-scheduler-jwt
    key: secret

# The Dagster UI / GraphQL API is an admin surface with NO authentication of its own (DG-002).
# Default and preferred: leave the ingress off and reach it with `kubectl port-forward` (see 3g).
# To publish it, an auth layer must sit in front, and the chart refuses an ingress without one:
webserver:
  ingress:
    enabled: true
    className: envoy              # your gateway's / controller's class (required)
    host: dagster.inaiera.com     # required, not the placeholder
    tls:                          # required
      - secretName: dagster-tls
        hosts:
          - dagster.inaiera.com
    auth:
      mode: gateway               # "gateway" or "oidc-proxy" (see "Securing the Dagster UI" below)
      fromNamespace: inaiera-gateway   # the ONLY namespace allowed to reach the webserver pod

persistence:                      # see "Storage: no ReadWriteOnce volume is shared" below
  enabled: true
  computeLogs:
    mode: rwx                     # or s3
    size: 20Gi
    storageClass: efs-sc          # REQUIRED for rwx: a ReadWriteMany class (EFS, NFS, Azure Files, CephFS)
  artifacts:
    size: 10Gi
    storageClass: gp3             # ReadWriteOnce is fine here: only the code server mounts it
```

#### Storage: no ReadWriteOnce volume is shared (DG-019)

The chart used to mount one ReadWriteOnce compute-logs PVC and one artifacts PVC into the code server, the daemon **and**
the webserver. A ReadWriteOnce volume attaches to one node, so pods the scheduler placed on different nodes (or a rollout
that started a new pod before the old one released the volume) stayed in `ContainerCreating` with a Multi-Attach error.
Each volume now goes only to the pods that use it:

| Data | Written / read by | Chart behaviour |
|---|---|---|
| Compute logs (step stdout/stderr) | written by the code server, shown by the webserver, so it must be **shared** | `mode: rwx`: one **ReadWriteMany** PVC mounted by the code server and the webserver only, or `mode: s3`: Dagster's `S3ComputeLogManager`, no PVC |
| Artifacts (step outputs inside a run) | the code server only | a ReadWriteOnce PVC mounted by the code server **alone**; its Deployment uses `strategy: Recreate` so the old pod releases the volume first |
| The same paths in the daemon, and the artifacts path in the webserver | nobody | `emptyDir` (the Dagster instance only needs the directories to exist) |

* `rwx` needs `persistence.computeLogs.storageClass` to be a ReadWriteMany class; the chart **refuses to render** without
  it rather than guess (an RWO class would leave the second pod pending). `persistence.enabled=false` makes everything an
  `emptyDir`, and then the UI cannot show step logs written in another pod.
* `s3` (`persistence.computeLogs.s3.*`: `bucket`, `prefix`, `region`, `endpointUrl` for MinIO/Ceph, `useSsl`) is the choice
  where no RWX class exists. Credentials: `s3.credentialsSecretRef` (an existing Secret; `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` go to the code server and the webserver only), or leave it empty and annotate the service account
  for IAM roles. The image contains `dagster-aws` for this.
* `codeServer.replicaCount > 1` cannot share the artifacts volume, so it is refused while `persistence.enabled`; use
  `persistence.enabled=false` with `mode: s3` for several code servers.
* Not verifiable here: there is no Kubernetes cluster in the local setup, so the volume attachment itself was not
  exercised; the manifests are checked by tests (no RWO claim is referenced by more than one Deployment, and a RWO claim's
  Deployment is a single replica with `Recreate`), and the S3 mode was run against a real Dagster instance and MinIO.

#### Securing the Dagster UI (DG-002)

Dagster's webserver and GraphQL API can launch runs, reload code locations and edit schedules, and have no
login. Two supported ways to publish them, and **the chart refuses an ingress that names neither**
(`helm lint/template/install` fail):

| `ingress.auth.mode` | What must be true | What the chart enforces |
|---|---|---|
| *(ingress off, default)* | Internal only; admins use `kubectl port-forward` | No Ingress, NetworkPolicy admits **no** inbound traffic, Service must be `ClusterIP` |
| `gateway` | The InAiEra Envoy gateway (`jwt_authn`) fronts the host, with an **admin-only** route policy (for example an RBAC rule on `x-user-roles`) | `className`, real `host`, `tls`, and `auth.fromNamespace`; the webserver pod accepts traffic **only** from that namespace |
| `oidc-proxy` | An SSO proxy (ingress-nginx external auth) restricts the host to administrators | The same, plus the annotations `nginx.ingress.kubernetes.io/auth-url` and `.../auth-signin` |

What this repo **cannot** check: that the gateway's admin-only policy (or the SSO group restriction) really
exists. That lives in the gateway/SSO configuration, so verify it there before announcing the URL: an
anonymous request and a non-admin user must both be refused. The NetworkPolicy needs a CNI that enforces it
(Calico, Cilium, ...). The code server's gRPC port (4000) is also unauthenticated; it is a ClusterIP service
and is not published.

#### Reaching the dbt Runner

Dagster calls the runner at `http://dbt-runner-<orgId>.<namespace>.svc.cluster.local` (port 80), computed
from `tenant.orgId`; `dagster/tests/test_helm_chart.py` render both charts and
fail if that stops matching the Service the runner chart creates. The runner's NetworkPolicy only admits
namespaces labelled `inaiera.com/data-plane: "true"` (`networkPolicy.api.ingressFrom`), so label the
Dagster namespace: `kubectl label namespace $NAMESPACE inaiera.com/data-plane=true`.

### 3f. Deploy Dagster

```bash
helm upgrade --install dagster docker/helm/dagster \
  --namespace=$NAMESPACE \
  -f dagster-values.yaml \
  --wait
```

### 3g. Verify production deployment

```bash
# All pods running?
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/instance=dagster

# Dagster webserver health
kubectl port-forward -n $NAMESPACE svc/dagster-dagster-dbt-runner-webserver 3000:3000 &
curl http://localhost:3000/health

# Can Dagster reach the dbt Runner (Service name/port, token and NetworkPolicy)? A failing test prints why.
helm test dagster --namespace=$NAMESPACE --logs

# Scheduling is the dbt Runner's job here, so verify it there (NOT in Dagster's sensor tick history,
# which stays empty because this deployment has no sensors):
kubectl exec -n $NAMESPACE deploy/dbt-runner-$ORG_ID -- env | grep SCHEDULER_ENABLED      # SCHEDULER_ENABLED=true
kubectl logs -n $NAMESPACE deploy/dbt-runner-$ORG_ID | grep -E "scheduler_starting|scheduler_triggering"
```

Register a project with a short schedule (for example `*/2 * * * *`) and confirm that exactly one run
per occurrence appears, including across a restart of the runner pod:

```sql
-- in the dbt Runner database: one row per occurrence, each with exactly one run
SELECT scheduled_fire_time, run_id FROM scheduled_occurrences
WHERE project_id = '<project id>' ORDER BY scheduled_fire_time DESC LIMIT 10;
```

### Moving production scheduling to Dagster later

Only when all of this exists and has been tested on a cluster:

1. One code location per Git repo in Kubernetes (the `location_manager` code server, or one Deployment per
   repo) with `project_schedule_sensor` and `git_commit_sensor` deployed, and a way for the webserver and
   daemon to read that location list (they read the generated `workspace.yaml`).
2. Only then set `scheduler.enabled=false` **and** `scheduler.externalOrchestrator=dagster` on the dbt
   Runner chart. Hand-over order matters: never run both, or an occurrence is fired twice. The runner's
   `ScheduledOccurrence` dedupe only covers the runner's own scheduler, not Dagster's sensor.
3. Verify with the same register / wait / restart check as above, reading runs rather than sensor ticks.

---

## 4. Environment Variable Reference

### Dagster containers

| Variable | Required | Default | Description |
|---|---|---|---|
| `DAGSTER_HOME` | Yes | `/dagster-config` | Where `dagster.yaml` is mounted (compose also generates `generated/workspace.yaml` here) |
| `DAGSTER_DEPLOYMENT_TARGET` | Yes | `local` | `local` or `prod` (controls resource config) |
| `DAGSTER_PG_HOST` | Yes | — | Postgres host for Dagster metadata |
| `DAGSTER_PG_PORT` | Yes | `5432` | — |
| `DAGSTER_PG_DB` | Yes | `dagster` | — |
| `DAGSTER_PG_USER` | Yes | `dagster` | — |
| `DAGSTER_PG_PASSWORD` | Yes | — | From Kubernetes Secret |
| `DAGSTER_CODE_SERVER_HOST` | Yes | — | Hostname of the gRPC code server (written into the generated workspace.yaml) |
| `DAGSTER_CODE_SERVER_PORT` | Yes | `4000` | — |
| `DBT_RUNNER_BASE_URL` | Yes | `http://localhost:8082` | dbt Runner API base URL. In Kubernetes the chart sets it from `tenant.orgId` (`http://dbt-runner-<orgId>.<ns>.svc.cluster.local`); `localhost:8082` is only the local compose host mapping |
| `SCHEDULER_JWT_SECRET` | Yes | `local-dev-dbtrunner-scheduler-secret` | Key Dagster signs its per-request scoped JWT with (sent in `X-Internal-Service-Token`); must equal the runner's `SCHEDULER_JWT_SECRET`. From a Kubernetes Secret |
| `ORG_ID` | Yes | `local-dev` | This data plane's org: the `org_id` claim of the scoped tokens (the runner rejects any other). In Kubernetes the chart sets it from `tenant.orgId` |
| `LOCATION_RELOAD_ATTEMPTS` / `LOCATION_RELOAD_DEADLINE_SECONDS` | No | `4` / `60` | Local compose location manager: reload attempts per call and the time budget for one call's retries (DG-014) |
| `DBT_RUN_MAX_WAIT_SECONDS` | No | `7200` | The longest a submitted dbt run may take before `dbt_run_monitor_sensor` cancels it and reports it as not finished (DG-018) |
| `LOCATION_MANAGER_METRICS_PORT` | No | `9102` | Local compose location manager: port of `/metrics` (`0` disables) |
| `DAGSTER_COMPUTE_LOGS_DIR` | No | `/var/lib/dagster/compute-logs` | Op compute log storage (local manager; with `mode: s3` it is only the local staging dir) |
| `DAGSTER_ARTIFACTS_DIR` | No | `/var/lib/dagster/artifacts` | Dagster artifact storage |

### dbt Runner (Phase 2 additions)

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | `false` only in local compose, or in production once Dagster sensors are deployed there (see section 3). The runner chart refuses `false` without `scheduler.externalOrchestrator` |
| `SCHEDULER_JWT_SECRET` | — | Verifies Dagster's scoped scheduler tokens on the `/internal` routes (iss/sub `dagster`, aud `dbtrunner`, one scope per route). Must differ from `SERVICE_JWT_SECRET`, `DELEGATION_JWT_SECRET` and `INTERNAL_SERVICE_TOKEN` (the runner refuses to start otherwise) |
| `INTERNAL_SERVICE_TOKEN` | — | Static token for the runner's `POST /api/v1/validate` only (Workspace's codegen loop). Not accepted on `/internal` routes, not held by Dagster |

---

## 5. Sensor Behaviour Reference

These sensors run in the **local compose** stack (per-repo code locations). The production Helm chart does
not deploy them; see "Who schedules dbt projects" at the top.

### `project_schedule_sensor` (cron scheduling)

- Polls every **30 s**
- Calls `GET /api/v1/projects/scheduled` — returns all projects with
  `schedule_enabled=true` and a non-null `schedule_cron`.
- Evaluates each project's cron with `croniter` to find the latest
  due-but-not-yet-fired time.
- Cursor per project: `{project_id: last_fire_iso_utc}` — persisted by
  Dagster across daemon restarts.
- `run_key = f"{project_id}::{next_fire.isoformat()}"` — Dagster deduplicates
  across retries. The same instant is sent to the dbt Runner as `scheduled_fire_time`, which claims it in its
  `ScheduledOccurrence` table (`UNIQUE(project_id, scheduled_fire_time)`) atomically with the run and returns the
  existing run for a replay (DG-006, section 1).

### `dbt_run_monitor_sensor` (outcome reporting, DG-018)

- Polls every **20 s**; one per code location (also in the generic Helm location). It never starts a dbt run.
- Lists the location's Dagster runs tagged `inaiera/dbt_monitor=pending` and `GET`s each one's status from the Runner.
- A terminal status -> a `dbt_run_result` run (`run_key = dbt-result::<runner run id>`); the run fails if the dbt run
  failed or was cancelled. Past `DBT_RUN_MAX_WAIT_SECONDS` -> cancelled and reported. Details: section 1,
  "A submitted dbt run does not hold a Dagster run slot".

### `git_commit_sensor` (CI/CD trigger)

- Polls every **60 s**
- For each project with **`git_trigger_enabled`** ("run on new commit", DG-008 — its own setting and its own listing,
  `GET /api/v1/internal/projects/git-triggered`; independent of whether the project also has a cron schedule), runs
  `GET /internal/projects/{id}/head` on the Runner (below) to get the current HEAD SHA without cloning.
- Compares against `project.last_synced_sha` from the API.
- On a new SHA, fires a full `dbt build`. (`state:modified+` needs a saved dbt manifest that the Runner does not keep yet, so the incremental run failed with "no comparison manifest".)
- `run_key = f"{project_id}::{remote_sha}"` — deduplicates across ticks.
- **Dagster never talks to Git** (DG-009). The sensor asks the dbt Runner for the commit a project's ref points to right now
  (`GET /api/v1/internal/projects/{id}/head`, scope `dbt.projects.read`). The Runner runs `git ls-remote` with the project's own
  credential (`credential_ref` → `GIT_CREDENTIAL_<REF>`, see the Runner chart's `gitCredentials`) and its own Git egress, with the same
  resolver a run submission uses. The code server image has no `git`, the chart has no Git credential setting and no Git egress rule
  (`codeServer.gitCredentials` and `networkPolicy.git` were advertised but never wired; a values file that still sets them is now
  refused by `helm lint/install`). A **private repository is configured once, on the Runner**. A project whose head cannot be
  resolved (host unreachable, bad credential, missing ref) is skipped for that tick and logged as `git_commit_sensor.head_unavailable`.

---

## 6. Troubleshooting

### Sensor shows "Failed" tick

```bash
# In Dagster UI: Automation → Sensors → <sensor> → Tick history → click a failed tick
# The error message is shown inline.

# Common causes:
# 1. dbt Runner unreachable: check DBT_RUNNER_BASE_URL and network policy
# 2. SCHEDULER_JWT_SECRET or ORG_ID differs from the runner's: sensor gets 401 (invalid token) or 403 ORG_MISMATCH
#    from /internal/projects/scheduled. 401 RAW_TOKEN_REJECTED means something is still sending the static token
# 3. No projects with schedule_enabled=true (project_schedule_sensor) / git_trigger_enabled=true (git_commit_sensor):
#    the sensor returns 0 run requests (not an error). A project is watched for commits ONLY when git_trigger_enabled is on
#    (PATCH /projects/{id} {"git_trigger_enabled": true}); migration 0012 turned it on for the schedule-enabled projects that
#    were being watched before, new projects start with it off
```

### Run submitted but never starts

```bash
# Check daemon logs
kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=daemon --tail=100

# Check run queue
# In Dagster UI: Runs → Queued
```

### Dagster DB migration

```bash
# Run once after first deploy (or after Dagster version upgrade)
kubectl exec -n $NAMESPACE \
  deploy/dagster-dagster-dbt-runner-webserver \
  -- dagster instance migrate
```

### dbt Runner scheduler not running (scheduled projects never fire)

```bash
# Production must show SCHEDULER_ENABLED=true
kubectl exec -n $NAMESPACE deploy/dbt-runner-$ORG_ID -- env | grep SCHEDULER_ENABLED

# Healthy: scheduler_starting at boot, scheduler_triggering at every occurrence
kubectl logs -n $NAMESPACE deploy/dbt-runner-$ORG_ID | grep -E "scheduler_starting|scheduler_triggering"
# "dbtrunner.scheduler_disabled" means SCHEDULER_ENABLED=false: with the Helm Dagster chart nothing else
# schedules, so set scheduler.enabled back to true.
```

---

## 7. Code-location names, Git authentication and logs (DG-010 / DG-011 / DG-012)

**A code location is named after its repository, not just its base name (DG-012).** The name is `<repo name>-<8 hex>`, e.g.
`inaiera-dbt-test-6bf3fd86`: the hash is of the *canonical* repository URL (host and path; no user info, `.git`, `#subdirectory`
suffix or transport, so `https://…/a/b.git`, `git@host:a/b.git` and `ssh://git@host/a/b` are one repository). `orgA/analytics.git` and
`orgB/analytics.git` therefore get two locations instead of one mixed code server; the readable part is only a label. The hash is
part of the contract (a location's name keys its Dagster run history): `dagster/tests/test_locations.py` pins it. **Changing to this
scheme renames every existing location once**: the old locations' run history stays in Dagster under the old name, and a dbt run that
was in flight at the upgrade is not picked up by the renamed location's monitor sensor.

**Supported Git authentication (DG-010).** Dagster does not talk to Git. The dbt Runner does, and it supports exactly two mechanisms:
(1) HTTPS with a per-project `credential_ref` (operator-provisioned `GIT_CREDENTIAL_<REF>`, sent as an Authorization header scoped to the
repository's host); (2) whatever the Runner pod's own environment provides — ssh keys (`SSH_AUTH_SOCK` / `GIT_SSH_COMMAND` / `~/.ssh`), a
credential helper, `GIT_ASKPASS`, proxy variables, a private CA (`GIT_SSL_CAINFO`). Every git subprocess inherits the pod's environment and
only ADDS to it (`GIT_TERMINAL_PROMPT=0`, plus the header for mechanism 1); the per-repo code-server processes Dagster starts inherit
Dagster's whole environment too. The Runner does not manage ssh keys.

**Credentials never reach logs (DG-011).** The Runner refuses a repository URL with user info at registration (422), redacts it in every
project it returns, and both the Runner and Dagster pass every log event (structlog values, standard-library records, tracebacks) and the
error text stored on a run through one scrubber (`app/core/log_scrub.py` / `inaiera_dbt_runner_dags/log_scrub.py`):
`https://ci:tok@host/x.git` appears as `https://***@host/x.git`.

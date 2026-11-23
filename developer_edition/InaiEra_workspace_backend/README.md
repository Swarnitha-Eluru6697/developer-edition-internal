# InAiEra Workspace Service

A per-org developer authoring surface deployed inside the customer data plane on the subdomain `orgid.app.workspace.inaiera.com`. It implements the Snowflake-Workspaces model — file-based development with per-user personal storage, multi-language editor, Git integration, and separate execution environments — for InAiEra's lakehouse and transformation stack.

---

## What this is

A FastAPI service plus a contract-level React/Redux frontend. Together they form one product (Workspace) that runs as an independent application inside each customer's data plane.

Per the architecture diagram, the Snowflake-Workspaces concepts map to InAiEra as follows:

| Snowflake-Workspaces concept | InAiEra implementation |
|---|---|
| User's Personal Database | Per-user prefix `{org_id}/{user_id}/{workspace_id}/{branch}/{path}` in cloud object storage (S3/ADLS/GCS); metadata rows in the data plane Postgres |
| File-based Workspace (.sql, .py, .ipynb, structured projects) | Files stored in cloud storage; metadata in Postgres. Phase 1 = `.sql` + `.ipynb` only. Project layout follows dbt conventions. |
| Execution Environments (Warehouse or SPCS) | SQL → Data Lake House query API; notebook cells → JupyterHub kernel pod spawned per user |
| External Git Repo | GitHub / GitLab / Azure DevOps via provider abstraction; OAuth tokens in the data plane's cloud secrets manager |
| VS Code / Local IDE | Same Git repo as Workspace — no special integration required |
| User | Authenticates via Control Plane (`www.app.inaiera.com`); JWT issued, browser redirected to `orgid.app.workspace.inaiera.com` with the token |

---

## How a user reaches the workspace

```
  ┌──────────────────────────────┐         ┌────────────────────────────────────┐
  │   Control Plane (SaaS)       │         │  Data Plane (customer cloud)       │
  │   www.app.inaiera.com        │         │  orgid.app.workspace.inaiera.com   │
  │                              │         │                                    │
  │  1) User logs in             │         │                                    │
  │  2) Mints JWT (RS256)        │         │                                    │
  │  3) Publishes JWKS at        │         │  4) Validates JWT locally          │
  │     /.well-known/jwks.json   │ ──JWKS─▶│     against cached JWKS            │
  │                              │         │  5) Serves the SPA + API + WS     │
  │  Browser ──redirect──with──token──┐    │                                    │
  └──────────────────────────────┘    │    └────────────────────────────────────┘
                                      │                          ▲
                                      └──────────────────────────┘
                                          User's browser talks
                                          DIRECTLY to data plane.
                                          Customer data never traverses
                                          the control plane.
```

Once authenticated, every HTTP and WebSocket request goes browser → data plane. The control plane is on the auth path only.

---

## Per-user "personal database"

Each developer has a private storage namespace inside the data plane:

```
{org_id}/{user_id}/{workspace_id}/{branch}/{path}
```

Example: `acme-corp/alice-uuid/ws-marketing-uuid/main/models/staging/stg_orders.sql`

This pattern, enforced by `app/storage/__init__.py:storage_key_for_file()`, gives:

- **Per-user isolation** — Alice's files cannot resolve to Bob's keys
- **Per-org isolation** — already physical because each data plane is in a different cloud account, but reinforced at the key level
- **Branch-aware storage** — the same logical file on different branches lives at different keys

Metadata about each file (path, language, content hash, branch, owner) is in the data plane Postgres. File content is in cloud object storage. The two are linked by the storage key.

---

## Project layout

```
workspace-service/
├── app/                       # FastAPI backend
│   ├── main.py                FastAPI entry, lifespan, route wiring
│   ├── core/
│   │   ├── config.py          Pydantic settings (env-driven)
│   │   ├── auth.py            JWT validation via JWKS (cached)
│   │   ├── deps.py            FastAPI dependencies (current_user, db)
│   │   └── errors.py          Error envelope
│   ├── api/
│   │   ├── auth.py            /auth/me, login bootstrap
│   │   ├── workspaces.py      CRUD on workspaces
│   │   ├── files.py           CRUD on files, tree listing
│   │   ├── executions.py      Submit SQL or notebook executions
│   │   └── git.py             OAuth flow, repo binding, commit, push, PRs
│   ├── ws/
│   │   └── gateway.py         /ws/sessions/{id} — JWT-in-query, Redis pub/sub
│   ├── db/                    SQLAlchemy async engine + session
│   ├── models/                Users, Workspaces, WorkspaceFiles, GitBinding,
│   │                          GitCredential, Sessions, Executions, AuditLog
│   ├── schemas/               Pydantic request/response models
│   ├── services/              Business logic: workspace, file, execution
│   │                          router (Redis fan-out), audit
│   ├── executors/
│   │   ├── sql_executor.py    Streams from Data Lake House query API
│   │   └── notebook_executor.py  JupyterHub + Jupyter messaging protocol
│   ├── git/                   GitProvider Protocol + GitHub, GitLab, ADO,
│   │                          and a local working-copy helper (gitpython)
│   └── storage/               BlobStore Protocol + S3, Azure, GCS
├── migrations/                Alembic — 0001_initial creates all tables
├── docker/
│   ├── Dockerfile             Production multi-stage build, non-root, tini
│   ├── docker-compose.yml     Local stack (Postgres, Redis, MinIO, service)
│   ├── git-credential-helper.sh
│   └── helm/workspace/        Helm chart deployed by Infra & Product Release
│       ├── Chart.yaml             Mgmt into each customer data plane
│       ├── values.yaml
│       └── templates/
│           ├── _helpers.tpl
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── ingress.yaml      Per-org subdomain wiring
│           ├── configmap.yaml
│           └── extras.yaml       ServiceAccount, HPA, migration Job
├── frontend/                  React + Redux + RTK Query + Monaco contract
│   ├── package.json
│   └── src/
│       ├── main.tsx
│       ├── api/
│       │   ├── client.ts      Typed RTK Query endpoints
│       │   └── auth.ts        Captures JWT from URL, redirects on 401
│       ├── ws/
│       │   └── middleware.ts  WS lifecycle + envelope→action bridging
│       ├── store/
│       │   ├── index.ts
│       │   └── executionSlice.ts   Accumulates streamed rows
│       ├── components/
│       │   ├── App.tsx        File tree | Monaco | Results pane
│       │   └── CodeEditor.tsx Monaco wrapper with run/save shortcuts
│       └── types/index.ts     Mirrors backend Pydantic schemas
├── tests/
│   ├── conftest.py            Real RSA key, JWKS cache priming, JWT minting
│   ├── test_auth.py           JWT validation paths
│   ├── test_workspaces.py     Workspace + file CRUD (integration)
│   └── test_storage_keys.py   Pin the per-user key pattern
├── alembic.ini
└── pyproject.toml
```

---

## Auth — JWT + JWKS in detail

1. User authenticates at `www.app.inaiera.com`.
2. Control plane mints an RS256 JWT (TTL ~15 min) with claims:
   - `sub`: user_id
   - `org_id`: customer org UUID
   - `email`: user email
   - `roles`: list (e.g. `["developer"]`)
   - `dp_base`: data plane base URL
   - `aud`: `workspace.inaiera`
   - `iss`: `https://www.app.inaiera.com`
   - `iat`, `exp`
3. Browser navigates to `https://orgid.app.workspace.inaiera.com/#token=<jwt>`. The token rides in the URL **fragment** only: browsers never send a fragment to any server, so it can't reach access logs, proxies or analytics. A `?token=` query string is not accepted (the SPA scrubs it and ignores it).
4. The SPA's auth slice reads the fragment, strips it from the URL, and calls `POST /api/v1/auth/exchange` with `Authorization: Bearer <jwt>`. The backend verifies that exact token (and that it belongs to the gateway-authenticated user) and answers with an HttpOnly, SameSite=Strict session cookie. The JWT is never held in JS-readable storage.
5. Every later same-origin request is authenticated by the cookie (and, in production, the gateway's identity headers).
6. WebSocket connections carry a single-use, 30-second ticket from `POST /api/v1/auth/ws-ticket` in the query string (`?ticket=`), never the JWT.
7. The backend's `validate_jwt()` (`app/core/auth.py`):
   - Reads the `kid` from the unverified header
   - Fetches the matching public key from the cached JWKS (fetches the JWKS endpoint at startup; refreshes on cache miss)
   - Verifies signature, audience, issuer, expiry
   - Cross-checks `org_id` against the data plane's configured org_id and rejects with 403 if they differ (defense in depth — a leaked token can't be replayed against the wrong data plane)
8. On 401, the SPA redirects back to `www.app.inaiera.com/login?next=<current>` for a fresh token.

### Calls from Workspace to the dbt and FlinkSQL runners (X-001)

Workspace never forwards the user's own credential (Authorization header or session cookie) to a runner. Each
call carries a **delegated token** minted for exactly that call:

```
Browser --(HttpOnly session / trusted gateway identity)--> Workspace
Workspace --(HS256, aud=dbtrunner | flinksql-runner, ONE scope, sub=<user>, act=workspace, <=120 s)--> runner
```

- Claims: `iss` = `workspace`, `act.sub` = `workspace`, `sub`/`org_id`/`email` = the gateway-authenticated
  user, `aud` = the one runner, `scope` = the one operation, `permissions` = the caller's Workspace permissions that
  scope needs (never role names), a fresh `jti`, `iat`/`exp`.
- Scopes: dbt `dbt.projects.read|write`, `dbt.runs.read|trigger`; Flink `flink.jobs.read|edit|deploy`
  (plus `flink.catalog.read` for the runner's Kafka catalog). A scope is not enough on its own: each runner also
  checks the token's **permissions** (DBT-001). The dbt runner needs the edit permission of the job's kind for
  `dbt.projects.write` (`workspace:dbt:edit`, `workspace:sql-jobs:edit`, `workspace:python-jobs:edit`), the run one for
  `dbt.runs.*`, either for `dbt.projects.read`; the Flink runner needs `workspace:flink:edit` / `workspace:flink:deploy`
  (reads: either, or `workspace:catalog:view`). A token without one gets 403 `FORBIDDEN` even with the right scope.
  Workspace's `tests/test_rbac_contract.py` keeps the runners' tables equal to `permissions.json`.
- Keys: `DBTRUNNER_DELEGATION_JWT_SECRET` / `FLINKSQL_DELEGATION_JWT_SECRET` here, equal to the runner's
  `DELEGATION_JWT_SECRET`. Each is its own key (never the internal service token or the DQ secret). Helm mounts
  them from Secrets (`dbtRunner.delegationJwtSecretRef`, `flinkSqlRunner.delegationJwtSecretRef`, required when the
  integration is enabled). With no key configured the call is refused with 503; there is no fallback to the user's token.
- Each runner accepts only its **own audience** (default `dbtrunner` / `flinksql-runner`), so a token minted for
  Workspace (`workspace.inaiera`) means nothing there. The dbt runner makes state-changing delegations single-use
  (jti recorded); the Flink runner has no replay store, so it caps the lifetime at 120 s.
- **External CI** does not use a browser or Workspace credential. The control plane issues it a service-principal
  token (RS256, verified against the JWKS) with `aud` = the runner (`dbtrunner` / `flinksql-runner`) and the
  `scope` of each route it calls, plus a `permissions` claim holding the permission IDs the routes need
  (e.g. `workspace:dbt:run`). The same scope names apply. The runners reject a token with no `scope`, and one
  without the needed permission.
- The Data Quality proxy still forwards the user's credential to DQ; that is outside this change.

The data plane never calls back to the control plane on the hot path. A 15-minute control plane outage doesn't break in-flight customer sessions.

---

## Execution model

Workspace itself does not run customer code. Instead it dispatches to the correct execution engine:

| File type | Executor | Lifecycle |
|---|---|---|
| `.sql` | SQL Executor (`app/executors/sql_executor.py`) | Stateless. POSTs to Data Lake House `/v1/queries/stream`, receives NDJSON of `schema` then `rows` batches then `done`/`error`, re-publishes each message as a WS envelope to the user's session channel via Redis. |
| `.ipynb` cell | Notebook Executor (`app/executors/notebook_executor.py`) | Stateful per user. Asks JupyterHub to spawn a kernel pod for the user if one isn't running. Opens the Jupyter messaging WebSocket to the kernel, forwards the cell's `execute_request`, translates kernel messages (`stream`, `display_data`, `execute_result`, `error`, `status`) into WS envelopes. The kernel pod is the per-user "personal database compute" in InAiEra's mapping. |
| `.flink.sql` | Not executed here | A streaming job definition, not a query. The editor only syntax-checks it (`POST /api/v1/flink/validate` → the runner's EXPLAIN path). Deployment happens from Git — see below. |

### FlinkSQL: author here, deploy from Git

FlinkSQL files (`*.flink.sql`, language `flinksql`) are ordinary workspace files
— same Monaco editor, same tabs, same Git flow as `.sql`. They are never sent to
the lakehouse SQL executor.

Jobs are registered from a repository, exactly like a dbt project: the runner's
Jobs API only accepts inline SQL, so `POST /api/v1/flink/jobs/from-git` shallow-
clones the repo, assembles the FlinkSQL (an explicit `sql_path`, else a
conventional entrypoint like `job.sql`, else every `.sql` file concatenated in
path order), and submits that to the runner. Provenance (`url` / `ref` / `path`
/ `sha`) round-trips through the job's `description` because the runner has no
Git columns of its own; `POST /api/v1/flink/jobs/{id}/sync` re-reads the repo and
PATCHes the job with the latest SQL.

A repository is untrusted input, so **symbolic links in a clone are never followed** (W-012, W-013), by the
workspace Git import, by pull's re-import and by the Flink assembly above. These all share ONE implementation,
`app/git/safe_read.py`: `walk_repo_files` (which entries are candidates and which are rejected, the same verdict
for every caller), `ensure_safe_path` (an explicit path such as `sql_path`) and `read_repo_file` (`lstat`, reject
symlinks and non-regular files such as FIFOs, resolve and require the path to stay inside the clone, open with
`O_NOFOLLOW`; the link's target is never opened). `tests/test_repo_file_rules.py` fails if any other module reads
files or walks directories on its own, so a new importer has to use the helper too. The import reports each rejected path in `skipped_files` (reason `symbolic link (not followed)`,
`not a regular file`, ...) and logs `git_import_unsafe_path_rejected`; a Flink job whose SQL files or `sql_path`
are symlinks is refused with a 422 naming the repo-relative path (never the target).

The WS gateway never touches the executor directly. Flow:

```
Client                Gateway              Router (Redis)        Executor
  │  HTTP POST /executions ─────────────────────────────────────────▶ │
  │  ◀────────── execution_id ────────────────────────────────────────│
  │                                                                   │
  │  WS open /ws/sessions/{user_id}                                   │
  │ ─────────▶                                                        │
  │           SUBSCRIBE ws:session:{user_id} ▶                        │
  │                                                                   │
  │                                            PUBLISH each event ◀───│
  │ ◀──── execution.schema, .rows, .succeeded ◀                       │
```

This means many editor tabs in one browser can share one WebSocket per user; executions started by any tab fan out to all of them.

---

## Local dev quickstart

```bash
# Backend + Postgres + Redis + MinIO
cd docker
docker compose up --build

# In another terminal: apply migrations
docker compose exec workspace alembic upgrade head

# Run tests (unit tests only — integration requires the running stack)
cd ..
pip install -e ".[dev]"
pytest tests/

# Frontend (optional — uses Vite dev server)
cd frontend
npm install
npm run dev
```

For JWT testing locally you need either a control plane stub that publishes a JWKS endpoint, or a script that mints JWTs with a key you've manually placed in the cache. The test suite includes the latter (`tests/conftest.py`).

---

## Production deployment

The Helm chart at `docker/helm/workspace/` is consumed by the parent `inaiera-data-plane` chart that the Admin Portal's provisioning process applies to each customer's k8s cluster.

```bash
helm install workspace ./docker/helm/workspace \
  --namespace inaiera \
  --set tenant.orgId=acme-corp \
  --set tenant.cloud=aws \
  --set tenant.region=us-east-1 \
  --set image.tag=0.1.0
```

The chart:

- Creates a `Deployment` with 2 replicas (HPA scales to 10 based on CPU)
- Wires the per-org `Ingress` host `acme-corp.app.workspace.inaiera.com`
- Creates a `ServiceAccount` annotated for cloud IAM (IRSA on EKS, Workload Identity on AKS, WIF on GKE) — the service authenticates to S3/ADLS/GCS without static keys
- Runs a `Job` (`pre-install`, `pre-upgrade`) that executes `alembic upgrade head` against the data plane Postgres before the new replicas roll
- Picks up all secrets by reference to k8s `Secret` objects that the provisioning process has populated from the cloud-native secrets manager (AWS Secrets Manager / Azure Key Vault / GCP Secret Manager)

No static credentials live in the chart or in the container.

---

## What's complete in this package

- Per-user isolated file storage via canonical key pattern
- JWT validation via JWKS with org-mismatch defense in depth (9 tests pass)
- File CRUD with optimistic concurrency (`If-Match: <content_hash>`)
- WebSocket gateway with Redis pub/sub fan-out across replicas
- SQL executor streaming from Data Lake House
- Notebook executor speaking Jupyter messaging protocol to JupyterHub
- Git provider abstraction with GitHub, GitLab, Azure DevOps implementations
- Storage adapters for S3, Azure Blob, GCS
- Initial Alembic migration for the whole schema
- Production Dockerfile (multi-stage, non-root, tini)
- Local docker-compose stack
- Helm chart with deployment, service, ingress, configmap, HPA, migration job
- Frontend contract: RTK Query API client, WS middleware, Monaco editor, three-pane shell

## What's deferred to a later phase

- Shared workspaces (`workspace_type` column already present)
- Streamlits and "structured projects" beyond a dbt project layout
- Python `.py` standalone scripts as a distinct file type
- Full notebook UI (cells, rich outputs, kernel state visualization)
- Catalog-driven SQL autocomplete (hook stubbed)
- FlinkSQL: connector/topic-aware autocomplete and in-editor deploy (jobs deploy
  from Git today — see "FlinkSQL: author here, deploy from Git")
- Production UI polish (this package ships a contract, not a polished SPA)

---

## Reference

- See `InAiEra_HLD_Rev2.docx` for the full platform context
- See `frontend/README.md` for the frontend contract detail
- See `docker/helm/workspace/values.yaml` for every per-tenant configuration knob

# InAiEra Agent Platform — Developer Guide

Audience: engineers building on or extending this codebase. For what's real vs.
stubbed, see the README first — this guide assumes you've read that.

---

## 1. Architecture at a glance

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  React + TS frontend         │  REST  │  FastAPI backend              │
│  (Vite, port 5173)           │◄──────►│  (port 8000)                   │
│  - AppContext (global state) │  JSON  │  - SQLModel ORM                │
│  - Drawer/Modal host pattern │        │  - routers/ per resource       │
└───────────────┬──────────────┘        └───────────────┬────────────────┘
                                                          │
                                    ┌─────────────────────┼─────────────────────┐
                                    │                      │                      │
                           ┌────────▼────────┐   ┌─────────▼─────────┐  ┌────────▼────────┐
                           │ Postgres 16      │   │ harness.py          │  │ Temporal server │
                           │ + pgvector       │   │ (interpreter,        │  │ + worker.py     │
                           │ (docker-compose) │   │  sync path)          │  │ (durable path)   │
                           └──────────────────┘   └──────────┬───────────┘  └──────────┬───────┘
                                                              │                          │
                                                     ┌────────▼────────┐        (shares harness's
                                                     │ ucs_client.py    │         NODE_HANDLERS via
                                                     │ model_adapter.py │         temporal_workflow.py)
                                                     │ sandbox.py       │
                                                     └──────────────────┘
```

`POST /api/agents/{id}/run` actually executes the flow graph — see §9 for
what's genuinely real here (a working interpreter, a working dual-check
entitlement gate, a working Temporal workflow for the durable path) vs.
still simplified. This is not a message-queue-free CRUD skeleton anymore;
read §9 before assuming any particular piece is or isn't real.

---

## 2. Prerequisites

- Node.js 18+, npm
- Python 3.11+
- Docker + Docker Compose (for Postgres)
- Temporal CLI (for real agent execution — see §9) — a single binary, no
  Docker needed: `temporal server start-dev`. Download from
  [temporalio/cli releases](https://github.com/temporalio/cli/releases).

---

## 3. Local setup

```bash
# 1. Database
docker compose up -d

# 2. Backend
cd backend
pip install -r requirements.txt
# Without a real ANTHROPIC_API_KEY, LLM/Agent/Classifier/Parameter-
# Extractor nodes now genuinely fail their run rather than silently
# succeeding with mock text (§0.58, GAP_REPORT) — set this explicitly
# for local development so those nodes still exercise end to end:
export ALLOW_MOCK_MODELS=true
# Real encryption at rest for EnvVar/Knowledge Base/MCP secrets
# (§0.59, GAP_REPORT) — set this to a persisted value so encrypted
# secrets stay readable across restarts; otherwise a new one is
# generated in memory each time (see §12 below for details):
export SECRETS_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uvicorn app.main:app --reload --port 8000
# Tables + seed data are created automatically on first startup, via a
# real Alembic migration (§0.69, GAP_REPORT) — see "Database
# migrations" below.

# 3. Frontend (separate terminal)
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

API docs (auto-generated, always current): `http://127.0.0.1:8000/docs`

**Resetting to a clean seeded state** (useful after manual testing mutates data):
```bash
docker compose down -v && docker compose up -d
# then restart uvicorn — init_db()/seed() re-run automatically
```

**Database migrations (§0.69, GAP_REPORT)** — real, versioned Alembic
migrations, not `SQLModel.metadata.create_all()` plus ad-hoc manual
`ALTER TABLE` statements. `init_db()` runs `alembic upgrade head`
automatically on startup (`AUTO_MIGRATE=true`, the default for local
dev — but **not** under `ENVIRONMENT=production`, where the default is
now `false`; see the settings list below). Changed a model in `app/models.py`? Don't hand-write an `ALTER
TABLE` — generate a real migration instead:
```bash
cd backend
python3 -m alembic revision --autogenerate -m "add foo column to bar"
# review the generated file in migrations/versions/, then:
python3 -m alembic upgrade head
```
See `backend/migrations/README.md` for the full real workflow,
including the production deployment pattern (a separate migration step
before rollout, so N concurrent app replicas can't race to apply the
same migration). That pattern used to require setting
`AUTO_MIGRATE=false`; it is the default in production now, and a
deployment that wants startup migrations opts in explicitly and gets a
warning saying so.

---

## 4. Project structure

```
backend/app/
  models.py           SQLModel table definitions (12 tables)
  db.py                Engine setup — Postgres connection, init_db()
  seed.py              Mock data matching the approved UI mockup exactly
  main.py              FastAPI app, CORS, router wiring, startup hook
  routers/
    agents.py           Agent CRUD + flow-graph node mutation + publish
    templates.py        Read-only template list
    runs.py              Run list/detail
    traces.py            Trace list/detail + synthesized span waterfall
    alerts.py            Alert list/detail + acknowledge/resolve (real history tracking)
    monitoring.py       Platform/per-agent metrics, evals, home stats
    integrations.py     Model providers, tools, MCP servers, data sources

frontend/src/
  api.ts                Typed fetch client — one function per endpoint
  types.ts               TS interfaces mirroring the backend models
  constants.ts            NODE_TYPES, PALETTE_GROUPS, NAV, PERSONAS — the single
                           source of truth for the builder's node palette
  context/AppContext.tsx  Global state: persona, toasts, drawer/modal overlay state
  components/              Sidebar, Topbar, Drawer/Modal shells, NodeConfigPanel
                           (all 16 node types' config forms), TraceWaterfall, ui.tsx
                           (Chip/Stat/Banner primitives)
  drawers/                  AgentProfileDrawer (9 tabs), Run/Trace/Alert detail,
                           ToolDetailDrawer (two-level drill-down)
  modals/                    NewAgent, Publish, AddMcp, ApiKeyConfig, DsConfig
  pages/                      One file per route
```

---

## 5. Database schema

12 tables, all under `public` schema. Foreign keys are enforced (a real
improvement over the earlier SQLite version, which didn't enforce them by
default).

| Table | Purpose | Notable columns |
|---|---|---|
| `agent` | Core agent record | `flow` (JSONB — the node graph, §3.6 of the design doc), `metrics` (JSONB) |
| `agentlog` | Structured logs per agent | FK → `agent.id`, `trace_id` correlates to `trace.id` |
| `run` | Run history | FK → `agent.id` |
| `trace` | Trace metadata (spans are synthesized on read, not stored) | FK → `agent.id` |
| `alert` | Alerts with full history | `history` (JSONB list of `{action, by, ts}`) |
| `role` | Read-only RBAC display | FK → `agent.id` — no write endpoint by design (§7.4/§7.5) |
| `modelprovider` | External LLM providers | `types` (JSONB list) |
| `tool` / `toolaction` | Tools + their sub-actions | `toolaction.params` (JSONB) |
| `mcpserver` | Connected MCP servers | |
| `datasource` | External data sources | `auth` discriminates OAuth vs. API-key flow |
| `template` | Agent templates | |

**JSONB columns and the mutation gotcha** — read this before touching
`agents.py`'s flow-graph endpoints. SQLAlchemy's change detection can miss
in-place mutations of JSON/JSONB columns if you mutate the same list/dict
object that's already attached to the ORM instance, then reassign it — the
old and new values end up looking identical at flush time and the `UPDATE`
gets silently skipped (this exact bug shipped and was caught during the
Postgres migration — see README for the full story). The fix used throughout
`agents.py`:
```python
nodes = copy.deepcopy(a.flow.get("nodes", []))   # never alias the existing list
# ...mutate the copy...
a.flow = {"nodes": nodes}
flag_modified(a, "flow")                          # force SQLAlchemy to persist regardless
```
Follow this pattern for any new JSONB-mutating endpoint.

---

## 6. API reference

Base URL: `http://127.0.0.1:8000`. All bodies/responses are JSON.

### Agents
| Method | Path | Notes |
|---|---|---|
| GET | `/api/agents` | List all |
| POST | `/api/agents` | Create (draft, minimal default flow) |
| GET | `/api/agents/{agent_id}` | Full agent incl. flow graph |
| POST | `/api/agents/{agent_id}/nodes` | Add a node (builder: drag from palette) |
| PATCH | `/api/agents/{agent_id}/nodes/{node_id}/position` | Move a node — 400 if node type is locked (trigger/gov/respond) |
| PATCH | `/api/agents/{agent_id}/nodes/{node_id}/config` | Merge-patch a node's config dict |
| DELETE | `/api/agents/{agent_id}/nodes/{node_id}` | Delete — 400 if locked |
| POST | `/api/agents/{agent_id}/publish` | Real entitlement-plan resolve (§7.6 contract); genuinely blocks (403) if the agent has no role bindings |
| POST | `/api/agents/{agent_id}/run` | **Real execution** — walks the flow graph via the harness (sync) or a real Temporal workflow (durable, if the flow has a `hitl` node). Body: `{"input": "...", "invoking_user": "user:..."}`. Creates real Run/Trace/AgentLog rows. See §9. |
| GET | `/api/agents/{agent_id}/logs` | Structured logs |
| GET | `/api/agents/{agent_id}/roles` | Read-only RBAC bindings |
| GET | `/api/agents/{agent_id}/traces` | Traces for this agent |

### Templates, Runs, Traces
| Method | Path | Notes |
|---|---|---|
| GET | `/api/templates` | |
| GET | `/api/runs`, `/api/runs/{run_id}` | `?status=` filter on list |
| GET | `/api/traces`, `/api/traces/{trace_id}` | `?agent_id=` filter on list |
| GET | `/api/traces/{trace_id}/spans` | STUB — synthesizes a waterfall from the trace's stored `status` |

### Alerts
| Method | Path | Notes |
|---|---|---|
| GET | `/api/alerts` | `?state=open\|resolved` |
| GET | `/api/alerts/{alert_id}` | |
| POST | `/api/alerts/{alert_id}/acknowledge` | Body: `{"by": "<persona>"}`. Appends to `history`, sets `acked=true` |
| POST | `/api/alerts/{alert_id}/resolve` | Body: `{"by": "<persona>"}`. Appends to `history`, sets `state=resolved` |

### Monitoring
| Method | Path | Notes |
|---|---|---|
| GET | `/api/monitoring/metrics` | `?agent_id=` — platform aggregate if omitted |
| GET | `/api/monitoring/evals` | `?agent_name=` |
| GET | `/api/monitoring/stats` | Home page stats |

### Integrations
| Method | Path | Notes |
|---|---|---|
| GET | `/api/integrations/model-providers` | |
| POST | `/api/integrations/model-providers/{name}/api-key` | STUB — doesn't call the real provider |
| GET | `/api/integrations/tools`, `/api/integrations/tools/{name}/actions` | |
| GET | `/api/integrations/mcp-servers` | |
| POST | `/api/integrations/mcp-servers` | STUB — no real handshake |
| GET | `/api/integrations/data-sources` | |
| POST | `/api/integrations/data-sources/{name}/oauth` | For sources with `auth=oauth` |
| POST | `/api/integrations/data-sources/{name}/api-key` | For sources with `auth=apikey` |

### Misc
`GET /api/health` — liveness check.

---

## 7. Frontend architecture patterns

### The overlay pattern (Drawer/Modal)
Every drawer and modal is opened via `AppContext`, not local component state:
```tsx
const { openDrawer, openModal } = useApp();
openDrawer('agentProfile', agent.id);   // shows AgentProfileDrawer
openModal('apiKeyConfig', provider.name);
```
`DrawerHost`/`ModalHost` (rendered once, at `App.tsx` root) switch on
`ctx.drawer`/`ctx.modal` and pass `ctx.drawerArg`/`ctx.modalArg` down. This
means any component, anywhere, can open any overlay without prop drilling —
mirrors the single-page-app pattern used in the original HTML mockup this
was built from.

### Node types are data-driven, not hardcoded per-component
`constants.ts`'s `NODE_TYPES` and `PALETTE_GROUPS` are the single source of
truth for the builder. The canvas (`Builder.tsx`), the palette, and the
config-panel switch (`NodeConfigPanel.tsx`) all read from these — adding a
node type touches exactly two places (see §8 below), never the canvas
rendering logic itself.

### API calls go through `api.ts`, never raw `fetch()` in components
Every backend call has a typed wrapper in `api.ts`. Pages/drawers/modals call
`api.xxx(...)` and handle the promise locally (`.then(setState).catch(e =>
toast(e.message))`). This keeps the base URL, headers, and error-shape
handling in one place.

---

## 8. Extending the builder: adding a new node type

This is the most common extension point. Two files, no backend changes
needed (config is a freeform JSON dict server-side).

**1. Register it in `constants.ts`:**
```ts
export const NODE_TYPES: Record<string, NodeTypeDef> = {
  // ...existing...
  mynode: { label: 'MY NODE', sub: 'does a thing', cls: 'c-blue',
            pal: 'My Node', dot: 'var(--blue)', group: 'Transform' },
};
```
Add its key to the relevant group in `PALETTE_GROUPS` (or a new group).
`cls` must be one of the CSS classes defined in `styles.css`
(`c-blue`/`c-purple`/`c-green`/`c-amber`/`c-slate`/`c-red`/`c-teal`, or
`trigger`/`gov`/`respond` for locked nodes).

**2. Add a config panel case in `NodeConfigPanel.tsx`:**
```tsx
case 'mynode':
  return (
    <div className="cfg">
      <SecHead style={{ marginTop: 0 }}>My Node</SecHead>
      <Field label="Some setting">
        <input value={cfg.some_setting || ''} onChange={(e) => set({ some_setting: e.target.value })} />
      </Field>
      {delBtn}
    </div>
  );
```
`set(patch)` merges into local state and PATCHes the backend
(`api.updateNodeConfig`) — every field you add this way is real, persisted
config, not decorative.

That's it — the node is now draggable from the palette, renders on the
canvas, and has a working config form.

---

## 9. Real agent execution

`POST /api/agents/{id}/run` genuinely executes the flow graph — this is not
a stub. What's real, and how to run it yourself:

**The interpreter (`harness.py`)** — one `NODE_HANDLERS` dispatch table, 12
of 16 node types wired to real logic: trigger, gov, retrieval, llm, tool,
code, respond, condition, http, aggregator, classifier, template.
`run_flow_sync()` is the sync-path entry point, called directly by
`execution.py` for any flow without a `hitl` node. The remaining 4
(`iteration`/`loop`, `docextractor`, `hitl`'s own logic, `subagent`/
`memory`/`agent`) are unwired for specific architectural reasons documented
in `harness.py`'s own comments — not just unfinished work; see the README's
"remaining node types" section for each one's actual blocker.

**The entitlement gate (`ucs_client.py`)** — a real dual-check against an
actual, separately-built UCS product over HTTP (`UCS_BASE_URL`, default
`127.0.0.1:8001`), not a table inside this repo. Two independent real UCS
calls per object: the agent principal's own bound groups (sourced from this
repo's `role` table, then verified against UCS), and the invoking user's
real group membership (resolved entirely by UCS itself). Both must pass.
Verified with a case where a user personally had UCS access but the agent
didn't — correctly denied — proving this isn't just checking the same table
twice. Blocks both `run` and `publish` for real (`publish` returns HTTP 403
if the check fails). See the README's "Connecting to a real UCS instance"
section for how to point this at a running UCS.

**The durable path (`temporal_workflow.py` + `worker.py`)** — a real
`AgentRunWorkflow` executed by a real Temporal server. Routing rule (exactly
per §3.6/§5): a flow containing a `hitl` node uses this path; every other
flow stays sync. Requires two extra processes beyond the API:

```bash
temporal server start-dev --ip 127.0.0.1 --port 7233 --ui-port 8233
cd backend && python -m app.worker      # separate terminal
```

Without the worker running, a durable-path `run` call will time out waiting
for an activity to be picked up — that's expected Temporal behavior (the
workflow is durably queued, just unprocessed), not a bug.

**Verifying it's real, not just self-reported:** don't trust this API's own
"success" response as sufficient proof. Cross-check with Temporal directly:
```bash
temporal workflow list --address 127.0.0.1:7233
```
This should show `Completed` workflows with real IDs
(`run-{agent_id}-{random}`) independent of anything the FastAPI process
claims.

**The model adapter (`model_adapter.py`)** — real Anthropic call if
`ANTHROPIC_API_KEY` is set; otherwise, since §0.58 (GAP_REPORT), a mock
response requires the real, explicit `ALLOW_MOCK_MODELS=true` opt-in —
without it, a missing key raises and fails the run. A real provider
error (rate limit, network, bad key) always raises too, regardless of
`ALLOW_MOCK_MODELS` — never silently fakes a real-looking response, and
never lets a real failure quietly become a run's own "successful"
output.

**Extending to more node types** — the pattern for wiring a new handler:
```python
def handle_condition(cfg: dict, ctx: dict) -> dict:
    # cfg["expression"], ctx["vars"] hold whatever prior nodes set
    ctx["vars"]["branch_taken"] = eval_safely(cfg.get("expression"), ctx["vars"])
    return ctx

NODE_HANDLERS["condition"] = handle_condition
```
Handlers taking a `session: Session` parameter (like `gov`/`tool`) get one
injected automatically by both `run_flow_sync` and the Temporal activity —
check the `if node["type"] in (...)` branch in both places if you add
another session-requiring handler type, since that check currently only
lists `gov`/`tool`.

**What's still not wired**, honestly: classifier, iteration, loop,
condition, hitl's own approval-pause logic, template, aggregator,
docextractor, http, subagent, memory, agent. A run hitting one of these logs
`{"status": "skipped", "reason": "no handler wired yet"}` for that node and
continues — it doesn't crash, and it doesn't fake success either.

**What's not the real production system**, even for the 7 wired types:
- UCS is a real implementation of the *entitlement-check contract*, not the
  actual UCS product (no Gravitino, no OpenLineage, no audit chain).
- Retrieval/Tool nodes' actual data fetch is a placeholder string — the
  entitlement check in front of them is real, but there's no pgvector
  embedding pipeline or Trino connector behind them yet.
- Code sandboxing is subprocess + resource limits, not gVisor/Firecracker
  (no container-runtime privileges or KVM host available here).

---

## 10. Adding a new page

1. Create `pages/MyPage.tsx`, following the pattern in any existing simple
   page (e.g. `Runs.tsx`): `useState` + `useEffect` fetching from `api.ts`,
   render with the shared `PageHead`/`Stat`/`Chip` primitives from `ui.tsx`.
2. Add a route in `App.tsx`'s `<Routes>`.
3. Add a nav entry in `constants.ts`'s `NAV` array, with the `roles` array
   controlling which personas see it (mirrors the mockup's role-gated nav).

---

## 11. Remaining known gaps

There is currently **no automated test suite** (no pytest, no Vitest/Jest).
All verification so far has been manual — see the companion Manual Testing
Guide for the procedure to follow before merging changes.

There is **no auth** — see §12.

**9 of 16 node types have no execution logic** yet (§9 lists them) — a run
hitting one skips it rather than crashing, but the flow's actual intended
behavior for that node doesn't happen.

**Retrieval and Tool nodes don't touch real data** — no embedding pipeline,
no warehouse connector. The entitlement check gating them is real; what
they'd fetch if allowed through isn't.

---

## 12. Deployment notes

- `DATABASE_URL` — override for non-local Postgres.
- `TEMPORAL_ADDRESS` — override for a non-local Temporal cluster (defaults
  to `127.0.0.1:7233`).
- `ANTHROPIC_API_KEY` — set to enable real LLM calls instead of the labeled mock. Since §0.63 (GAP_REPORT), this env var still takes priority, but a real key stored via the Model Providers UI (Settings → Model Providers → Anthropic) works as a genuine fallback when it's unset — see `model_adapter.py`'s own `_stored_anthropic_config()`.
- `EMBEDDING_PROVIDER` (§0.66, GAP_REPORT) — `hashing` (default, always available, honestly not semantic) or `voyage` (real semantic embeddings via a real Voyage AI API call — requires `VOYAGE_API_KEY`). See `embeddings.py`'s own module docstring for the real reason `hashing` is the default: this environment's own network policy blocks Hugging Face, so a real, locally-run pretrained model can't be downloaded here.
- `VOYAGE_API_KEY` / `VOYAGE_EMBED_MODEL` (§0.66, GAP_REPORT) — only used when `EMBEDDING_PROVIDER=voyage`; `VOYAGE_EMBED_MODEL` defaults to `voyage-3-lite` if unset.
- `BATCH_MAX_CONCURRENCY` / `AGENT_MAX_CONCURRENCY` / `LLM_MAX_CONCURRENCY` (§0.75, GAP_REPORT) — real concurrency admission control for batch execution (see `app/concurrency.py`'s own module docstring for the full real design). Defaults (8 / 8 / 4) are sensible for this reference environment's own default DB pool capacity (15 connections); a real production deployment with a larger pool and/or a higher real per-minute provider rate limit can raise these — just keep `BATCH_MAX_CONCURRENCY`/`AGENT_MAX_CONCURRENCY` comfortably under the real DB pool capacity, and `LLM_MAX_CONCURRENCY` under whatever the real Anthropic account's own concurrent-request limit actually is.
- `LLM_PRICE_TABLE_JSON` (§0.78, GAP_REPORT) — real, per-deployment override for `model_adapter.py`'s own reference per-model pricing (see `calculate_cost_usd`'s own docstring for the full real design and honesty note on why the defaults are reference rates, not a contractually-verified price list for this platform's own model names). Set to a real JSON object in the exact `{"Model Name": [input_$_per_MTok, output_$_per_MTok, cached_input_$_per_MTok]}` shape, e.g. `{"Claude Sonnet 5": [3.00, 15.00, 0.30]}`, to make `Run.cost_usd`/`monitoring.py`'s own `costAvg` reflect a real deployment's own actual, current contracted rate.
- `STORAGE_BACKEND` (§0.80, GAP_REPORT) — `local` (default, this environment's own real, working `LocalDiskStorageBackend`) or `s3` (a real, genuine `boto3`-based `S3StorageBackend` — see `app/storage.py`'s own module docstring for the full real design and this sandbox's own stated inability to reach a real cloud endpoint to demonstrate a live round trip). `LOCAL_STORAGE_DIR` (default `/var/inaiera-uploads`) sets the real local-disk root. `S3_BUCKET`/`S3_ENDPOINT_URL` configure the real S3 backend (`S3_ENDPOINT_URL` unset targets real AWS S3; set it for MinIO or another real S3-compatible endpoint); credentials resolve via `boto3`'s own standard `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/IAM-role chain.
- `UPLOAD_ALLOWED_CONTENT_TYPES` (§0.80, GAP_REPORT) — comma-separated override for `upload_security.py`'s own real content-type allowlist (see `validate_content_type`'s own docstring for the real, magic-byte-based validation this gates).
- `UPLOAD_DEFAULT_TTL_DAYS` (§0.80, GAP_REPORT) — default real retention window for a new upload (default 30; a per-request `ttl_days` query param overrides it, with `0` a real, explicit no-expiry opt-out). See `routers/uploads.py`'s own real `sweep_expired_uploads`/`POST /api/uploads/_sweep-expired` for the real cleanup this enables — a real production deployment needs a real scheduler (cron, Celery beat, a Kubernetes CronJob) outside this app's own repo to call that endpoint on an interval.
- `CLAMD_HOST`/`CLAMD_PORT` (§0.80, GAP_REPORT) — when set, `upload_security.py`'s own real `scan_for_malware` uses the real `clamd` client against a real running ClamAV daemon instead of its own default heuristic scanner (real EICAR-signature detection plus real dangerous-executable-header rejection). No real ClamAV daemon is reachable from this sandbox to demonstrate a live scan against.
- `READYZ_REQUIRE` — comma-separated list of which `/readyz` dependency checks (`postgres`, `temporal`, `worker`, `ucs`, `model_provider`, `malware_scanner`) must be healthy for the endpoint to return 200 rather than 503. When set it wins outright, including narrowing production below its default; a name that is not on that list is **refused at probe time** rather than ignored, because an ignored name is a dependency the deployment believes it is gating on and is not. Unset, the default depends on the environment: `postgres,temporal,worker,model_provider` under `ENVIRONMENT=production`, and `postgres` alone everywhere else. `ucs` is deliberately not in the production set — an install with no catalogue wired up is supported. `READYZ_PROBE_TIMEOUT_SECONDS` (default 3) bounds each individual check.
- `CORS_ALLOWED_ORIGINS` — comma-separated list of exact origins `main.py`'s CORS middleware allows. Unset keeps the `*` default outside production, which is appropriate for this local-only dev backend with no auth layer. Under `ENVIRONMENT=production` an explicit allowlist is **required**: unset, empty, or containing `*` refuses the process at import, before a single request is served. A warning would not do — a deployment that inherited `*` works perfectly and nothing surfaces the exposure until a browser-facing auth surface is put in front of it.
- `MODEL_PROVIDER_REQUIRED` — the one provider production policy has chosen, e.g. `OpenAI`. When set, `/readyz`'s `model_provider` check is satisfied only by that provider, so a leftover key for a different one cannot report a deployment ready. Unset, any provider with a runtime adapter and a configured key counts. Naming a provider with no adapter (e.g. `Amazon Bedrock`) reports `unreachable`, not `not_configured`, because no key will ever fix it.

**Knowledge Base database setup (§0.67, GAP_REPORT)** — every KB table this service creates lives inside one dedicated schema (`inaiera_kb`), never in a customer's own default schema. See `docs/kb_service_account_setup.sql` for a real, runnable script to set up a least-privilege Postgres role scoped only to that schema — the recommended way to configure a Knowledge Base's connection credentials against a real, externally-managed database, rather than pointing it at a broader admin account.
- `ALLOW_MOCK_MODELS` (§0.58, GAP_REPORT) — must be set to `true`,
  explicitly, for `call_llm` to return a mock response when
  `ANTHROPIC_API_KEY` isn't set. Without it, a missing key genuinely
  raises and fails the run, rather than silently succeeding with
  fabricated text as the answer — a real, deliberate fix for a real,
  reported bug. Set this for local development only.
- `ENVIRONMENT=production` — if this and `ALLOW_MOCK_MODELS=true` are
  both set, the app refuses to start at all (see `main.py`'s own
  startup check) — the two together mean a production deployment could
  serve synthetic answers while still reporting every run as a success,
  and this platform won't let that combination run.
- `SECRETS_ENCRYPTION_KEY` (§0.59/§0.89, GAP_REPORT) — a real, persisted
  Fernet key (`python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`) encrypting EnvVar secrets,
  Knowledge Base connection passwords/API keys, and the MCP server
  token at rest. If unset AND `ENVIRONMENT=production`, this process
  now genuinely refuses to start (§0.89) — a real, separate API
  process and Temporal worker process would otherwise each generate
  their own, independent temporary key, and a secret one encrypts
  becomes genuinely undecryptable by the other. Outside production,
  a temporary key is still generated in memory for that process only
  (with a real, loud warning logged) — secrets encrypted under it
  become unreadable after a restart. Set this to a real, persisted
  value for any real deployment; changing it makes every previously-
  encrypted secret unreadable (they don't automatically re-encrypt
  under a new key — see `secrets_crypto.py`'s own module docstring for
  the honest backward-compatible fallback this relies on, and its own
  `enc:v1:`-prefix scheme distinguishing a genuine key-management
  failure from legacy plaintext).
- The frontend's API base URL is hardcoded in `api.ts`'s `BASE` constant —
  make this an env var (`VITE_API_BASE`) before deploying anywhere the
  backend isn't at `127.0.0.1:8000`.
- No auth exists. The persona switcher is a UI convenience for demoing
  role-gated navigation, not real authentication — do not deploy this
  publicly without adding real auth in front of it. This matters more now
  than it did before real execution existed: an unauthenticated `/run`
  endpoint that can trigger real LLM calls (once a key is configured) is a
  cost/abuse surface, not just a data-integrity one.

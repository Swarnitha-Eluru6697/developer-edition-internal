# ML Studio — backend + frontend

A real, working Node.js + TypeScript backend and React + TypeScript frontend
covering every major area of the ML Studio mockup. Everything in here
actually runs, is type-checked, and was tested against a live Postgres
instance and a real browser — see "What's verified" below.

**Versioning:** this app is versioned in `backend/package.json` and
`frontend/package.json` (kept in lockstep) — see `CHANGELOG.md` for what
changed in each version. The standalone UI mockup (delivered as its own
file, `ML-Studio-mockup-vX.Y.Z.html`) is a static HTML/CSS/JS prototype,
technically separate from this app, but it **always ships with the exact
same version number** — bumped together, delivered together, whenever
either changes. If the two ever show different version numbers, that's a
bug: it means shared reference data (like the base model library) drifted
out of sync between them, the way it briefly did before v2.2.0.

## What's included

**Backend** (`backend/`) — Express + TypeScript + PostgreSQL (`pg`)
- Full domain: base model library, catalog models + version history,
  datasets (+ comments/audit/sharing), endpoints (OAuth 2.0 client
  credentials + rate limits), alert policies + active state + per-model
  overrides + notification channels, training runs (classical ML and LLM
  fine-tuning, both validated server-side), pipelines (DAG + logs +
  restart), deployments (promote/rollback), model monitoring (8-section
  payload: overview/performance/drift/predictions/explainability/
  infrastructure), and compute pool utilization
- RBAC — sourced from the real, external **Unified Catalog Service (UCS)**,
  not a local table. Genuinely catalog/model-scoped actions
  (`training-run:create`, `deployment:promote/rollback`) call UCS's real
  `POST /api/v1/authz/check` per-decision; ML-Studio-operational actions
  (pipeline restart, alert ack — things with no catalog-object
  correspondence) use a simple local tier instead, since UCS's RBAC was
  never built to answer those. Fails closed if UCS is unreachable. See
  `backend/src/ucs/README.md` for exactly what's live-verified vs. what's
  spec-correct-but-needs-a-real-Gravitino.
- Model cataloging — training-run completion registers the model and a
  version in UCS (Gravitino, catalog type `model`) and emits a real
  OpenLineage lineage event (dataset → model). Fails soft: a UCS outage
  doesn't block the local operation, but is logged loudly, not swallowed.
- Server-side validation on training-run creation mirrors (and enforces
  independently of) the wizard's own client-side rules: an LLM base model
  requires fine-tune params and a chat-shaped dataset; a classical-ML base
  model rejects LoRA/full-fine-tune fields

**Frontend** (`frontend/`) — React + TypeScript + Vite
- 13 pages: Dashboard, Model overview (8 tabs), Model catalog (search/
  filter/test), Train wizard (5 steps, domain-aware), Datasets (4 detail
  tabs), Pipelines (DAG/logs/restart), Experiments, Deploy & promote,
  Endpoints, Alerts (full CRUD), Catalog (Gravitino), Access & audit,
  Cluster monitoring
- A role switcher in the sidebar drives real RBAC — try switching to
  ml-viewer and attempting a mutation anywhere in the app
- Typed API client, shared domain types mirroring the backend

## Real execution, not just modeled state

Pipelines and Deploy & Promote both follow the same pattern: a real,
locally-verifiable default, plus a real spec-correct client for the
production system it stands in for.

- **Pipelines**: restarting one runs on `LocalWorkflowExecutor` by
  default — stages genuinely progress over real wall-clock time with real
  DB state transitions (pending → running → ok), not an instant flip. Set
  `ARGO_SERVER_URL` to submit to a real Argo Workflows server instead
  (`backend/src/execution/workflowEngine.ts`).
- **Deploy & Promote / Endpoints**: promoting a model provisions a real
  endpoint — by default, a genuine HTTP route (`POST /api/serve/:modelId/predict`)
  that actually answers requests with a deterministic (same input → same
  output) computed response, honestly labeled as a local stand-in rather
  than a reloaded trained model artifact. Set `KSERVE_API_URL` to
  provision real KServe `InferenceService`s instead
  (`backend/src/execution/servingEngine.ts`).

Both spec-correct clients (Argo, KServe) are correct against their
documented REST APIs but **unverified against a live cluster**, since none
exists in this environment — same disclosure as the UCS integration.

## Service architecture — this is not a single app anymore

As of v2.14.0, this is corrected against InAiEra's real control-plane/
data-plane deployment model — ML Studio runs *inside* a customer's own
data plane, alongside Gravitino, Trino, and the customer's own storage.
It reads and writes that storage directly (`backend/src/storage/`) — the
same way Gravitino and Trino already do — rather than through any
mediating service.

```
ml-studio-app/            (this repo) — trains, experiments, deploys,
                            monitors, and reads/writes the customer's own
                            S3/ADLS/GCS bucket directly for trained-model
                            artifacts
model-import-service/    — runs on InAiEra's OWN infrastructure (real
                            outbound internet access — never inside a
                            customer data plane, which has none).
                            Downloads real base-model weights from
                            HuggingFace, stages them at InAiEra, then
                            copies them into a SPECIFIC customer's own
                            bucket and registers them in that customer's
                            own Gravitino — a real cross-account storage
                            copy, not a network path into the data plane
unified-catalog-service/ — model identity, versions, RBAC, lineage,
                            running inside the same data plane as ML
                            Studio (a separate real deployment — see the
                            UCS section below)
```

**An earlier design put a third service ("Central Model Cache") between
ML Studio and object storage.** It's been removed — see this repo's
`CHANGELOG.md` v2.14.0 entry for the full account. ML Studio doesn't
need brokering to reach storage that's already local to it; only Model
Import Service's cross-account copy (InAiEra's environment reaching into
a customer's bucket) is a genuinely different, separately-scoped
operation, and that's the piece that stayed a separate tool.

**Known, deliberate gap**: `base_models` (the 85-entry library of what
you can train from) is still ML Studio's own local table, not sourced
from UCS/Gravitino. This should be corrected too — not done yet because
verifying it needs a live Gravitino instance, which nothing in this
environment can run. Flagged here, not left silently inconsistent.

## Running it

Requires Node 18+ and a PostgreSQL instance. To exercise real trained-
model artifact storage, set `STORAGE_BACKEND` and that backend's own
vars in `backend/.env` — without it configured, training still completes
normally, just without persisting the resulting weight file anywhere.

Requires Node 18+ and a PostgreSQL instance.

Optional but recommended: **Python 3 with numpy, scikit-learn, xgboost,
lightgbm, catboost, umap-learn, statsmodels, pmdarima, and prophet**
(`pip install numpy scikit-learn xgboost lightgbm catboost umap-learn
statsmodels pmdarima prophet`) on the same machine as the backend.
Training-run completion shells out to `python3` for real training —
`real_trainer.py` for deep-learning models (a real
`MLPClassifier`/`MLPRegressor`), `classical_trainer.py` for classical ML
(60 of the 78 non-LLM base models — dynamically imports and genuinely
fits the actual class the catalog names, e.g. real
`sklearn.ensemble.RandomForestClassifier`, real `xgboost.XGBClassifier`),
and `timeseries_trainer.py` for the remaining 6 true time-series base
models (Prophet, ARIMA, SARIMA, auto-ARIMA, Exponential Smoothing,
Holt-Winters — real fit, real forecast, real MAE/RMSE/MAPE against a
held-out window). Together these cover real training for all 78 non-LLM
base models. **Without these available, all three fail soft** — the
resulting model gets clearly-labeled derived/estimated metrics instead of
a hard error, so the app still works, just honestly labeled as not
measured rather than real telemetry.

### Automated tests

```bash
cd backend
npm test   # vitest — real unit tests for pure logic (HPO's mixed-radix
           # sampler, classical-model classpath/task mapping, the alert
           # evaluator's percentile calculation). Route/service logic
           # that needs a live Postgres + UCS is covered by the manual
           # verification steps documented in CHANGELOG.md, not by this
           # suite — a real integration-test harness remains a disclosed,
           # open gap.
```

### Database

```bash
docker compose up -d   # postgres:16 on localhost:5432, matches backend/.env.example
```

No Docker: install Postgres locally, create a `mlstudio` database and a
`postgres` user with password `postgres` (or update `DATABASE_URL` in
`backend/.env` to match your own).

### Backend

```bash
cd backend
cp .env.example .env
npm install
npm run seed   # DEV ONLY, DESTRUCTIVE: drops every table and loads demo data
npm run dev    # http://localhost:4000
```

`npm run seed` drops every table — user datasets, runs and models included —
and reloads demo data. It is for a local database only; never run it against
a real deployment.

### What loads at server start

A deployment does not need `npm run seed`. After `initSchema()`, the server
loads its reference data non-destructively — nothing is ever deleted:

- **Reference catalog** (`src/db/referenceData.ts`): the 56 `algorithm_specs`
  and 86 `base_models` are upserted (new rows inserted, changed catalog columns
  updated, `stored_weights_uri` and other runtime columns left alone), and the
  five `compute_pools` are inserted only if missing. Runs before the server
  listens, in one transaction behind a Postgres advisory lock, so replicas
  starting together take turns and the later ones write nothing. A failure is
  logged as a warning and the server starts on the catalog it already has.
  Disable with `SEED_REFERENCE_DATA=false`.
- **Sample datasets** (`src/db/sampleDatasets.ts`): `sample-fraud-transactions`
  (classification, target `is_fraudulent`), `sample-house-prices` (regression,
  target `price`) and `sample-daily-sales` (time series, `date` → `sales`, daily
  2024–2025). Generated deterministically and registered through the same code
  as `POST /api/datasets/:name/upload`, so each gets a real profiled, versioned
  snapshot in object storage. Loaded once, after the server is listening: a
  sample a user deleted is not re-created, and a user's dataset with the same
  name is never touched. Needs `STORAGE_BACKEND` (dataset files only live in
  object storage) — without it the server logs a warning and skips them.
  Disable with `SEED_SAMPLE_DATASETS=false`.

`npm run seed:reference` (or `node dist/db/seedReference.js` in the image) runs
the same non-destructive load on demand.

### Frontend

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173, proxies /api to :4000
```

Open http://localhost:5173. Try switching roles in the sidebar, then attempt
a promote/rollback/regenerate-secret/restart as `ml-viewer` — it'll be
rejected with a specific, human-readable reason.

### Local object storage and base-model weights

Datasets, trained-model artifacts and base-model weights live in S3-compatible
storage. For local work the compose file bundles **MinIO**, which keeps its data
in the `mlstudio-minio-data` volume, so nothing is lost on restart (an in-memory
mock such as moto loses every upload when it stops).

1. In `.env`, uncomment the "Local dev with the bundled MinIO" block from
   `.env.example` and pick your own `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`
   (password at least 8 characters).
2. Install the Python side: `python3 -m venv .venv && .venv/bin/pip install -r requirements-training.txt`.
3. Start everything: `scripts/start-local.sh` (Postgres, MinIO + bucket, API; safe to re-run).
   MinIO's console is at http://localhost:9011.

**Base-model weights.** Only `hf://` base models publish weights; `sklearn://` and
`oci://` entries are algorithms whose weights exist only after training.
`scripts/download_base_model_weights.py` fetches a model from Hugging Face, stores
each file under `base-models/<id>/` in the bucket, and records `stored_weights_uri`
on the base model, so the catalog shows **Weights: Yes**:

```bash
.venv/bin/python scripts/download_base_model_weights.py --list
.venv/bin/python scripts/download_base_model_weights.py qwen2.5-0.5b-instruct whisper-large-v3-turbo kokoro-82m
```

Each person runs it against their own storage; the weights come from Hugging Face,
not from a teammate. Gated models (e.g. `llama-3-8b-instruct`) need
`export HF_TOKEN=…` from an account that has accepted the model's licence.
Re-running skips files already stored. Sizes: Qwen2.5-0.5B ~1 GB, Whisper-turbo
~1.6 GB, Kokoro-82M ~0.4 GB, Llama-3-8B ~16 GB, Qwen3-Embedding-4B ~8 GB,
gpt-oss-20b ~40 GB, Qwen3-27B ~55 GB.

## Known scope limits (honest, not hidden)

- Seed data has all 85 real base models — the mockup's original 80 (73 ML,
  7 LLM) plus 5 additional deep-learning entries added on request (deep
  feedforward classifier/regressor, a CNN image classifier, a GRU sequence
  classifier, and a denoising autoencoder for representation learning) to
  round out Neural/Deep Learning coverage that the mockup's LSTM/GRU
  forecasters and anomaly-detection autoencoders only partially covered.
  The 3 catalog models (deployed instances/adapters) and 2 datasets are
  still a representative slice, since those are things a real team
  registers a handful of, not hundreds.
- "Test this model" on Model Catalog is a clearly-labeled **simulated**
  response — there's no real inference server behind it, same as the
  mockup itself never made a real model call.
- "Complete" on Experiments simulates a training run finishing with
  plausible metrics — no real training happens.
- Some visual richness from the mockup (multi-level sub-tabs, DAG modal
  styling) is flattened into simpler single-level UI here — functionally
  equivalent, visually plainer. As of v2.2.0, Dashboard, Pipelines' DAG,
  and Alerts have all been brought in line with the mockup's structure.

## Unified Catalog Service integration — live-verified, not just spec-read

As of v2.12.0, this was checked against the actual uploaded UCS codebase
(86 Python files), and further verified by standing up a real instance in
this environment — real Postgres, all 12 real Alembic migrations, the
actual FastAPI app running — and live-testing against it: a full lineage
round-trip, real `authz/check` allow/deny across multiple cases, and the
complete ML Studio create → complete → query flow. Model/catalog
registration and RBAC role/group creation depend on Gravitino (a separate
Java service that can't run in this sandbox) and are spec-correct but
unverified end-to-end — confirmed via UCS's own error traceback that this
is a missing-dependency gap, not a request-format defect. See
`backend/src/ucs/README.md` for the full breakdown of verified vs.
spec-correct-but-unverified, and how to run `scripts/bootstrapUcs.ts`
against a real deployment.

## What's verified (not just "should work")

- Backend and frontend both type-check clean (`npx tsc --noEmit`)
- Schema and seed script run cleanly against real Postgres every time,
  including after schema changes (the dev-only seed script drops and recreates)
- Every route smoke-tested against a live server via curl — every success
  path, every validation rejection, every RBAC rejection (specific,
  human-readable error messages, not generic 403s)
- Every page loaded through a real Chromium browser (Puppeteer) with zero
  JS console errors, across all 13 routes in one pass
- Real write actions clicked through the actual rendered UI, not just
  curled: promote/rollback (with a full reversible round-trip confirmed),
  pipeline restart (log preserved, DAG reset), alert override (confirmed
  it changes the live alert's displayed condition), dataset comment/share
  (confirmed the automatic audit-log cross-effect), training-run creation
  through the full 5-step wizard for both an LLM and a classical-ML run,
  experiment completion
- Two real bugs found and fixed during this work, not just written around:
  a stale-data bug in the shared `useApi` hook (switching to an unmonitored
  model left the previous model's data on screen under the error message —
  fixed by clearing `data` at the start of every fetch), and confirmed
  rollback's reversible-swap semantics are intentional, not a bug, by
  testing a full promote → rollback → rollback round-trip

## v2.36.3: Durable HPO and LLM GPU certification

HPO creation is now asynchronous. `POST /api/hpo-searches` validates the request, pins the immutable dataset snapshot, creates queued trial jobs, and returns `202`. The existing worker process claims individual HPO trials with PostgreSQL `FOR UPDATE SKIP LOCKED`; `WORKER_COMPUTE_PROFILES` routes CPU/GPU trials to the correct worker fleet. Trials heartbeat, retry after stale-worker recovery, and support `POST /api/hpo-searches/:id/cancel`. Cancellation of a running trial propagates to the trainer subprocess.

For LLM fine-tuning, `backend/Dockerfile.cuda` defines the intended CUDA worker runtime and `requirements-training.txt` includes Transformers, PEFT, Accelerate and bitsandbytes. `backend/src/training/llm_gpu_certify.py` is the release-certification harness. A full certification performs real fine-tuning, reloads the saved Hugging Face/PEFT artifact, and performs generation. GPU workers can make certification a startup gate with `REQUIRE_LLM_GPU_CERTIFICATION=true` and `LLM_GPU_CERT_CONFIG=/path/to/cert.json`. A node without CUDA cannot truthfully pass this gate.

## v2.36.9 production serving/artifact policy

For production deployments, set `SERVING_MODE=production` (or run with `NODE_ENV=production`) and configure `STORAGE_BACKEND`. Production mode is fail-closed: local serving never substitutes the deterministic development prediction when a real artifact is missing or inference fails. Durable artifact persistence is also required automatically in production; it can be enforced explicitly in any environment with `ARTIFACT_PERSISTENCE_MODE=required` or `REQUIRE_DURABLE_ARTIFACTS=true`.

Promotion and rollback now serialize per model and commit model-version aliases, deployment state, endpoint state, and the audit row in a single local PostgreSQL transaction after the serving target reports Ready. If that local commit fails after the external serving target changed, ML Studio attempts to restore the previous serving target.


## v2.37.0 data/runtime additions

### Apache Iceberg
Configure PyIceberg using `ICEBERG_CATALOG_NAME`, `ICEBERG_CATALOG_URI`, optional `ICEBERG_CATALOG_TYPE`, `ICEBERG_WAREHOUSE`, and optional `ICEBERG_CATALOG_PROPERTIES_JSON`. Register an exact physical table snapshot with `POST /api/datasets/:name/iceberg-snapshots` using `{ "table": "namespace.table", "snapshotId": 123 }`. Omitting `snapshotId` resolves the current snapshot once and records that immutable ID.

### CNN image datasets
Upload `format=image-zip`. The ZIP contract is `<class-name>/<image-file>`; the parent folder is the real label. Supported image extensions include JPEG, PNG, BMP, GIF, WebP and TIFF. `cnn-image-classifier` defaults its target contract to `label`.

### Global/TFT forecasting
For native LSTM/GRU/TFT models, supplying `entityColumn` without `entityValue` selects a global model across entities. TFT additionally accepts `predictionHorizon`, `knownRealColumns`, `unknownRealColumns`, `knownCategoricalColumns`, and `staticCategoricalColumns`.

### Reproducibility metadata
Set `GIT_COMMIT`, `GIT_REPOSITORY`, `CONTAINER_IMAGE`, `CONTAINER_IMAGE_DIGEST`, and optionally `TRAINER_VERSION` in CI/deployment. ML Studio records these together with dependency hashes, exact dataset checksum/version, seed, hyperparameters and model artifact metadata.

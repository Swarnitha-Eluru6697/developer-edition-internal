# Running the full ML Studio stack locally

Five processes. Start them in this order; each section says what breaks if you skip it.

## 1. PostgreSQL

Two databases on the local server: `mlstudio` (this app) and `ucs` (the Unified
Catalog Service).

```bash
createdb mlstudio    # if it doesn't exist
createdb ucs
```

## 2. Object storage (moto, S3-compatible) — port 5001

Trained model artifacts and dataset snapshots are written here. Without it,
training still runs but records no `weights_uri`, so nothing is servable and
promotion refuses the candidate.

```bash
pip install 'moto[server]'
moto_server -p 5001
```

`backend/.env` already points at it: `STORAGE_BACKEND=s3`,
`S3_BUCKET=ml-studio-local`, `S3_ENDPOINT=http://localhost:5001`.

## 3. Unified Catalog Service — port 8000

**Required for Train and Deploy & promote.** `training-run:create`,
`deployment:promote` and `deployment:rollback` are gated on UCS's real
`authz/check`, and that check *fails closed* — with UCS unreachable every one of
those actions is denied, with a message saying so.

```bash
cd ~/Desktop/InaiEraNew/InaiEra-unified-catalog-backend
python3 -m venv .venv && . .venv/bin/activate       # needs Python ≥ 3.10
pip install -r requirements.txt
pip install ./vendor/gateway-auth-python            # provides the `gateway_auth` module

DB_DSN="postgresql+asyncpg://$USER@localhost:5432/ucs" \
TRUST_ENVOY_HEADERS=true \
ENVOY_INTERNAL_SECRET="$UCS_GATEWAY_SECRET" \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`ENVOY_INTERNAL_SECRET` must equal `UCS_GATEWAY_SECRET` in `backend/.env`. UCS
rejects every header-authenticated request that doesn't carry the matching
`X-Inaiera-Gateway-Secret` — in a deployed data plane Envoy stamps that header,
but ML Studio calls UCS directly and so sends it itself.

### RBAC bindings

`authz/check` only ever returns "no matching grant" until the role→group
bindings exist. Normally `backend/src/scripts/bootstrapUcs.ts` creates them
through `POST /api/v1/admin/rbac/*`, which also mirrors each role into
**Gravitino** — a separate JVM service that needs Docker.

Where Gravitino can't run, `scripts/provision-ucs-rbac.py` writes the same
`rbac_bindings` rows directly, using UCS's own `privileges_for()` so the tier
semantics are UCS's and not a local reimplementation:

```bash
DB_DSN="postgresql+asyncpg://$USER@localhost:5432/ucs" python3 scripts/provision-ucs-rbac.py
```

Be clear about what this does and does not buy you: `authz/check` reads only
that table, so authorization becomes fully real. Gravitino-backed operations —
catalog registration, model/version registration under `/admin` — still do not
work, and ML Studio logs each one as a non-fatal cataloging failure.

Tier mapping, matching `bootstrapUcs.ts`:

| ML Studio role      | UCS group             | Tier     | Can            |
|---------------------|-----------------------|----------|----------------|
| `ml-viewer`         | `ml-studio-viewers`   | `select` | read           |
| `ml-engineer`       | `ml-studio-engineers` | `writer` | read, version  |
| `ml-platform-admin` | `ml-studio-admins`    | `owner`  | read, version, register |

## 4. Python training dependencies

The trainers are real Python subprocesses. `python3` (whatever is first on the
backend's PATH) needs:

```bash
python3 -m pip install numpy scikit-learn joblib pandas pyarrow xgboost lightgbm
brew install libomp    # macOS: xgboost/lightgbm won't load without it
```

Without these a run fails with `TRAINING_EXECUTION_FAILED` naming the missing
module — it does not fall back to fabricated metrics.

## 5. Backend (:4000) and frontend (:5173)

```bash
cd backend  && npx ts-node src/db/seed.ts && npm run dev
cd frontend && npm run dev
```

`seed.ts` **drops and recreates every table**, including uploaded dataset
snapshots and trained model versions. Re-run it only when the schema changes.

## Verifying the stack end to end

```bash
curl -s localhost:4000/api/ucs/status       # {"healthy":true}
curl -s localhost:4000/api/workers          # the training worker fleet
```

Then in the UI: Train wizard → pick a base model → a dataset with an uploaded
snapshot → launch. The run goes queued → running → succeeded with real measured
metrics, and Deploy & promote can promote it.

A dataset with no uploaded snapshot has no column schema, and the wizard says so
rather than guessing one from the dataset's name — upload a file on the Datasets
page first.

## Serving mode

`SERVING_MODE=development` (the default) lets prediction endpoints fall back to
a deterministic local stand-in when a model has no loadable artifact, always
labelled `dataSource: "stand-in"`. `SERVING_MODE=production` removes that
fallback: the same request returns `MODEL_INFERENCE_UNAVAILABLE` with the
reason. Neither mode ever returns an invented number as a measured one.

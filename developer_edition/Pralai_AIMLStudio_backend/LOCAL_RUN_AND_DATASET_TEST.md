# ML Studio — Local Run & Dataset End-to-End Test

This guide brings up ML Studio locally **with no Docker and no Kubernetes**, and
walks through a verified end-to-end test of the **dataset** path (upload →
real profiling → immutable versioned snapshot → object storage → RBAC).

> Scope note: this covers the **dataset** feature only, which is intentionally
> **self-contained** — it does **not** require UCS, Argo, KServe, or Ollama.
> Model training/serving need those extra services (see the platform setup
> guide); datasets do not.

---

## 1. What the dataset path actually does

`POST /api/datasets/:name/upload?format=csv|parquet|json|jsonl|xlsx|image-zip`

1. **Auth** — `roleMiddleware` reads the `X-User-Role` header. Upload is a
   local tier-1 action (`dataset:upload`), so it needs `ml-engineer`,
   `gpu-quota-approver`, or `ml-platform-admin`. `ml-viewer` is denied (403).
   **Datasets are NOT UCS-gated** (`middleware/role.ts` keeps them local-only),
   so UCS need not be running.
2. **Dataset must pre-exist** — there is no create-dataset route; the upload
   target must already be a row in `datasets` (created by `npm run seed`).
   Unknown name → 404.
3. **Raw body** — `express.raw({ type: "*/*", limit: "50mb" })` captures the
   file as a real `Buffer` (empty body → 400).
4. **Checksum** — SHA-256 over the exact bytes.
5. **Real profiling** — the buffer is written to a temp file and handed to
   `src/training/dataset_profiler.py` (spawned as `python3`, reads
   `{"filePath","targetColumn?"}` on stdin). It computes **genuine** per-column
   stats with pandas: dtype, missing count/%, cardinality, top values,
   min/max/mean/std, **Tukey-IQR outlier counts**, duplicate-row count, and
   constant/near-constant flags. (`image-zip` uses `imageDatasetProfiler`
   instead.)
6. **Schema evolution** — columns are diffed against the **previous** snapshot;
   added/removed columns are reported (informational, never blocking).
7. **Immutable snapshot** — a new row in `dataset_snapshots` at
   `version = MAX(version)+1`, stored at
   `datasets/<name>/v<N>/data.<format>` in object storage. Re-uploading never
   overwrites an earlier version (a previously-trained model's data stays put).
8. **Pointer update** — `datasets.latest_snapshot_version` / `storage_uri` /
   `column_names` advance to the new snapshot.

**Storage is required.** `getStorage()` supports only `s3` / `adls` / `gcs`
(no local-filesystem backend). With `STORAGE_BACKEND` unset the upload returns
**503**. For local testing we use **moto** (a pip-installable S3 mock) with
`STORAGE_BACKEND=s3` — no AWS account, no Docker.

---

## 2. Prerequisites

| Need | This machine | Note |
|------|--------------|------|
| Node 20 + npm | ✅ v20.16 | backend + frontend |
| PostgreSQL | ✅ 14 (Homebrew, running on 5432) | one DB: `mlstudio` |
| Python 3 **with pandas** | via venv | the profiler subprocess |
| Object storage | **moto** (pip, no Docker) | local S3 stand-in on :5001 |

> The backend spawns the literal command `python3` for profiling, so the
> Python that has **pandas** must be the `python3` on the backend process's
> PATH. The simplest way is to **activate the venv in the shell that starts the
> backend** (below).

---

## 3. One-time setup

```bash
# 3.1  Python venv with pandas + moto + boto3
python3 -m venv ~/mlvenv
source ~/mlvenv/bin/activate
pip install --upgrade pip
pip install pandas "moto[server]" boto3
# (add pyarrow to test parquet, openpyxl for xlsx, Pillow for image-zip)

# 3.2  Postgres database (Postgres already running on :5432)
createdb mlstudio
```

Create `backend/.env` (note the DB user is your local login, trust-auth, no
password — adjust `DATABASE_URL` if yours differs):

```dotenv
DATABASE_URL=postgres://<your-local-user>@localhost:5432/mlstudio
PORT=4000
UCS_BASE_URL=http://localhost:8000        # unused by datasets; harmless

# Local S3 (moto) — no Docker, no AWS
STORAGE_BACKEND=s3
S3_BUCKET=ml-studio-local
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=test
S3_SECRET_ACCESS_KEY=test
S3_ENDPOINT=http://localhost:5001
S3_FORCE_PATH_STYLE=true

REQUIRE_DURABLE_ARTIFACTS=false
```

---

## 4. Bring it up (4 terminals, or background jobs)

```bash
# Terminal A — local S3 (moto) + bucket
source ~/mlvenv/bin/activate
python -m moto.server -p 5001 &
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test python - <<'PY'
import boto3
boto3.client("s3", endpoint_url="http://localhost:5001", region_name="us-east-1").create_bucket(Bucket="ml-studio-local")
print("bucket ready")
PY

# Terminal B — backend (venv activated so its python3 has pandas)
source ~/mlvenv/bin/activate
cd backend
npm install
npm run seed            # applies schema.sql, seeds 2 datasets + base models
npm run dev             # → http://localhost:4000

# Terminal C — frontend
cd frontend
npm install
npm run dev             # → http://localhost:5173  (proxies /api → :4000)
```

`npm run seed` creates two upload targets:
`fraud-transactions-2026` (structured) and `support-tickets-corpus`
(unstructured).

Health check: `curl localhost:4000/api/health` → `{"ok":true}`.

---

## 5. End-to-end dataset test (verified)

```bash
# 5.1  A CSV that exercises the profiler (missing values, outliers, a target)
cat > fraud_v1.csv <<'CSV'
transaction_id,amount,merchant_category,hour,customer_age,is_fraud
t1001,120.50,grocery,14,34,0
t1002,9800.00,electronics,2,52,1
t1003,45.20,grocery,9,,0
t1006,250000.00,electronics,3,60,1
t1010,410.00,travel,17,,0
CSV

# 5.2  Upload v1 (ml-engineer)
curl -s -X POST "http://localhost:4000/api/datasets/fraud-transactions-2026/upload?format=csv" \
  -H "X-User-Role: ml-engineer" -H "Content-Type: text/csv" \
  --data-binary @fraud_v1.csv | python3 -m json.tool

# 5.3  Inspect the snapshot + real profiling
curl -s "http://localhost:4000/api/datasets/fraud-transactions-2026/snapshots/1" \
  -H "X-User-Role: ml-engineer" | python3 -m json.tool

# 5.4  Upload v2 with a changed schema → schemaEvolution is reported
# 5.5  List snapshots (desc) → v2, v1 with distinct checksums
curl -s "http://localhost:4000/api/datasets/fraud-transactions-2026/snapshots" \
  -H "X-User-Role: ml-engineer" | python3 -m json.tool

# 5.6  RBAC — ml-viewer is denied
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "http://localhost:4000/api/datasets/fraud-transactions-2026/upload?format=csv" \
  -H "X-User-Role: ml-viewer" --data-binary @fraud_v1.csv   # → 403
```

### Verified results

| Check | Result |
|-------|--------|
| Upload → immutable snapshot | ✅ v1 (12 rows) then v2 (5 rows), `version = MAX+1` |
| Real per-column profiling | ✅ `amount` outlierCount 3 (IQR); `customer_age` missing 16.67% (2/12); `is_fraud` mean 0.25 |
| SHA-256 checksum per snapshot | ✅ distinct per version |
| Schema evolution | ✅ `+device_type / −customer_age` detected |
| Object storage persistence | ✅ `v1/data.csv` (414 B) + `v2/data.csv` (225 B) present in the moto bucket |
| Dataset pointer advances | ✅ `latest_snapshot_version=2`, `storage_uri`/`column_names` updated |
| RBAC | ✅ `ml-viewer` → 403, `ml-engineer` → 200 |
| Error handling | ✅ unknown dataset → 404, empty body → 400 |

### UI

Open `http://localhost:5173` → **Datasets**. The role switcher (top of the app)
sends `X-User-Role`; switch to `ml-engineer` to upload, view Snapshots &
Quality, comments, and the audit tab.

---

## 6. Known gap found during testing

**Uploads are not written to `dataset_audit_log`.** `logAudit()` is called for
`commented` / `shared` / `revoked` only, not for `uploaded`, so there is no
audit trail of *who uploaded which snapshot version*. The snapshot row does
record `uploaded_by`, but the dataset **audit log** does not. Worth closing
when the code moves into `Pralai_AIMLStudio_backend`.

---

## 7. Teardown

```bash
# stop the three background processes (backend, frontend, moto)
# then, to reset data:
dropdb mlstudio && createdb mlstudio        # fresh DB
# moto S3 is in-memory — it empties when the moto.server process stops
```

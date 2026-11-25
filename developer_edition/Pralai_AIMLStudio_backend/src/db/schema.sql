CREATE TABLE IF NOT EXISTS base_models (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL CHECK (domain IN ('ml','llm')),
  title TEXT NOT NULL,
  task TEXT NOT NULL,
  description TEXT NOT NULL,
  framework TEXT NOT NULL,
  license TEXT NOT NULL,
  uri TEXT NOT NULL,
  gpu BOOLEAN NOT NULL,
  compute_profile TEXT NOT NULL,
  long_description TEXT NOT NULL,
  -- Where this model's real weights actually live in the customer's
  -- configured object storage (S3/ADLS/GCS), once downloaded there —
  -- distinct from `uri` (the SOURCE reference: hf://, sklearn://, oci://).
  -- NULL until scripts/downloadBaseModelWeights.ts has actually fetched
  -- and uploaded it. Only ever gets populated for domain='llm' entries —
  -- sklearn:// and oci:// entries have no pretrained weights to fetch
  -- (see that script's own comments for why).
  stored_weights_uri TEXT,
  weights_downloaded_at TIMESTAMPTZ,
  -- Coarse grouping used by the catalog's and the wizard's category filters.
  -- NULL for LLM entries, which are grouped by `task` instead — an LLM has no
  -- meaningful "classification vs regression" category.
  category TEXT,
  -- Which trainer implementation this entry dispatches to, and (where several
  -- entries share one trainer) which variant of it.
  algorithm_key TEXT,
  algorithm_variant TEXT,
  -- Published model-card facts for LLM entries: parameter count and context
  -- window, verbatim as the model card states them. NULL where the model
  -- publishes no figure — deliberately not estimated.
  params TEXT,
  context_window TEXT,
  -- Sibling base models worth considering instead of this one.
  alternatives TEXT[] NOT NULL DEFAULT '{}',
  -- The hyperparameters training actually starts from, and the tunable surface
  -- (type, default, bounds/values) that HPO search spaces and wizard field
  -- validation are built from. NULL where this entry's trainer defines no
  -- documented default set: the UI then says none is specified rather than
  -- showing an invented block.
  default_hp JSONB,
  supported_hp JSONB,
  -- Curated presentation order, NOT alphabetical. The library is deliberately
  -- ordered by what you'd most likely reach for — xgboost-classifier first,
  -- then the other common tabular learners, then the LLM entries as a group.
  -- Sorting by id instead scatters that (adaboost-* first, LLMs interleaved
  -- among the sklearn entries) and buries the recommended default on a later
  -- row of the Train wizard's grid.
  sort_order INTEGER NOT NULL DEFAULT 0
);

-- The hyperparameter surface of each trainer, keyed by the algorithm_key a
-- base_models row carries. Separate from base_models because many library
-- entries share one trainer: ridge-regression, lasso-regression and
-- elasticnet-regression are three catalog entries over one linear_family
-- spec, distinguished by their algorithm_variant.
CREATE TABLE IF NOT EXISTS algorithm_specs (
  algorithm_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  applies_to_text TEXT,
  -- True for a family whose members are genuinely different estimators sharing
  -- one trainer; fields then carry `showIf` listing the variants they apply to.
  has_applies_to BOOLEAN NOT NULL DEFAULT FALSE,
  default_applies_to TEXT,
  applies_to_tooltip TEXT,
  applies_to_options JSONB,
  -- [{name,label,type,default,range?,required?,tooltip,showIf?,options?}]
  fields JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_models (
  id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  domain TEXT NOT NULL CHECK (domain IN ('ml','llm')),
  -- 'trained': classical-ML/DL real training output (the vast majority).
  -- 'fine-tuned': LLM full fine-tune. 'adapter': LLM LoRA/PEFT fine-tune.
  -- Previously every registered model got 'base' unconditionally — which
  -- was never even accurate for the models it originally meant to cover
  -- (a trained RandomForestClassifier isn't a "base" model in any real
  -- sense), and gave fine-tuned/adapter LLMs no distinct kind at all.
  kind TEXT NOT NULL CHECK (kind IN ('trained','fine-tuned','adapter')),
  framework TEXT NOT NULL,
  base_ref TEXT,
  updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
  id SERIAL PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES catalog_models(id) ON DELETE CASCADE,
  v INTEGER NOT NULL,
  alias TEXT NOT NULL CHECK (alias IN ('champion','challenger','staging','archived','training')),
  note TEXT NOT NULL,
  -- Real artifact location for THIS version specifically — previously
  -- only model_monitoring (keyed by model_id, one row per model) held a
  -- weights URI, so retraining silently made the previous version's real
  -- artifact unrecoverable the moment a new one was registered. NULL
  -- means no real weights exist for this version (STORAGE_BACKEND wasn't
  -- configured at training time, or this base model's training isn't
  -- covered by real training yet).
  weights_uri TEXT,
  -- What format the real artifact is actually in, for serving to use
  -- (KServe's modelFormat) — 'sklearn' for both real_trainer.py and
  -- classical_trainer.py output today (both save real sklearn-family
  -- joblib objects, including the DL path's MLP — it's a real sklearn
  -- object regardless of the catalog's declared pytorch/tensorflow
  -- framework). NULL alongside weights_uri when no real artifact exists.
  artifact_format TEXT,
  -- The framework/architecture that GENUINELY trained this version —
  -- distinct from catalog_models.framework, which is the DECLARED
  -- framework from the base model's own catalog entry (e.g. "PyTorch")
  -- and is never corrected by real training. For a deep-learning base
  -- model, real training today is a real, honest scikit-learn
  -- MLPClassifier/MLPRegressor stand-in (see real_trainer.py) — real
  -- native PyTorch training is available for supported catalog architectures; unsupported architecture/data contracts fail closed
  -- in this environment (network policy blocks the CPU-only wheel
  -- index; the only reachable PyPI build is CUDA-linked and doesn't fit
  -- this environment's disk quota alongside everything else already
  -- installed). Previously this divergence was disclosed only as a
  -- source-code comment and a free-text `measuredBy` string buried
  -- inside the detailed monitoring JSON — real, but not a real,
  -- queryable field the catalog itself could ever surface. NULL for
  -- LLM fine-tuning, which has no real trainer yet at all (simulated
  -- metrics, honestly labeled that way already).
  actual_framework TEXT,
  actual_architecture TEXT,
  -- One real version number per model, not just "highest so far" —
  -- closes a genuine race: two concurrent completions for the same
  -- model previously both computed the same MAX(v)+1 before either
  -- committed, silently colliding. Enforced at the database level, not
  -- just by application-side sequencing.
  UNIQUE (model_id, v)
);

CREATE TABLE IF NOT EXISTS datasets (
  name TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('upload','iceberg')),
  structure_type TEXT NOT NULL CHECK (structure_type IN ('structured','unstructured')),
  size TEXT NOT NULL,
  rows TEXT NOT NULL,
  owner TEXT NOT NULL,
  tags JSONB NOT NULL,
  detail TEXT NOT NULL,
  -- Convenience pointer to the latest real snapshot (dataset_snapshots
  -- below) — display/summary only. The actual immutable data lives in
  -- dataset_snapshots; this is never written to directly by an upload.
  storage_uri TEXT,
  column_names JSONB,
  latest_snapshot_version INTEGER
);

-- Real, immutable, versioned physical snapshots — previously an upload
-- OVERWROTE datasets.storage_uri directly: re-uploading the same
-- dataset name silently replaced the file a previously-trained model
-- had used, with no way to ever get back to the exact data that
-- produced it. A new upload is now a new row, never an edit to an
-- existing one — the same real pattern already used for model_versions
-- and pipeline_definitions. Includes a real profile (column stats,
-- missing %, distributions) computed once at upload time from the
-- actual file content, not estimated later from a description.
CREATE TABLE IF NOT EXISTS dataset_snapshots (
  id UUID PRIMARY KEY,
  dataset_name TEXT NOT NULL REFERENCES datasets(name) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  storage_uri TEXT NOT NULL,
  -- Real format this snapshot was actually uploaded as (csv, parquet,
  -- json, jsonl, xlsx) — previously not tracked at all, meaning a
  -- downloaded snapshot always got saved to a file literally named
  -- data.csv regardless of what it really was. A Parquet snapshot
  -- resolved that way would have its real binary content silently
  -- misdetected as CSV during training — a real correctness gap, not
  -- just a missing convenience field. Defaults to csv for the snapshots
  -- created before this column existed.
  format TEXT NOT NULL DEFAULT 'csv',
  row_count INTEGER NOT NULL,
  column_names JSONB NOT NULL,
  -- SHA-256 of the real uploaded content — a real, verifiable identity
  -- for this exact snapshot, independent of the storage backend.
  checksum TEXT NOT NULL,
  -- Real per-column statistics computed from the actual file: dtype,
  -- missing count/%, and for numeric columns mean/std/min/max/quartiles,
  -- for non-numeric columns cardinality and top value counts. See
  -- dataset_profiler.py — computed once here, not re-derived on every
  -- read.
  profile JSONB,
  uploaded_by TEXT NOT NULL,
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (dataset_name, version)
);

CREATE TABLE IF NOT EXISTS endpoints (
  model_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  status TEXT NOT NULL,
  token_endpoint TEXT NOT NULL,
  client_id TEXT NOT NULL,
  client_secret_masked TEXT NOT NULL,
  -- A real salted hash (Node's built-in scrypt — no new dependency,
  -- the same KDF Node's own docs recommend for exactly this use case),
  -- not the raw secret. Previously the actual plaintext client_secret
  -- was stored directly in Postgres — real, working authentication, but
  -- a genuine credential-storage gap: anyone with read access to this
  -- table could use every endpoint's secret directly, no cracking
  -- required. Format: "salt:hash", both hex. The plaintext is generated
  -- and returned to the caller exactly once (creation or regenerate-
  -- secret) and never persisted anywhere.
  client_secret_hash TEXT NOT NULL DEFAULT '',
  rpm INTEGER,
  burst INTEGER,
  concurrent INTEGER
);

CREATE TABLE IF NOT EXISTS notification_channels (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('slack','teams')),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  -- The real, usable webhook URL — previously there was no field for
  -- this at all; only webhook_masked existed, which the client
  -- populated DIRECTLY with an already-redacted-looking string. That
  -- was never a real, dispatchable URL — there was no way to actually
  -- fire a webhook through this API at all. webhook_masked is now
  -- computed server-side FROM this real value for display, the same
  -- pattern already fixed for endpoint client secrets.
  webhook_url TEXT NOT NULL DEFAULT '',
  webhook_masked TEXT NOT NULL DEFAULT '',
  channel_dest TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alert_policies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  label TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
  operator TEXT NOT NULL,
  unit TEXT NOT NULL,
  window_duration TEXT NOT NULL,
  action TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  applies_to JSONB NOT NULL,
  default_threshold DOUBLE PRECISION NOT NULL,
  channel_id TEXT REFERENCES notification_channels(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
  model_id TEXT NOT NULL,
  policy_id TEXT NOT NULL REFERENCES alert_policies(id) ON DELETE CASCADE,
  windows_exceeded INTEGER NOT NULL DEFAULT 0,
  current_value TEXT NOT NULL DEFAULT '—',
  ack_state TEXT NOT NULL DEFAULT 'none',
  override_threshold DOUBLE PRECISION,
  PRIMARY KEY (model_id, policy_id)
);

-- Append-only record of resolved alerts. alert_state above holds only ONE
-- current row per (model, policy) — resolving it and having the same alert
-- fire again later would overwrite any "history" kept there, so History
-- needs its own table, not a reinterpretation of current state.
CREATE TABLE IF NOT EXISTS alert_history (
  id SERIAL PRIMARY KEY,
  model_id TEXT NOT NULL,
  policy_id TEXT NOT NULL REFERENCES alert_policies(id) ON DELETE CASCADE,
  final_value TEXT NOT NULL,
  windows_at_resolution INTEGER NOT NULL,
  resolved_by TEXT NOT NULL,
  resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Groups training runs (and HPO searches) toward a shared objective —
-- the missing entity Experiments was just a flat run table without.
CREATE TABLE IF NOT EXISTS experiments (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  objective_metric TEXT NOT NULL,
  objective_direction TEXT NOT NULL CHECK (objective_direction IN ('maximize','minimize')),
  owner TEXT NOT NULL,
  team TEXT,
  tags JSONB,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS training_runs (
  id UUID PRIMARY KEY,
  base_model_id TEXT NOT NULL,
  -- What the trained model gets registered as in catalog_models on
  -- completion — distinct from base_model_id (the library entry being
  -- trained FROM). One base model can produce many differently-named
  -- trained models, same as fraud-detector/fraud-detector-dl both come
  -- from different base models but neither IS its base model.
  model_name TEXT NOT NULL,
  dataset_name TEXT NOT NULL,
  compute_profile TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  created_at TIMESTAMPTZ NOT NULL,
  target_column TEXT,
  features JSONB,
  split_train INTEGER,
  split_val INTEGER,
  split_test INTEGER,
  fine_tune_method TEXT,
  lora_params JSONB,
  full_params JSONB,
  -- Real hyperparameter overrides for classical-ML/deep-learning training
  -- — previously nothing carried a trial's winning hyperparameters from
  -- an HPO search through to the model actually registered: promote-best
  -- re-triggered real training from scratch with only the DEFAULT
  -- hyperparameters, silently discarding what HPO had found. NULL means
  -- "use this training path's own defaults."
  hyperparams JSONB,
  -- Per-column preprocessing chosen in the Train wizard, {column: strategy}.
  -- NULL means every column uses its type-appropriate default, which is what
  -- every run did before this was configurable.
  preprocessing JSONB,
  -- Non-blocking dataset findings from the quality gate (leakage, cardinality,
  -- imbalance). Persisted because the background worker is the normal
  -- completion path and discards the in-memory result, which left no way to
  -- ever see why a run's metrics looked too good.
  quality_gate_warnings JSONB,
  -- Optional — a run doesn't have to belong to an experiment (matches how
  -- the Train wizard already worked before this), but can be grouped into
  -- one for comparison.
  experiment_id UUID REFERENCES experiments(id) ON DELETE SET NULL,
  -- Real async worker lifecycle — previously "queued -> succeeded" only
  -- ever happened via a client-triggered POST .../complete within a
  -- single HTTP request; there was no real claim, heartbeat, timeout, or
  -- retry semantics at all. A real background worker (see
  -- services/trainingWorker.ts) now claims queued runs itself.
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  -- What the caller explicitly asked for, if anything — e.g. "pin this
  -- run to fraud-transactions-2026 v12" for reproducibility (re-running
  -- an experiment, or an audit needing the exact prior data). NULL means
  -- "use whatever the latest version is at train time," the existing
  -- default behavior, unchanged. Distinct from dataset_snapshot_version
  -- below: this is the INPUT (what was requested, before resolution);
  -- that one is the OUTPUT (what was actually resolved and used) — a
  -- request for a version that got deleted, or a request left NULL,
  -- both still produce a real resolved version once training runs.
  requested_dataset_version INTEGER,
  -- The real time-series dataset contract this run declared — timestamp
  -- column, value column, optional entity key + value for a multi-
  -- series dataset, optional declared frequency/timezone, and the real
  -- missing-period policy. NULL for every non-time-series run. Required
  -- when a real dataset is being used for one of the 6 true time-series
  -- base models — a generic targetColumn alone doesn't establish enough
  -- of the real contract a forecasting dataset actually needs.
  time_series_config JSONB,
  -- The real dataset contract LLM fine-tuning needs — which real column
  -- (or real prompt/completion column pair) holds the actual training
  -- text, and the real max sequence length to tokenize to. NULL for
  -- every non-LLM run. Unlike time_series_config, there's no legitimate
  -- "omit it and fall back to synthetic" case for this one — real
  -- instruction-tuning text can't be honestly fabricated, so this is
  -- required whenever fine_tune_method is set.
  llm_fine_tune_config JSONB,
  -- Which exact immutable dataset_snapshots version this run actually
  -- trained on — recorded at completion time, once real training has
  -- resolved and used a specific snapshot. NULL when no real snapshot
  -- existed yet (synthetic data was used instead) or for LLM fine-tunes
  -- (unstructured corpora aren't snapshotted the same way). This is what
  -- makes a trained model's data genuinely reproducible: not "the
  -- dataset named X as it happens to look today," but this exact,
  -- immutable, checksummed file.
  dataset_snapshot_version INTEGER,
  error TEXT
);

-- One HPO search = a declared search space over a base model + dataset,
-- producing N trials. Each trial is either a REAL training run (deep
-- learning base models — real_trainer.py actually trained with THAT
-- trial's hyperparameters) or an honestly-labeled simulated one
-- (classical ML / LLM base models, which real_trainer.py doesn't cover).
CREATE TABLE IF NOT EXISTS hpo_searches (
  id UUID PRIMARY KEY,
  experiment_id UUID REFERENCES experiments(id) ON DELETE CASCADE,
  base_model_id TEXT NOT NULL,
  dataset_name TEXT NOT NULL,
  search_space JSONB NOT NULL,
  num_trials INTEGER NOT NULL,
  objective_metric TEXT NOT NULL,
  objective_direction TEXT NOT NULL CHECK (objective_direction IN ('maximize','minimize')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
  best_trial_id UUID,
  created_at TIMESTAMPTZ NOT NULL,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  requested_dataset_version INTEGER,
  dataset_snapshot_version INTEGER,
  target_column TEXT,
  time_series_config JSONB,
  fine_tune_method TEXT,
  llm_fine_tune_config JSONB,
  compute_profile TEXT NOT NULL DEFAULT 'cpu-small',
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  error TEXT
);

CREATE TABLE IF NOT EXISTS hpo_trials (
  id UUID PRIMARY KEY,
  search_id UUID NOT NULL REFERENCES hpo_searches(id) ON DELETE CASCADE,
  trial_number INTEGER NOT NULL,
  hyperparams JSONB NOT NULL,
  seed INTEGER NOT NULL DEFAULT 0,
  metric_value DOUBLE PRECISION,
  data_source TEXT NOT NULL CHECK (data_source IN ('measured','simulated')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  weights_uri TEXT,
  artifact_format TEXT,
  compute_profile TEXT NOT NULL DEFAULT 'cpu-small',
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE
);

-- v2.36.3 upgrade-safe HPO queue migration for existing databases.
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS requested_dataset_version INTEGER;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS dataset_snapshot_version INTEGER;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS target_column TEXT;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS time_series_config JSONB;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS fine_tune_method TEXT;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS llm_fine_tune_config JSONB;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS compute_profile TEXT NOT NULL DEFAULT 'cpu-small';
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS seed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS compute_profile TEXT NOT NULL DEFAULT 'cpu-small';
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hpo_searches DROP CONSTRAINT IF EXISTS hpo_searches_status_check;
ALTER TABLE hpo_searches ADD CONSTRAINT hpo_searches_status_check CHECK (status IN ('queued','running','completed','failed','cancelled'));
ALTER TABLE hpo_trials DROP CONSTRAINT IF EXISTS hpo_trials_status_check;
ALTER TABLE hpo_trials ADD CONSTRAINT hpo_trials_status_check CHECK (status IN ('queued','running','completed','failed','cancelled'));

CREATE INDEX IF NOT EXISTS idx_hpo_trials_queue ON hpo_trials(status, compute_profile, created_at);
CREATE INDEX IF NOT EXISTS idx_hpo_trials_search ON hpo_trials(search_id, trial_number);

-- Immutable, versioned pipeline definitions — what a pipeline actually
-- IS (its ordered stages), separate from any one execution of it. A new
-- version is a new row, never an edit to an existing one — the same
-- "immutable, reusable definition" concept model_versions already gives
-- trained models. Previously `pipelines` conflated definition and run
-- into one row (a status, a started_at — genuinely a RUN's shape, not a
-- reusable definition's), with no versioning at all.
CREATE TABLE IF NOT EXISTS pipeline_definitions (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  -- Ordered stage titles this definition compiles into real
  -- pipeline_stages rows (and a real Argo Workflow DAG, when Argo is
  -- configured) every time it's run.
  stages JSONB NOT NULL,
  compute_profile TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS pipelines (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  compute_profile TEXT NOT NULL,
  meta TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  -- Which real definition (and which real version of it) this run
  -- actually executed — NULL for runs that predate pipeline_definitions
  -- existing, or that were started without one. A real run should be
  -- traceable back to the exact real definition it ran, not just to a
  -- title string.
  definition_id UUID REFERENCES pipeline_definitions(id) ON DELETE SET NULL,
  definition_version INTEGER
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
  id SERIAL PRIMARY KEY,
  pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  stage_order INTEGER NOT NULL,
  title TEXT NOT NULL,
  -- What real work this stage actually does when it runs — previously
  -- every stage was an opaque title the executor just slept through
  -- (locally) or ran as an identical generic "echo running stage: X"
  -- container (Argo). A real pipeline compiler needs to know a stage IS
  -- a dataset-validation step, or a training step, etc., to dispatch
  -- real work instead of a placeholder. 'generic' preserves the
  -- previous placeholder behavior for a stage with no real work defined
  -- for it yet — an honest no-op, not a silently-skipped real step.
  stage_type TEXT NOT NULL DEFAULT 'generic'
    CHECK (stage_type IN ('dataset-validation','preprocessing','training','hpo','evaluation','register','deploy','generic')),
  -- Real, stage-type-specific parameters this exact run needs to do
  -- real work — e.g. {datasetName, targetColumn, baseModelId} for a
  -- training stage, {modelName, deployTarget} for a deploy stage.
  -- Copied from the real pipeline_definitions stage spec at run time,
  -- same "definition is immutable, a run is a real snapshot of it"
  -- pattern already used for dataset_snapshot_version.
  stage_params JSONB NOT NULL DEFAULT '{}',
  -- Real per-stage output — what a completed stage actually produced
  -- (e.g. training's real modelName/modelVersion/metrics) that a LATER
  -- real stage needs as input. Previously this only ever lived in an
  -- in-memory JS object inside LocalWorkflowExecutor.runStages(), which
  -- genuinely cannot work once a real Argo pipeline runs each stage as
  -- a SEPARATE container/process with no shared memory — this is what
  -- makes cross-stage context real and queryable across process
  -- boundaries: a later stage (in-process or a separate real Argo CLI
  -- invocation) builds its context by querying every earlier stage's
  -- real stage_output for the same real pipeline_id, in real
  -- stage_order.
  stage_output JSONB NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK (state IN ('pending','running','ok','failed')),
  sub TEXT NOT NULL
);

-- v2.36.5 upgrade-safe migration for existing databases.
ALTER TABLE pipeline_stages ADD COLUMN IF NOT EXISTS stage_output JSONB NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS pipeline_logs (
  id SERIAL PRIMARY KEY,
  pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  t TEXT NOT NULL,
  src TEXT NOT NULL,
  level TEXT NOT NULL,
  msg TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
  model_id TEXT PRIMARY KEY REFERENCES catalog_models(id) ON DELETE CASCADE,
  current_version INTEGER NOT NULL,
  previous_version INTEGER,
  health_status TEXT NOT NULL DEFAULT 'healthy' CHECK (health_status IN ('healthy','degraded')),
  runtime TEXT NOT NULL,
  replicas TEXT NOT NULL
);
-- Who last promoted (or rolled back) the live version: the gateway principal's
-- name (req.actor). The Agents app lists "ML Studio models" for its current
-- user from this. NULL for deployments made before this column existed.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS deployed_by TEXT;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS deployed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS model_monitoring (
  model_id TEXT PRIMARY KEY REFERENCES catalog_models(id) ON DELETE CASCADE,
  model_type TEXT NOT NULL CHECK (model_type IN ('classification','regression','generation','clustering','anomaly_detection','dimensionality_reduction')),
  health_score INTEGER NOT NULL,
  overview JSONB NOT NULL,
  performance JSONB NOT NULL,
  drift JSONB NOT NULL,
  predictions JSONB NOT NULL,
  explainability JSONB,
  infrastructure JSONB NOT NULL,
  -- LLM-only sections (nullable — a classification/regression model has
  -- none of these; a generation model has all of them). Traditional ML's
  -- explainability/predictions above don't map cleanly onto an LLM either,
  -- so generation-type rows leave explainability null and repurpose
  -- predictions for sample-interaction traces instead of class predictions.
  hallucination JSONB,
  safety JSONB,
  cost JSONB,
  fine_tuning JSONB,
  -- Deep-learning-from-scratch training metrics — distinct from fine_tuning
  -- (which implies starting from a pretrained base). A classification/
  -- regression model trained as a deep net (framework = pytorch) gets this
  -- populated; a classical ML model (xgboost etc) or an LLM (which uses
  -- fine_tuning instead) does not.
  training_metrics JSONB,
  -- Production input-quality signals for a deployed deep learning model —
  -- shape/tensor violations, NaN/Inf, missing/invalid, out-of-range. A
  -- separate category from training_metrics (which is about the TRAINING
  -- run) — this is about live production inputs the deployed model is
  -- actually seeing.
  dl_input_quality JSONB,
  -- Real classical-ML / time-series training result — the fit/score
  -- output from classical_trainer.py (real accuracy/f1, mse/r2,
  -- silhouette, detection rate, or trustworthiness, depending on task)
  -- or timeseries_trainer.py (real mae/rmse/mape from a real forecast
  -- against a held-out window) for the 6 Prophet/ARIMA-family models.
  -- Distinct from training_metrics (deep-learning-shaped: loss curves,
  -- epochs) since neither has a training loop to show curves for — this
  -- is a single real measurement, not a series. NULL for LLMs and for
  -- deep learning models (which use training_metrics instead).
  classical_training_result JSONB
);

CREATE TABLE IF NOT EXISTS dataset_comments (
  id SERIAL PRIMARY KEY,
  dataset_name TEXT NOT NULL REFERENCES datasets(name) ON DELETE CASCADE,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dataset_audit_log (
  id SERIAL PRIMARY KEY,
  dataset_name TEXT NOT NULL REFERENCES datasets(name) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dataset_access (
  id SERIAL PRIMARY KEY,
  dataset_name TEXT NOT NULL REFERENCES datasets(name) ON DELETE CASCADE,
  principal TEXT NOT NULL,
  access_level TEXT NOT NULL CHECK (access_level IN ('viewer','editor'))
);

-- Experiments: training_runs gets real (simulated) eval metrics once it
-- finishes, so runs can be compared against each other.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS metrics JSONB;

-- Access & Audit: one shared, append-only log for every RBAC-gated mutation
-- across the app (promote, rollback, regenerate secret, restart pipeline,
-- alert policy changes, dataset sharing) — not just datasets' own log.
CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cluster monitoring: current utilization per compute profile/pool.
CREATE TABLE IF NOT EXISTS compute_pools (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('cpu','gpu')),
  capacity INTEGER NOT NULL,
  in_use INTEGER NOT NULL,
  queued_jobs INTEGER NOT NULL DEFAULT 0
);

-- Gravitino catalog table removed — the real Unified Catalog Service is
-- now the single source of truth for the catalog view this used to
-- locally fake (see CHANGELOG.md for the UCS integration).

-- v2.36.7: durable ground-truth feedback keyed by prediction id.
CREATE TABLE IF NOT EXISTS prediction_feedback (
  model_id TEXT NOT NULL REFERENCES catalog_models(id) ON DELETE CASCADE,
  prediction_id UUID NOT NULL,
  ground_truth JSONB NOT NULL,
  prediction JSONB,
  correct BOOLEAN,
  error DOUBLE PRECISION,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(model_id,prediction_id)
);
CREATE INDEX IF NOT EXISTS idx_prediction_feedback_model_created ON prediction_feedback(model_id,created_at DESC);

-- Raw snapshots from Prometheus-compatible KServe/vLLM/DCGM endpoints.
CREATE TABLE IF NOT EXISTS monitoring_telemetry_snapshots (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  payload JSONB NOT NULL,
  collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_monitoring_telemetry_collected ON monitoring_telemetry_snapshots(collected_at DESC);

-- v2.37.0 physical-source + reproducibility lineage hardening.
ALTER TABLE dataset_snapshots ADD COLUMN IF NOT EXISTS external_source JSONB;
-- Size of this snapshot's stored object. Previously only the LATEST upload's
-- byte count survived, written straight onto datasets.size — so a dataset whose
-- newest file was small reported that small size for the whole thing ("0.00 MB"
-- next to "Files 2"). Keeping it per snapshot is what makes the total
-- computable.
ALTER TABLE dataset_snapshots ADD COLUMN IF NOT EXISTS size_bytes BIGINT;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS dataset_checksum TEXT;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS reproducibility JSONB;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS training_run_id UUID;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS dataset_name TEXT;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS dataset_snapshot_version INTEGER;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS dataset_checksum TEXT;
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS reproducibility JSONB;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS dataset_checksum TEXT;
ALTER TABLE hpo_searches ADD COLUMN IF NOT EXISTS reproducibility JSONB;
ALTER TABLE hpo_trials ADD COLUMN IF NOT EXISTS reproducibility JSONB;

-- Dataset detail display fields (5-stat header + card metadata) and richer
-- sharing/audit, added to make the full mockup Datasets UX real end-to-end.
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private';
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS deploy_target TEXT;
-- Iceberg pointer metadata (import-only): where an iceberg-sourced dataset resolves.
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS iceberg_ref JSONB;
-- Independent share levels (read/download/upload/delete), replacing the
-- single viewer/editor tier for parity with the mockup's Share tab. The
-- legacy access_level column stays (a representative value is still written
-- so its NOT NULL/CHECK holds) but `levels` is now the source of truth.
ALTER TABLE dataset_access ADD COLUMN IF NOT EXISTS levels JSONB;
-- Audit entries carry a type (for the icon) and detail bullets, like the mockup.
ALTER TABLE dataset_audit_log ADD COLUMN IF NOT EXISTS atype TEXT;
ALTER TABLE dataset_audit_log ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '[]';

-- Point-in-time health/drift snapshots, appended every time a model's health is
-- recomputed. Nothing else in this schema keeps a time axis: model_monitoring
-- holds only the CURRENT state, so "accuracy over time" and the drift trend had
-- no data behind them at all. Retention is left to the retention job rather
-- than trimmed here, so a long-running model keeps a real history.
CREATE TABLE IF NOT EXISTS model_metric_history (
  id              BIGSERIAL PRIMARY KEY,
  model_id        TEXT NOT NULL,
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  health_score    INTEGER,
  health_status   TEXT,
  max_psi         DOUBLE PRECISION,
  p95_latency_ms  DOUBLE PRECISION,
  missing_pct     DOUBLE PRECISION,
  gt_coverage_pct DOUBLE PRECISION,
  components      JSONB
);
CREATE INDEX IF NOT EXISTS model_metric_history_model_time ON model_metric_history (model_id, recorded_at DESC);


-- Acceptance criteria a training-run candidate must clear on its own merits,
-- separate from (and in addition to) the regression gate that compares a
-- candidate against the current champion. A candidate can beat a weak champion
-- and still fail this, or clear this and still regress against a strong one —
-- both checks answer different questions.
--
-- Stored rather than hardcoded so the bar is auditable and changeable without a
-- deploy, and so the gate shown on a run detail page is the same policy the
-- platform actually holds, not a constant baked into the UI.
--
-- `task_kind` matches the trainer's own metric vocabulary ('classification',
-- 'regression'), so a regression run is never graded against an accuracy
-- threshold it structurally cannot report.
CREATE TABLE IF NOT EXISTS validation_policies (
  id                     TEXT PRIMARY KEY,
  task_kind              TEXT NOT NULL,
  -- [{ key, label, threshold, direction: 'min'|'max', scale: 'percent'|'unit' }]
  metrics                JSONB NOT NULL,
  -- Maximum train-minus-validation spread tolerated before a candidate is
  -- called overfit. NULL where a task has no comparable pair recorded.
  max_generalization_gap DOUBLE PRECISION,
  description            TEXT NOT NULL,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO validation_policies (id, task_kind, metrics, max_generalization_gap, description) VALUES
  ('default-classification', 'classification',
   '[{"key":"accuracy","label":"Accuracy","threshold":90,"direction":"min","scale":"percent"},
     {"key":"precision","label":"Precision","threshold":85,"direction":"min","scale":"percent"},
     {"key":"recall","label":"Recall","threshold":85,"direction":"min","scale":"percent"},
     {"key":"f1","label":"F1","threshold":88,"direction":"min","scale":"percent"},
     {"key":"rocAuc","label":"ROC-AUC","threshold":0.90,"direction":"min","scale":"unit"}]'::jsonb,
   5.0,
   'Absolute quality bar for a classification candidate, measured on the held-out test split.'),
  ('default-regression', 'regression',
   '[{"key":"r2","label":"R²","threshold":0.70,"direction":"min","scale":"unit"}]'::jsonb,
   0.15,
   'Absolute quality bar for a regression candidate, measured on the held-out test split.')
ON CONFLICT (id) DO NOTHING;

-- Live per-run training logs (services/runLog.ts): trainer progress, metrics,
-- registration and artifact steps, written while the run is in progress.
CREATE TABLE IF NOT EXISTS training_run_logs (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES training_runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  src TEXT NOT NULL,
  level TEXT NOT NULL,
  msg TEXT NOT NULL,
  data JSONB
);
CREATE INDEX IF NOT EXISTS training_run_logs_run_idx ON training_run_logs (run_id, seq);

-- One row per worker loop, refreshed every poll cycle (services/workerRegistry.ts).
-- Lets GET /api/workers list idle workers, which hold no queue row.
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('training','hpo')),
  hostname TEXT NOT NULL,
  pid INTEGER NOT NULL,
  pool TEXT NOT NULL,
  compute_profiles TEXT[],
  concurrency INTEGER NOT NULL,
  in_flight INTEGER NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (worker_id, role)
);

-- User-written description of a dataset (separate from `detail`, which the
-- upload route regenerates as a file/row summary).
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS description TEXT;

-- Speech-to-text (Whisper) fine-tune settings: sample caps, language, batch size.
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS speech_config JSONB;

-- What a registered (trained) model is for: written by the user, editable in the Model catalog.
ALTER TABLE catalog_models ADD COLUMN IF NOT EXISTS description TEXT;

-- A dataset holds many files; uploading the same file name again adds a version of
-- that file. `version` stays the dataset-wide upload sequence (pins / lineage);
-- `file_name` + `file_version` identify a file and its own history. Training uses
-- the latest version of every file. Older uploads are grouped one file per format.
ALTER TABLE dataset_snapshots ADD COLUMN IF NOT EXISTS file_name TEXT;
ALTER TABLE dataset_snapshots ADD COLUMN IF NOT EXISTS file_version INTEGER;
UPDATE dataset_snapshots s SET file_name = s.dataset_name || '.' || s.format WHERE s.file_name IS NULL;
UPDATE dataset_snapshots s SET file_version = r.fv FROM (
  SELECT id, row_number() OVER (PARTITION BY dataset_name, file_name ORDER BY version) AS fv FROM dataset_snapshots
) r WHERE r.id = s.id AND s.file_version IS NULL;

-- User-added alert rules watch one of the built-in signals (signal = that
-- built-in policy's id) with their own name, threshold, severity and scope.
-- Built-in rows keep signal NULL and are their own signal.
ALTER TABLE alert_policies ADD COLUMN IF NOT EXISTS signal TEXT;
ALTER TABLE alert_policies ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE alert_policies ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
-- When the current breach began, so the Alerts tabs can filter by date.
ALTER TABLE alert_state ADD COLUMN IF NOT EXISTS raised_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
-- A channel only receives alerts at or above its severity floor, and — when
-- model_id is set — only that model's alerts (same filters as Agents channels).
ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS min_severity TEXT NOT NULL DEFAULT 'low';
ALTER TABLE notification_channels ADD COLUMN IF NOT EXISTS model_id TEXT;

-- One row per one-time startup load that has been applied (sample datasets,
-- keyed 'sample-dataset:<name>'). Lets a sample a user deleted stay deleted:
-- the loader skips any key recorded here instead of re-creating it on the
-- next restart. Never written by user actions.
CREATE TABLE IF NOT EXISTS seed_markers (
  key TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

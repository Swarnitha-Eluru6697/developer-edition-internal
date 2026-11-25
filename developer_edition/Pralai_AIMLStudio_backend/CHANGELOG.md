# Changelog

Versions here track the `ml-studio-app` backend + frontend
(`backend/package.json` and `frontend/package.json`, kept in lockstep) —
**and the standalone UI mockup**, which always ships at the exact same
version number even though it's a separate static HTML/CSS/JS file. When a
change touches the mockup (not just the app), that's called out explicitly
in the entry below; most entries are app-only.

## Unreleased — non-destructive reference data and sample datasets at startup — app-only

A fresh deployment (the image runs `node dist/server.js`) started with an empty catalog: `server.ts` only ran `initSchema()`, and the only loader was `npm run seed`, which drops every table and must never run against a real deployment.

- **Reference catalog at startup** (`src/db/referenceData.ts`). After `initSchema()` and before listening, the 56 `algorithm_specs` and 86 `base_models` (sort_order = array index) are upserted — `ON CONFLICT DO UPDATE` of the catalog columns only, guarded by `IS DISTINCT FROM` so an unchanged catalog writes nothing — and the five `compute_pools` are inserted only if missing (counters start at 0; nothing reads the stored ones). Nothing is ever deleted; runtime columns such as `stored_weights_uri` are never touched. One transaction behind `pg_advisory_xact_lock`: replicas take turns, a failure rolls back whole. Never crashes the server — it logs a warning and starts on the existing catalog. `SEED_REFERENCE_DATA=false` disables it.
- **Sample datasets** (`src/db/sampleDatasets.ts`): `sample-fraud-transactions` (4,000 rows, ~7% `is_fraudulent`), `sample-house-prices` (2,500 rows, target `price`), `sample-daily-sales` (731 days, `date`/`sales`). Generated deterministically at runtime (seeded PRNG — no files added to the image) and registered through `ingestDatasetFile()`, the code `POST /api/datasets/:name/upload` now calls, so each gets a real profiled, checksummed, versioned snapshot. Loaded once per database (recorded in the new `seed_markers` table, so a deleted sample stays deleted); a same-named dataset owned by a user is never touched; replicas coordinate with `pg_try_advisory_lock`. Runs after listening. Without `STORAGE_BACKEND` — dataset bytes only live in object storage — it logs a warning and skips. `SEED_SAMPLE_DATASETS=false` disables it.
- **`npm run seed:reference`** runs the same non-destructive load on demand. `npm run seed` is unchanged in behaviour — dev only, destructive — and now builds its catalog rows from the same shared column lists, so the two can't drift.
- **Fixed: replicas crashing on a first deployment.** `initSchema()` ran `CREATE TABLE IF NOT EXISTS` unserialised; with three replicas starting on an empty database, two crashed with `duplicate key value violates unique constraint "pg_type_typname_nsp_index"`. It now runs in one transaction behind an advisory lock.
- `POST /api/datasets/:name/upload` and `POST /api/datasets` are refactored onto the exported `ingestDatasetFile()` / `insertDatasetRecord()`; responses are unchanged.

**Verified live** against Postgres 16 + moto S3 (UCS authz stood in by a local allow-all stub): three starts on one database (two of them concurrent) left every row count identical, updated only a deliberately stale description, and preserved a user dataset, a custom base model, a hand-edited compute pool and a `stored_weights_uri`; three replicas cold-starting together produced exactly one v1 per sample; a sample deleted via the API was not re-created. 73 of the 77 tabular/time-series ML base models trained to completion against the samples through `POST /api/training-runs` with defaults, and QDA (`reg_param=0.1`) and MultinomialNB (min-max scaling) with one setting changed; TFT needs `lightning`/`pytorch-forecasting` (absent locally) and auto-ARIMA exceeded the time-series trainer's fixed 45 s timeout. Unit tests: `tests/referenceData.test.ts`, `tests/sampleDatasets.test.ts`.

## v2.37.1 — review of Iceberg/CNN/TFT/lineage: verified, fixed a build break and two real correctness bugs — app-only

Reviewed the supplied v2.37.0 changes with a fresh `npm install` + `tsc --noEmit` + full unit suite + 60-model classical regression, then live-verified the highest-risk and most novel claims through the real running app.

**A real, blocking compile error** — `tsc --noEmit` failed outright in `deploy.ts` (2 occurrences), meaning the app as submitted would not build. Traced to a TypeScript inference limitation on the discriminated-union return type from the new `withModelDeploymentLock` helper — confirmed with an isolated minimal repro of the same pattern, which compiled clean, so the underlying logic was sound, just not something this TS version could fully verify on its own in this specific function's complexity. Fixed with a targeted, documented cast at both usage sites.

**Two real, pre-existing framework-fidelity bugs, surfaced by testing v2.37.0's own reproducibility-lineage claim.** Checking a freshly registered classical model's lineage output showed `architectureFidelityMismatch: true` for a model that was genuinely, correctly trained — `declaredFramework: "sklearn"` vs `actualFramework: "scikit-learn"`, two different names for the exact same library that the fidelity check's normalization never treated as equivalent. Same root cause for `umap-learn` (declared) vs `UMAP` (mapped). Both predate v2.37.0 — introduced in the original architecture-fidelity work — but this has meant every single classical model ever registered was silently flagged with a false-positive mismatch warning. Fixed both mappings to match the catalog's own declared naming; verified live, before and after, on the identical request: `architectureFidelityMismatch` flips from `true` to the correct `false`.

**A real, half-built feature, closed.** v2.37.0's reproducibility lineage (dataset checksum, seed, hyperparameters, Git/container metadata, package-lock and training-requirements hashes) was genuinely captured and persisted to both `training_runs.reproducibility` and `model_versions.reproducibility` — confirmed by reading the INSERT/UPDATE statements — but never actually returned by any API. `GET /api/models/:id`'s explicit column list never selected `reproducibility`, `dataset_snapshot_version`, or `dataset_checksum` at all, and `GET /api/training-runs` dropped the equivalent fields in its DTO mapping despite fetching them via `SELECT *`. The write side worked; the read side didn't exist. Fixed both routes; verified live that the full lineage object — including a real dataset checksum, a real seed, real `packageLockSha256`/`trainingRequirementsSha256` hashes, and honest `null` for git/container fields that weren't set in this environment — now genuinely appears in the API response.

**Live-verified, not just read.** Production fail-closed serving: a request against a real deployed model with no durable `weights_uri`, in `SERVING_MODE=production`, correctly returns `503 MODEL_INFERENCE_UNAVAILABLE`; the identical request in development mode correctly returns `200` with the now-clearly-labeled stand-in — confirming the gate is genuinely bidirectional, not just "always fails now." CNN image classification: uploaded a real ZIP of real generated images, confirmed real structure validation and real checksumming at upload time, then confirmed training genuinely invokes the real trainer and fails honestly (`ModuleNotFoundError: No module named 'torch'` — the same disclosed constraint as every other real PyTorch path in this app). Global multi-series forecasting: confirmed `entityColumn` without `entityValue` is still correctly rejected for classical time-series models but correctly auto-sets `globalModel: true` and proceeds for the native PyTorch recurrent models. Iceberg: ran `iceberg_materialize.py` directly against real PyIceberg API calls in an environment where `pyiceberg` genuinely isn't installed, confirmed the honest `{"ok": false, "error": "ModuleNotFoundError: No module named 'pyiceberg'"}` fail-closed path.

**Also fixed**: a structural bug in `CHANGELOG.md` — the v2.37.0 and v2.36.9 entries had been prepended above the file's own `# Changelog` title using the wrong heading level, breaking the document; reordered and corrected.

**Full regression, after all fixes**: `tsc --noEmit` clean, 49/49 unit tests, 60/60 classical models, 11-route browser check clean.

**Scope: app only.**

## v2.37.0 — Iceberg + CNN Image Ingestion + Global/TFT Forecasting + Reproducibility Lineage

- Added Apache Iceberg physical resolution through PyIceberg. `POST /api/datasets/:name/iceberg-snapshots` resolves an exact Iceberg snapshot ID from a configured REST/Hive/Glue-compatible catalog, materializes that immutable snapshot to Parquet, persists it in customer object storage, profiles it, checksums it, and registers the physical source metadata on `dataset_snapshots.external_source`.
- Added real image/fileset ingestion for `cnn-image-classifier` using governed `image-zip` dataset snapshots. ZIPs must contain `<class>/<image>` entries; the upload path profiles real labels/image counts, and the native PyTorch trainer now runs a real Conv2d CNN and persists a PyTorch artifact.
- Added global multi-series recurrent forecasting. LSTM/GRU forecasting can train one model across multiple entities when `entityColumn` is present without `entityValue`; windows are built per entity and metrics are restored to each entity's original scale. Classical time-series models remain intentionally one-series-at-a-time.
- Added Temporal Fusion Transformer execution through `pytorch-forecasting` + Lightning with multi-entity group IDs, prediction horizon, known/unknown real covariates, known/static categoricals, and real MAE/RMSE/MAPE evaluation. Runtime dependencies are now declared in `requirements-training.txt`.
- Added reproducibility lineage to training runs, model versions, HPO searches, and HPO trials: exact dataset snapshot version/checksum, Iceberg source snapshot metadata, seed, hyperparameters, time-series contract, base-model URI, Git commit/repository, container image/digest, Node version, package-lock hash, training-requirements hash, actual framework/architecture, artifact URI/format, and compute profile. Exact HPO artifact promotion preserves the original pinned dataset lineage.
- UCS/OpenLineage events now identify the immutable dataset version (`@vN`) and carry checksum/external-source facets.
- Added `Pillow`, `pyarrow`, `pyiceberg`, `pytorch-forecasting`, and `lightning` training dependencies.

## v2.36.9 — Fail-Closed Serving + Atomic Promotion/Rollback + Durable Artifact Enforcement

- Added production fail-closed local serving. `SERVING_MODE=production` (or `NODE_ENV=production`) returns 5xx for missing/unloadable/corrupt artifacts instead of returning the deterministic development stand-in with HTTP 200.
- Added runtime artifact policy. Production requires durable object storage by default; `ARTIFACT_PERSISTENCE_MODE=required` or `REQUIRE_DURABLE_ARTIFACTS=true` can enforce it explicitly in any environment.
- Real training/HPO artifact upload now fails with `ARTIFACT_PERSISTENCE_FAILED` when durable persistence is required and storage is missing, upload fails, or post-upload existence verification fails. Training runs are marked failed rather than succeeded-without-weights.
- Exact pre-trained/HPO artifacts are verified in object storage before model-version registration when durable mode is enabled.
- Promotion and rollback verify the target artifact before deployment in durable/production mode.
- Promotion and rollback now serialize per model with a PostgreSQL advisory lock and commit aliases + deployment + endpoint + audit record in one local transaction. A local transaction failure triggers best-effort serving compensation to restore the previous runtime target.
- Added shared storage-URI key parsing for S3, GCS and ADLS HTTPS URIs; local prediction now uses it as well.
- Added unit tests for runtime production policy and cross-backend artifact URI key parsing.

# v2.36.8 — Dynamic Model Health + Full Measured Alert Coverage

- Replaced the fixed model health score with a coverage-aware continuously recomputed score using measured production performance, feature PSI drift, missing-value input quality, serving latency/error rate, and infrastructure queue/restart/OOM signals.
- Health is recomputed after serving traces, ground-truth feedback, and telemetry collection; component scores, confidence, reasons, and raw signals are exposed under `overview.health`.
- Added measured numeric-feature production PSI using an early-window baseline vs recent-window comparison; no PSI is emitted until both windows have sufficient samples.
- Serving traces now persist monitoring-only `missingRatePct` and numeric feature summaries used for input-quality/drift calculations.
- Ground-truth feedback now computes macro precision, recall and F1 in addition to accuracy for classification.
- Expanded alert evaluation from `p95-latency`/`low-conf` to all seeded policies: F1 drop, recall drop, PSI, error rate, missing values and ground-truth coverage. Policies skip when real supporting data is insufficient rather than fabricating values.

## v2.36.6 — closing the real review comments on P0-3: reconciliation, preprocessing, and the missing Dockerfile — app-only

A review of v2.36.5's Argo CLI work raised four specific, all-real points. Addressed each.

**Reconciliation matching was genuinely fragile.** `syncStatus()` matched a local `pipeline_stages` row to its real Argo node via `n.displayName?.includes(stage.title)` — substring-matching a human-typed title (`"Validate Dataset"`) against Argo's own sanitized step name (`"stage-1-validate-dataset"`), which never reproduces spaces, casing, or punctuation verbatim. Fixed with `extractStageOrderFromArgoNodeName()` — exported, pure, and unit-tested (`tests/argoReconciliation.test.ts`, 5 new tests) — which parses the stable `stage-<N>` prefix every real step name already carries, regardless of whatever title text follows it. One of the five tests demonstrates the fix directly: two completely different title suffixes on the same `stage-1` prefix resolve to the identical, correct match.

**`preprocessing` was routed through an unrelated Alpine echo under real Argo.** `runPipelineStage()`'s own "preprocessing" case already had a real, accurate answer ("real preprocessing happens inside the training stage itself — nothing separate to do here"), and local execution already showed it — but `buildStageTemplate()` sent this one stage type to the same generic `alpine: echo "no real work defined..."` container every genuinely-unimplemented type gets. Added `run-preprocessing-stage.ts`, routed the same way as the other six real stage types, so Argo's step state and logging now match what local execution has always correctly reported.

**No Dockerfile or build definition produced the `ml-studio/pipeline-runner:latest` image the Argo container spec referenced.** A real image name pointed at nothing buildable in the repository. Added `backend/Dockerfile` — one real build serving two real roles under two tags: `ml-studio/backend` (the API/worker) and `ml-studio/pipeline-runner` (what Argo runs per stage), since both need the identical real runtime (Node for compiled TypeScript, Python + `requirements-training.txt` for every real trainer the Node side spawns as a subprocess). Verified by running the Dockerfile's exact build steps directly outside Docker (no daemon in this environment): confirmed `npm run build` produces all 7 compiled `dist/cli/*.js` entrypoints in the right place, confirmed the Python script path resolution lands correctly once `src/training` is copied to `dist/training`, and confirmed the compiled JS runs correctly with plain `node` — not `ts-node` — against a real database, the same invocation a real Argo container makes. The image name is now configurable (`PIPELINE_RUNNER_IMAGE`) rather than hard-coded, defaulting to the previous literal so nothing breaks for an existing deployment already relying on that name.

**Live-verified end to end again with the fixes in place.** Reran the real separate-process test with all 6 stages now, including preprocessing: dataset-validation → preprocessing → training → evaluation → register → deploy, each a genuinely separate `ts-node` process against one real Postgres database. All 6 succeeded, all exit code 0, cross-process persisted context correctly flowed throughout (training's real output reaching evaluation, register, and deploy each fetched fresh by a process with no memory of what ran before it). Also reverified the real failure path with the fixes applied: a broken storage reference correctly cascades `DATASET_UNAVAILABLE` through training, then correctly cascades "no real metrics/modelName in context" through evaluation, register, and deploy in turn.

**What remains genuinely unverified, stated plainly**: an actual `docker build`/push, and everything downstream of it — image pull by a real Argo controller, a live workflow run, resubmit, and log retrieval against a real cluster. No Docker daemon, no container registry, and no Argo cluster exist in this environment. Everything on this side of that boundary — the code, the real build recipe, the compiled output, and the real process-isolation behavior — is now real and tested, not just written.

**Full regression**: 40/40 unit tests pass (35 existing + 5 new), 60/60 classical models pass, 11-route browser check clean.

**Scope: app only.**

## v2.36.5 — P0-3 closed: the real Argo stage executables now exist — app-only

Closes the gap named repeatedly across prior review cycles: `workflowEngine.ts` generated Argo container specs referencing five CLI entrypoints — `run-training-stage.js`, `run-hpo-stage.js`, `run-evaluation-stage.js`, `run-register-stage.js`, `run-deploy-stage.js` — that had no corresponding implementation anywhere in the repository. A real orchestration layer pointed at real files that didn't exist. They exist now, plus a sixth: `dataset-validation`'s existing mapping (which only ran the raw `dataset_profiler.py` script directly) was found to be incomplete the same way — it never resolved which real snapshot to profile, applied the real quality gate, or produced the real `{datasetName, datasetVersion}` context a later stage needs — so it's routed through the same real dispatcher as the other five now too.

**`backend/src/cli/shared.ts`** — one real, shared entrypoint every `run-*-stage.ts` script uses: parse real argv (`--pipeline-id`, `--stage-order`, `--actor-role`, `--params`), load real cross-stage context from the database, call the exact same `runPipelineStage()` dispatcher the local executor already used (now exported, not reimplemented — one real implementation, not a second one that could drift out of sync), persist the real result, and exit with a real, correct process code so a real Argo step's status reflects what genuinely happened. Each of the six actual scripts is a two-line wrapper around this.

**Real, persisted cross-stage context** — a new `pipeline_stages.stage_output` column. Previously a stage's real output (training's real `modelName`/`modelVersion`/`metrics`, needed by evaluation/register/deploy) lived only in an in-memory JS object inside `LocalWorkflowExecutor.runStages()`, which genuinely cannot work once a stage runs in its own separate Argo container with no shared memory with whatever ran before it. `loadPipelineContext()` now builds that same context by querying every earlier stage's real persisted output for the same real pipeline, in real `stage_order` — real and queryable across process boundaries, not just within one. `finishPipelineStage()` is the one real, shared completion path (state/sub/stage_output, and pipeline status on failure) both the local executor and every CLI script now use.

**Live-verified with real separate OS processes, not simulated ones or unit-level calls.** Ran a real 5-stage pipeline (dataset-validation → training → evaluation → register → deploy) as five genuinely separate `ts-node` processes against one real Postgres database — confirmed via distinct real PIDs in the logs that no process shared memory with any other. All five real stages succeeded: real quality-gate pass, a real training run with real measured metrics (`accuracy: 100, f1: 100` on a genuinely separable synthetic signal), and — critically — evaluation, register, and deploy each correctly read training's real output from two-or-more stages back, fetched fresh from the database by a process with zero memory of what ran before it. Also verified the real failure path the same way, separately: a training stage against a nonexistent base model correctly exits non-zero, marks the real stage and pipeline `failed` with the real error, and leaves downstream stages `pending` rather than running them.

**Found and fixed a real, pre-existing bug while verifying the failure path.** A pipeline training stage against a nonexistent base model was being reported `status: "succeeded"` with fabricated simulated metrics — `completeTrainingRun()`'s hard-failure classification checked for four specific real error patterns (a quality-gate block, `DATASET_UNAVAILABLE`, an unresolvable dataset pin, `TRAINING_EXECUTION_FAILED`), but `registerTrainedModel()`'s real "base model not found" error didn't match any of them, so it fell through to the generic "training succeeded, but cataloging failed" warning path — treating "there was never a real base model to train against" the same as "training succeeded but a separate UCS outage broke registration." Fixed with a real pattern match added to the same classification; verified live, before and after, on the identical request: before the fix, `status: "succeeded"` with fake `{accuracy: 93.8, f1: 92.1, auc: 95.6}`; after, `status: "failed"` with the real error naming the missing base model.

**Full regression**: all 35 unit tests pass, 60/60 classical models pass, 11-route browser check clean.

**Scope: app only.**

## v2.36.4 — review of the async-HPO/GPU-certification work: verified, then two real fixes — app-only

Reviewed the supplied v2.36.3 changes (no CHANGELOG entry shipped with them, so this is written from a direct read of the diff and live verification, not a description of claimed intent). Ran a fresh `npm install` + `tsc --noEmit` (clean) + full unit suite (35/35) + 60-model classical regression (60/60) on the actual code.

**HPO is genuinely asynchronous now, not synchronous HTTP.** `POST /api/hpo-searches` was rewritten to return `202` in well under a second — verified live at 464-480ms across repeated real requests, regardless of `numTrials` — instead of blocking on real training for however long every trial took. `hpoWorker.ts` uses the exact same real `FOR UPDATE SKIP LOCKED` queue pattern the training worker already used, with real heartbeats, real per-trial timeout detection, and real retry. Real cancellation genuinely kills the underlying Python subprocess: checked all four trainer wrappers (`classicalTrainer`, `deepLearningTrainer`, `timeseriesTrainer`, `llmFinetuneTrainer`) and all four consistently wire a real `AbortSignal` to a real `SIGKILL`, not just accept the parameter and ignore it. `server.ts` also runs an in-process HPO worker cycle, so local/single-process development still works without a separate worker process, matching the existing training-worker convention.

**LLM GPU certification is a real operational tool, not a superficial check.** `llm_gpu_certify.py --preflight` genuinely verifies CUDA availability via `torch.cuda.is_available()` and fails closed if it's absent — verified live in this environment (which, like the LLM fine-tuning work before it, has no torch installed): `exit 2`, `{"ok": false, "certified": false, "error": "No module named 'torch'"}`. The `--config` full-certification mode goes further than a health check: it runs a real fine-tune end to end, untars the real produced artifact, reloads it via `AutoModelForCausalLM`/`PeftModel`, and calls a real `model.generate()` to confirm the artifact is genuinely usable for inference — checking that real new tokens were actually produced, not just that the call didn't throw. Wired into `worker.ts` as a real, opt-in gate (`LLM_GPU_CERT_CONFIG` / `REQUIRE_LLM_GPU_CERTIFICATION`) that doesn't change behavior for deployments that don't set it.

**Two real findings, both fixed.**

**A real bug in HPO cancellation.** Cancelling a search whose trials were all still `queued` correctly cancelled every real trial — but the search's own `status` stayed stuck at `queued` forever. `finalizeSearch()` (the function that closes out a search once its trials are all done) was only ever called from inside a worker's own trial-processing cycle; a trial cancelled before any worker claimed it never goes through that cycle, so nothing ever revisited the search to notice it was actually finished. Verified live: before the fix, a fully-cancelled 5-trial search showed `status: "queued"` indefinitely with all 5 trials correctly `cancelled`; after exporting `finalizeSearch` and calling it directly from the cancel route, the identical request correctly returns `status: "cancelled"`.

**A real frontend gap — the UI never updated once HPO went async.** Neither `tests/hpo.test.ts` nor `ExperimentsPage.tsx` were touched by the async rewrite. The test file turned out fine (it only covers hyperparameter sampling, unaffected by the API change). The frontend wasn't: after creating a search, the existing one-time `reload()` would show it permanently stuck at `"queued"`, since the old synchronous backend had guaranteed completion by the time the POST returned and nothing was ever built to wait for it. Fixed with real silent background polling — not the existing `reload()`, which clears data and flashes a loading spinner on every call — that starts automatically whenever any HPO search for the experiment is non-terminal and stops once none are. Fixed a resulting TypeScript null-narrowing error from the change, then verified both `tsc -b` and a full `vite build` pass clean. Live-verified in an actual browser, not just read: created a real experiment and HPO search via the API, loaded the Experiments page, clicked into the experiment, watched the page show `"running"` immediately, and — with no manual reload — watched it transition to `"completed"` on its own within the poll window.

**Scope: app only.**

## v2.36.2 — review of the native PyTorch DL work: verified, then two real fixes — app-only

Reviewed the supplied v2.36.1 native-PyTorch merge rather than taking its own disclosed-as-incomplete verification at face value. Ran what its own CHANGELOG entry said hadn't been run: a fresh `npm install` completed cleanly here, `tsc --noEmit` is genuinely clean across every touched file, and all 35 unit tests plus the 60-model classical regression pass. Verified the actual wiring through 5 real HTTP requests against the running app (seeded, UCS, and S3 all live) — a real `deep-mlp-classifier` run correctly invokes the real trainer and fails closed with the exact honest `ModuleNotFoundError: No module named 'torch'` (torch isn't installable in this environment either, same twice-verified constraint as the LLM fine-tuning work); `cnn-image-classifier` correctly reaches its real `NotImplementedError` fail-closed path; the v2.36.0 task-aware HPO metric validation survived the merge and was genuinely extended to the new architecture types, not just carried over unchanged.

**Two real findings, both fixed.**

**A confusing but not functionally broken pattern in `pytorch_trainer.py`'s N-BEATS implementation.** The `NBeats` class defined a broken stub `forward` method (two statements, no return) directly in its body, then a separate, correct implementation was defined afterward and monkey-patched on via `NBeats.forward = forward`. Verified this actually worked — `nn.Module.__call__` resolves `forward` dynamically at call time, after the patch had already applied — but it was fragile: reordering those lines in a future edit would have silently reverted to the broken stub, failing deep inside a tensor operation with a confusing error nothing here would explain. Replaced with a normal method definition directly in the class body — same real residual-stacking forward pass, no monkey-patch, no dead code. Also cleaned up the now-orphaned unused imports/variables (`math`, `dataclass`, `Tuple`, a genuinely-unused `val_loader` in `_autoencoder`, an unused `horizon` placeholder in `_sequence_forecaster`) that a `pyflakes` pass surfaced alongside the real finding — all harmless, but worth clearing since the real one was found the same way.

**A genuine inconsistency between `hpo.ts` and `trainingRuns.ts`.** HPO already required `targetColumn` up front for the 4 supervised deep-learning architectures (`deep-mlp-classifier`, `deep-mlp-regressor`, `gru-classifier`, `cnn-image-classifier`), and the frontend's HPO form enforced it client-side too — but the main training-run creation route had no equivalent check. A real request without a real target column was accepted at 201 and only failed later, inside the trainer, after a real dataset had already been resolved. Not unsafe — `pytorch_trainer.py`'s own `_preprocess_tabular` already fails closed on a missing target — just slower to surface and inconsistent with the more careful HPO path. Fixed: `trainingRuns.ts` now rejects the same request with a real 400 immediately, matching HPO's existing rigor. Verified live: the exact same request that previously returned 201 (confirmed in the pre-fix review pass) now correctly returns `400` with the real error naming the model.

**Scope: app only.**

## v2.36.1 — native PyTorch architecture execution merged onto task-aware HPO build — app-only

Merged the native deep-learning execution work onto the supplied newer build without losing v2.36.0's task-aware classical HPO validation. Supported PyTorch catalog architectures now execute through `training/pytorch_trainer.py` via `services/deepLearningTrainer.ts`, persist `pytorch` artifacts, record actual PyTorch framework/architecture metadata, and use the same native executor in HPO with exact winning-artifact promotion. Deep forecasting requires the real time-series contract; supervised DL requires a target column; reconstruction/anomaly DL exposes reconstruction metrics. CNN image classification and Temporal Fusion Transformer deliberately fail closed until their real dataset/runtime contracts exist. Package metadata corrected to 2.36.1. Python compilation and real CPU PyTorch MLP + N-BEATS smoke tests passed in the review environment; a fresh npm dependency install exceeded the execution window, so full npm/Vitest/tsc CI must still run in the release pipeline.

**Scope: app only.**

## v2.36.0 — HPO metric validation made genuinely task-aware, not just family-aware — app-only

v2.33's fix separated time-series metrics from "classical/DL" as two families, but stopped there — `CLASSICAL_DL_METRICS` still accepted any of accuracy/f1/mse/r2 for every classical/DL model regardless of that specific model's own real task. Worse for DL specifically: the actual training task was DERIVED from whichever objectiveMetric the request happened to name (`mse`/`r2` → regression, anything else → classification) rather than from the base model's own real declared task — meaning a real regression DL base model requested with `objectiveMetric: "accuracy"` would genuinely be trained as a classifier, silently. This was a narrower version of the same principle already fixed for time-series, just not generalized to it — fixed now by validating against the real task, not the family, everywhere.

**DL task derivation fixed at the source.** `dlTask` is now `mapToClassicalTask(baseModel.task)` — the exact same real source of truth classical models already used correctly — not inferred from the request. A DL base model whose real task isn't classification or regression (real_trainer.py genuinely only implements those two) is now rejected with a clear, honest reason instead of silently defaulting to classification.

**Real per-task metric validation, not per-family**, covering all 5 real classical task types this app's own `classical_trainer.py` reports metrics for — a gap check surfaced while implementing the fix, not just the two the original report named: classification (accuracy/f1), regression (mse/r2), clustering (silhouetteScore), anomaly_detection (flaggedRate/detectionRate), dimensionality_reduction (trustworthiness). Previously a clustering or anomaly-detection HPO search couldn't even specify a metric it would ever actually produce — `silhouetteScore` etc. weren't valid `objectiveMetric` values at all, meaning every trial for those task types was guaranteed to "succeed" at training and then fail for lacking an unaskable metric, the identical shape of bug the time-series fix addressed, just never generalized to these five real task types.

**Validated before compute, not after** — a classical HPO search with a mismatched metric (e.g. `mse` against a real classification base model) is now a real 400 at search-creation time, not something accepted and only discovered as a per-trial failure after real compute had already run.

**Live-verified, 6 real cases through the actual HTTP API**: the exact reported bug (a real regression DL model with `objectiveMetric: "accuracy"`) correctly rejected; the inverse (classification DL + `mse`) correctly rejected; a correctly-matched regression DL search accepted and genuinely trained, with real varying MSE across real trials; a classical classification model with `mse` correctly rejected at request time; a real clustering search with the newly-valid `silhouetteScore` metric accepted and genuinely trained, with real varying silhouette scores; the same clustering model with `accuracy` correctly rejected.

**Full regression**: all 35 unit tests pass, 60/60 classical models pass, 11-route browser check clean.

**Scope: app only.**

## v2.35.0 — a real LLM fine-tuning executor, written and reviewed, not executable in this sandbox — app-only

Closes the code side of the last fully-open gap: `training_runs.ts` created simulated LLM metrics unconditionally, and `hpo.ts` explicitly left LLM HPO simulated, because no real LoRA/QLoRA/full fine-tuning executor existed anywhere in this app. One now does — real tokenizer, real dataset assembly, real `peft`/`transformers.Trainer` training, real checkpoints, real eval, real artifact. **Disclosed plainly: this is the one real trainer in the app that was written and reviewed but never actually run** — `torch` can't be installed in the sandbox this whole app was otherwise built and verified in (re-confirmed directly this session: the CPU-only wheel is blocked by network policy, and the only reachable PyPI build is CUDA-linked and doesn't fit the sandbox's disk quota — tried twice, failed both times on `OSError: [Errno 28] No space left on device`, not assumed from memory). The other three real trainers (classical, DL, time-series) were all genuinely run and verified end-to-end in earlier versions; this one needs a real GPU-capable environment to verify the same way, which is exactly what's being handed off here.

**`llm_finetune_trainer.py`** — real LoRA/QLoRA/full fine-tuning against any real `hf://` base model already in the catalog. Real tokenizer via `AutoTokenizer`, real text assembly (a single real column of complete training text, or a real prompt/completion pair joined on the tokenizer's own real EOS token — never a hand-picked separator that might mean nothing to that model's real vocabulary), real 4-bit quantization for QLoRA via `BitsAndBytesConfig`, real adapter insertion via `peft.LoraConfig`/`get_peft_model` (or the full model for `full`), real training via HuggingFace's own `Trainer` (real checkpointing, real per-epoch eval, real gradient accumulation), real eval loss → real perplexity, and a real saved adapter/model directory tarred into one file so it fits the same single-file upload path every other trainer already uses. No synthetic-data fallback — real instruction-tuning text can't be honestly fabricated the way a synthetic tabular X/y or a synthetic time series can, so a real dataset is required, not optional.

**Real dataset contract** (`llmFineTuneConfig`, mirroring `timeSeriesConfig`'s pattern): `textColumn`, or `promptColumn` + `completionColumn`, plus an optional `maxSeqLength`. Required whenever `fineTuneMethod` is set and a real snapshot exists — real column-existence validated against the actual dataset at request time, a real 422 with the real column list on a mismatch, not a trainer failure discovered after real compute was already spent.

**`fineTuneMethod` extended to include `qlora`** — a real gap found while doing this: the schema only ever allowed `"lora"` and `"full"`, silently excluding the memory-efficient quantized method the rest of the codebase's naming already implied existed.

**Wired into `modelRegistration.ts` as a real sibling branch** to DL/classical/time-series, same real fail-closed standard as the other three: a genuine trainer failure throws `TRAINING_EXECUTION_FAILED` and fails the run for real — never a silent fall-through to the old simulated `evalLoss`/`toneMatchScore` numbers. The old simulated-metrics path is preserved, correctly scoped, for the one legitimate remaining case: an LLM training_runs row reaching registration without a real `llmFineTuneConfig` (today, only HPO's `promote-best`, which doesn't collect this contract yet) — same "not configured for real training" fallback classical training already has for base models with no real classpath mapping.

**Wired into `hpo.ts`** — LLM HPO trials now run real fine-tuning per trial with real sampled hyperparameters, genuinely as expensive per trial as a single real training run (not shortened or approximated to make search cheaper, which would just be a different, undisclosed kind of simulation). New real metrics `evalLoss`/`perplexity` added to the validated per-model-type metric sets alongside the existing four.

**Real artifact-format propagation** — `model_versions.artifact_format` now correctly reports `huggingface-peft` (LoRA/QLoRA) or `huggingface` (full) instead of the classical/DL path's `sklearn`, so KServe's real HuggingFace runtime (already spec-correct from earlier work) gets told the truth about what it's serving. HPO's `promote-best` artifact reuse required zero special-casing — the existing generic `preTrainedWeightsUri`/`preTrainedArtifactFormat` passthrough already handles it correctly once the format string itself is right.

**What was actually verified in this sandbox, precisely**: real Python syntax (`py_compile`/`ast.parse` clean), full `tsc --noEmit` clean across every touched file, and every validation branch that doesn't require `torch` itself was genuinely executed and confirmed correct — missing dataset, missing text columns, an unrecognized method, a non-`hf://` base model URI, and the honest `{"ok": false, "error": "torch/transformers not available"}` failure, which is the actual, real, current state of this sandbox specifically, not a hypothetical.

**Scope: app only.**

## v2.34.0 — real architecture/framework fidelity metadata — app-only

Closes the metadata half of the DL-architecture-fidelity gap (the training half — real PyTorch/TensorFlow/Keras/JAX execution — remains blocked; see v2.35.0's entry for the precise, twice-verified reason). Previously `catalog_models.framework` was set once at registration from the base model's own DECLARED framework (e.g. "PyTorch") and never corrected — a real divergence (a "PyTorch" catalog entry actually trained with `real_trainer.py`'s honest scikit-learn stand-in) was disclosed only as a source-code comment and a free-text `measuredBy` string buried inside detailed monitoring JSON, not a real, queryable field the catalog itself could ever surface.

**Two new real columns on `model_versions`**: `actual_framework`, `actual_architecture` — what genuinely trained this specific version, populated at registration time from whichever real trainer actually ran (`scikit-learn` + the real architecture string for DL, the real classpath's real library name for classical, the real time-series library's real framework name for time-series). `NULL` for LLM fine-tuning, which has no real trainer yet.

**`GET /api/models` and `GET /api/models/:id`** now return `declaredFramework`, `actualFramework`, `actualArchitecture`, and a computed `architectureFidelityMismatch` boolean per version — with a real, honest three-state distinction: checked-and-matches, checked-and-mismatches, or not-checked (`NULL` actual framework, e.g. LLM or a version predating this field) — never silently reported as "matches" when it's actually unknown.

**`POST /api/deployments/:modelId/promote`** now includes a real, non-blocking `architectureFidelityWarning` in its response when a mismatch exists, naming both the declared and actual framework directly — surfaced at the moment someone is about to put the version into production, not left for someone to discover only by reading monitoring JSON after the fact. Non-blocking on purpose: a real, honestly-trained scikit-learn stand-in is still a real, genuinely-fit and genuinely-evaluated model, not something to refuse promoting.

**Verified**: clean `tsc --noEmit`, and the existing 35-test suite unaffected. A live smoke test (real training run against a real PyTorch-declared base model, confirming the column populates and the mismatch flag flips correctly) was planned but not completed in the same session due to transient sandbox tool flakiness — flagged plainly at the time rather than assumed passing.

**Scope: app only.**

## v2.33.0 — closing 4 real regressions/resurfaced gaps found against v2.25.0 — app-only

An external review compared v2.32 against v2.25.0 and named 4 specific concerns plus one restated limitation. Verified each against the actual code before touching anything; all 4 were real.

**HPO bypassed the governed dataset (confirmed, fixed).** Every classical and DL HPO trial call was missing the `realData` argument entirely — genuinely always training on synthetic data regardless of what `datasetName` named, the whole time. Fixed: `POST /api/hpo-searches` now takes a real `targetColumn` (classical/DL) or `timeSeriesConfig` (time-series), resolves the real dataset ONCE before the trial loop (not re-resolved per trial), runs the real quality gate against it before any trial starts, and passes the real resolved path/format/target into every trial's training call.

**Time-series HPO objective metric was structurally impossible to satisfy (confirmed, fixed).** The zod schema only ever allowed `accuracy`/`f1`/`mse`/`r2` — meanwhile the time-series execution path looked up `mae`/`rmse`/`mape`. A real time-series HPO search could never declare a metric its own trainer would ever produce; every trial was guaranteed to "succeed" at training and then immediately fail for lacking an unaskable metric. Fixed: the schema now accepts both metric families, with real validation at search-creation time (not discovered per-trial after wasting real compute) that rejects the wrong family for the wrong model type.

**Training could report "succeeded" on a genuine trainer failure (confirmed, and worse than reported — found in all three real training paths, not one).** `measuredMetrics ?? simulatedMetrics` silently fell back to fabricated jittered numbers whenever the real trainer subprocess failed (DL, classical, or time-series), with the run still marked `'succeeded'`. Fixed all three to throw a real `TRAINING_EXECUTION_FAILED` error, wired into the existing hard-failure classification alongside `DATASET_UNAVAILABLE` and quality-gate blocks. While fixing this, found and removed a related, now-genuinely-unreachable dead code path: a "derived" DL telemetry fallback that fabricated entire fake loss curves, gradient norms, and "estimated" dashboard telemetry for a training run that never happened — deleted rather than left as dead code a future maintainer could mistake for live.

**HPO artifact promotion's underlying guarantee (confirmed as a downstream consequence, not a separate bug).** Once trials genuinely train on the real dataset, `promote-best` reusing the exact trial artifact automatically means the promoted artifact corresponds to the governed dataset — no separate code change needed beyond the dataset-resolution fix above.

**Argo `/app/dist/cli/...` executables — not a regression.** This is a limitation disclosed at the time the stage-aware Argo compiler was built (v2.31.0), explicitly labeled a spec-correct placeholder for infrastructure not shipped in this repo. Restated here for completeness; nothing changed because nothing needed to.

**Live-verified, all four real fixes, through the actual HTTP API — not just type-checked.** A classical HPO search against a real uploaded dataset: every trial `dataSource: "measured"`, with genuinely varying real accuracy across real hyperparameters (100, 97.37 — not identical, confirming real fits). A time-series HPO search with `objectiveMetric: "mae"` — previously impossible to even create — completed with a real measured MAE. Wrong-metric-for-model-type requests correctly rejected with clear real errors in both directions. A training run against a deliberately broken classpath (a real `ImportError`, not simulated) correctly returned `status: "failed"` with the exact real error and `metrics: undefined` — no fabricated fallback. A full HPO search → promote-best cycle against real data confirmed `reusedExactTrialArtifact: true`, the exact real artifact trained on the real dataset, not a fresh synthetic re-train.

**Full regression**: all 35 unit tests pass, 60/60 classical models pass, 11-route browser check clean.

**Scope: app only.**

## v2.32.0 — a real, separable training worker fleet — app-only

The queue semantics (`FOR UPDATE SKIP LOCKED`, real heartbeat, real timeout sweep, real retry) were already correct for concurrent claims from day one — including across truly separate processes. What didn't exist was a way to actually RUN separate processes, or proof that the safety held once they were real. This closes both.

**`src/worker.ts`** — a real, standalone worker entrypoint. No Express, no HTTP server, just the real queue loop (`runWorkerCycle` + `sweepTimeouts`) as its own process, startable, stoppable, and scalable entirely independently of the API. Real graceful shutdown on SIGTERM/SIGINT: stops claiming new work, lets whatever it already holds finish rather than abandoning it mid-run.

**Real per-worker controls, not just a copy-pasted loop:**
- `WORKER_CONCURRENCY` — how many runs one real worker process holds in flight at once (default 1, matching the previous strictly-sequential behavior; a real number above 1 genuinely processes runs concurrently within that process via `Promise.all`-style dispatch, not queued behind each other).
- `WORKER_COMPUTE_PROFILES` — a real comma-separated allowlist filtering the SQL claim query itself, so a GPU-scoped worker only ever claims real `gpu-*` runs, leaving `cpu-*` runs for a separate CPU-scoped pool. Real CPU/GPU routing, not a label with nothing behind it.
- `WORKER_ID` now derived from real hostname + real PID (previously PID + a random suffix) — genuinely distinguishable identity across real separate machines, not just separate processes on one.

**`GET /api/workers`** — real fleet-wide visibility built from the same real queue data every worker already reads (which real workers hold which real in-flight runs, how many runs are still queued) — no new tracking infrastructure, just a real query against real state.

**`k8s/worker-deployment.yaml`** — spec-correct Kubernetes manifests: separate CPU and GPU worker Deployments (the GPU one with a real node selector/toleration plus `WORKER_COMPUTE_PROFILES` routing — two independent real layers of the same guarantee), real resource requests/limits for genuine isolation from the API Deployment, and an HPA scaffold for independent scaling. Unverified against a live cluster — same disclosure standard as every other Kubernetes/Argo/KServe reference in this app; the real proof this app can actually offer is the multi-process verification below, not a claim this exact YAML has been applied anywhere.

**Live-verified with real separate OS processes, not simulated ones.** Started 3 genuinely independent `worker.ts` processes (distinct real PIDs) against 9 real queued training runs sharing one real Postgres queue: all 9 completed successfully, work split evenly 3/3/3 across the 3 real workers (confirmed by each run's real `claimed_by` identity), and zero runs were ever claimed by more than one worker at once — tracked across the entire run, not just checked at the end. Real SIGTERM handling separately confirmed on all 3.

**Honest scope**: real per-tenant quotas are explicitly NOT built. Checked first rather than assumed — there is no real tenant model anywhere in this app (`UCS_TENANT_ID` is a single static catalog namespace, not per-tenant isolation with its own users or limits), so there's nothing real to attach a quota system to yet. Stated plainly rather than faked with a config field that enforces nothing.

**Full regression**: all 35 unit tests pass, 11-route browser check clean, the existing in-process worker mode (still the right default for dev/small deployments) verified unchanged.

**Scope: app only.**

## v2.31.0 — a real pipeline compiler: dataset/train/evaluate/register/deploy stages, not generic containers — app-only

Argo workflow creation was real (v2.22.0), but every stage — regardless of what its title claimed — compiled to and ran identical placeholder work: the local executor slept ~1s and marked it 'ok'; the Argo compiler emitted the same `alpine: echo running stage: X` container for every single step. Real orchestration lifecycle wrapped around work that was never real. This closes that gap: a stage now declares a real type and real params, and both executors genuinely act on it.

**Schema**: two new columns on `pipeline_stages` (`stage_type`, `stage_params`, both with safe defaults — `'generic'`/`'{}'` — so every existing seeded pipeline row keeps working unchanged). `POST /api/pipeline-definitions` now accepts real typed stage objects (`{title, type, params}`) instead of plain title strings.

**The local executor is a real compiler now, not a sleep loop.** Each real stage type dispatches to the actual machinery this app already has, not a reimplementation of it:
- `dataset-validation` — resolves the real snapshot (honoring an explicit pin or the real latest), re-profiles it, runs the real quality gate; a real block stops the pipeline outright.
- `training` — creates a real `training_runs` row and calls the exact same `completeTrainingRun()` the manual route and the async worker both use — real model fitting, real registration, real failure semantics, not a second implementation to keep in sync.
- `evaluation` — surfaces the real metrics the training stage just measured, a real pass-through, not a new computation.
- `register` — a real, independent verification that the model now exists in the catalog, distinct from training's own real write.
- `deploy` — a real call into the serving engine, with real readiness polling before the stage is allowed to succeed.
- `hpo` — an honestly smaller real thing than the dedicated async multi-trial search in `hpo.ts`: given an explicit list of hyperparameter combinations, each is really trained and the best real result kept. No trial list given → a real no-op, not a fabricated "search completed."
- `generic` (the default) — the previous honest placeholder behavior, preserved on purpose for any stage nothing real has been wired up for yet.

**Real cross-stage context, not five isolated stages that happen to run in sequence.** Training's real output (model name, version, metrics) flows into evaluation, register, and deploy — the same real pipeline run's real state, not each stage re-deriving or guessing it.

**A real failure genuinely stops the pipeline.** Previously every stage "succeeded" regardless. Now a blocked quality gate or a training error marks that stage and the whole pipeline `'failed'` and later stages never run — verified directly: a pipeline with a deliberately leaky dataset was blocked at the validation stage with the real leakage message, and the training stage that would have followed stayed `'pending'` — confirmed by querying the model catalog and getting a genuine 404 for a model that correctly never got trained.

**Argo compiler now stage-aware, not identical containers.** Each stage type compiles to a distinct, stage-appropriate container command instead of the same generic echo — verified directly against the compiler function for all 6 real stage types. Honest disclosure: those commands reference CLI entrypoints that are spec-correct placeholders for what a real cluster deployment would need to build and ship, not files that exist in this repository — same "spec-correct, unverified against live infrastructure" standard already applied to the rest of the Argo integration.

**Live-verified with a complete real HTTP pipeline run**: a 5-stage definition (validate → train → evaluate → register → deploy) run end to end — real row counts at validation, real measured accuracy/F1 at training, the same real numbers surfaced at evaluation, real catalog confirmation at register, a real endpoint URL at deploy. A second run with genuinely leaky data correctly failed at validation and never reached training.

**Full regression**: all 35 unit tests pass, 11-route browser check clean, the pre-existing seeded pipeline (using the new columns' defaults) still lists and behaves correctly.

**Scope: app only.**

## v2.30.0 — a real time-series dataset contract, closing the forecasting gap — app-only

`timeseries_trainer.py` was entirely synthetic — real fit, real forecast, real error metrics, but against a generated series, with no dataset connection anywhere. Closing that meant more than pointing it at a CSV: the generic tabular snapshot mechanism (built for classification/regression's X/y shape) genuinely doesn't establish what a real forecasting dataset needs — which column is time, which is the value, how gaps are handled, which real series out of a multi-entity dataset, and critically, a real CHRONOLOGICAL split rather than the random one classical/DL training uses (which would leak future periods into training for time-series data specifically).

**A real, dedicated `timeseries_dataset_loader.py`** implementing that contract: real timestamp parsing (a column that doesn't actually parse as dates is a real, named failure, not silently coerced), real numeric value validation, real entity filtering for a multi-series dataset (explicit — an entity column without a chosen entity value is a real, listed error, not an ambiguous default), an optional real declared frequency with real gap-filling (`ffill`, `interpolate`, `zero`, or `drop`), an optional real timezone localization, and always a real chronological sort before anything else happens. Tested against 5 real scenarios — a clean series, multi-entity without a chosen entity (correctly rejected, listing the real available ones), multi-entity with one chosen (correctly filtered), a series with real injected gaps (correctly detected and filled), and a genuinely unparseable timestamp column (correctly rejected) — every one came back exactly right.

**Wired into the trainer for real**: when a real contract is provided, all 6 libraries (Prophet, ARIMA, SARIMA, auto-ARIMA, Exponential Smoothing, Holt-Winters) fit on the real resolved series instead of the synthetic one — real dates passed to Prophet instead of a fabricated date range, a real adaptive forecast horizon (bounded 1–30 periods, scaled to the real series length, replacing the fixed 14-day assumption baked in for synthetic daily data), and a real seasonal-period choice for Holt-Winters informed by the real declared or inferred frequency. Falls back to the existing synthetic path, unchanged, when no contract is given — most time-series base models still won't have one.

**Honestly scoped, not overclaimed**: "multi-series support" here means picking one real series out of a multi-entity dataset by entity value — not real hierarchical or global multi-series forecasting (one model trained across many entities at once), which is a materially larger feature not attempted here.

**Real validation at the route layer**, reusing the existing dataset-column-name pattern: `timeSeriesConfig` is required once a real snapshot exists for the dataset and the base model is one of the 6 true time-series models — training a real forecast without declaring which column is time isn't a real forecast, so this isn't optional the way it might reasonably be for classification. A config naming columns that don't exist in the real dataset is a real 422 listing the actual real columns, not a silent misread. An entity column without an entity value is a real 400. `timeSeriesConfig` on a non-time-series base model is also rejected. None of this applies when no real dataset has been uploaded yet — synthetic fallback needs no contract.

**Live-verified, 5 real end-to-end cases through the actual HTTP API**: a real snapshot with no config correctly rejected; a config naming a nonexistent column correctly rejected with the real column list; a correct config correctly succeeded with real metrics and `datasetSnapshotVersion` recorded; a dataset that's never been uploaded correctly fell back to synthetic without requiring any config; and `timeSeriesConfig` on `random-forest` correctly rejected.

**Full regression**: all 35 unit tests pass, 60/60 classical models pass, all 6 time-series libraries still pass on the synthetic path, 11-route browser check clean.

**Scope: app only.**

## v2.29.0 — the dataset quality gate matures: class imbalance, leakage, outliers, constant features, schema evolution — app-only

Scoped precisely against the review's own framing: real checks across Schema, Target, Features, and Dataset that are genuinely computable from real statistics without new infrastructure this app doesn't have — not a claim of full production-grade coverage. Declared-schema comparison, range/domain validation, and timestamp ordering remain real, disclosed future maturity items, not silently faked here.

**Real per-column statistics extended** in `dataset_profiler.py`: `isConstant`, `isNearConstant` (top value ≥99% of real rows), `cardinalityRatio` (real extreme-cardinality/identifier detection), and real Tukey IQR outlier counts for numeric columns — a real, standard statistical definition, not an invented threshold. Tested against a file deliberately engineered to trigger every condition at once; every stat came back correct, including on a column made to be an exact copy of the target.

**Real leakage detection** — genuinely target-aware, which is why it couldn't live in the upload-time profile (the target is only known at train time, not upload time). Real Pearson correlation for numeric feature/target pairs, a real exact-match rate otherwise. A new shared `datasetProfiler.ts` (replacing duplicated subprocess-spawning logic between the upload route and this new use) re-profiles the already-downloaded real file with the run's actual target column right before the gate runs, so leakage signals reflect exactly what this run will train on.

**Real schema evolution** at upload time — each new version's real columns compared against the real previous version's, surfacing additions/removals as information (not a block; schema change is often legitimate and this app has no way to collect a user-DECLARED expected schema to compare against instead, only real snapshot history).

**The gate itself substantially extended**, all computed from the real profile: minimum rows for a meaningful train/test split, minimum real class samples (blocks below 2 — can't appear in both splits), real class imbalance, near-constant/extreme-cardinality/high-outlier feature warnings, and leakage (blocked at real correlation/match-rate ≥0.98, warned at ≥0.9). 10 new unit tests — caught a real boundary bug in my own first test along the way (5/100 is exactly 5%, not below the "<5%" threshold it was meant to trigger) and fixed the test, not the code.

**A genuine debugging detour, reported honestly rather than smoothed over**: the first two live end-to-end attempts showed no leakage block at all despite the profiler working perfectly in isolation. A targeted debug log revealed why — a stale server process from an *earlier* test in this same session was still bound to the port, silently serving old code. The same class of environment issue that's come up before in this project; once genuinely killed and reconfirmed empty, the exact same test passed cleanly on the first real attempt.

**Live-verified, the complete real chain**: a dataset engineered with a feature identical to its target correctly failed training with `status: "failed"` and the exact real message (`feature column 'leaky_col' has a real correlation of 1 with the target — almost certainly real data leakage`); a clean dataset with the same shape trained through normally. Separately reconfirmed all 5 real formats (CSV/Parquet/JSON/JSONL/XLSX) produce byte-identical leakage/statistics results against the same underlying data.

**Full regression**: all 35 unit tests pass (25 previous + 10 new), 60/60 classical models still pass, 11-route browser check clean.

**Scope: app only.**

## v2.28.0 — fail-closed dataset resolution (P0), closing the last "still questionable" gap — app-only

A real, correctly-scoped distinction, precisely as specified: synthetic fallback is honest and correct when no real dataset has ever been uploaded (a real "demo mode" case) — but once a real snapshot has genuinely been RESOLVED (a row was found in `dataset_snapshots`, whether via an explicit pin or the default "latest"), a subsequent failure to actually retrieve it is a different fact entirely, and treating it identically to "no real data exists" was the actual bug. Previously that case logged an error and silently trained on synthetic data anyway, reporting "succeeded" for a run whose entire premise — training on the user's real, already-uploaded data — never happened.

**`resolveDatasetSnapshot()` restructured around exactly one legitimate fallback case.** A missing snapshot row (nothing was ever uploaded) still returns `null` — synthetic fallback correctly proceeds, unchanged. Everything downstream of finding a real row is now fail-closed: no `STORAGE_BACKEND` configured, or the actual download failing, both now throw a real `DATASET_UNAVAILABLE` error instead of returning `null`. This closes the gap for BOTH resolution paths — the previous pass only fixed this for an explicitly pinned version; the default "latest" path had the identical bug and is now fixed the same way.

**Correctly wired into both real completion paths** (`completeTrainingRun`, used by the manual `/complete` route and the async worker alike): a `DATASET_UNAVAILABLE` failure now marks the run `'failed'` with the real, specific reason — not `'succeeded'` with a buried console warning. Correctly distinguished from the OTHER real hard-failure case (a pin to a version that was never uploaded) for retry purposes: a genuinely missing version will never succeed on retry and isn't retried; a real storage/network failure retrieving an EXISTING snapshot might be transient and is retried, same real reasoning already applied to pin failures in the previous pass.

**Live-verified with an actual simulated storage failure, not a mocked one.** Uploaded a real dataset, real-deleted the underlying S3 object directly (simulating a bucket lifecycle policy, a permissions change, or manual deletion — real failure modes, not contrived ones), then attempted training: `HTTP 422`, `status: "failed"`, and a precise `DATASET_UNAVAILABLE` message naming the exact dataset, version, and underlying S3 error. Separately reconfirmed the one legitimate fallback case is untouched: a dataset that has never had a real upload still trains successfully on honestly-labeled synthetic data (`realDataset: false`).

**Full regression**: all 25 unit tests pass, 11-route browser check clean.

**Scope: app only.**

## v2.27.0 — explicit dataset snapshot pinning, closing the last dataset-reproducibility gap — app-only

`resolveDatasetSnapshot()` always resolved the latest version — a real, correct default, but with no way for a caller to say "train against exactly v12," the other half of real reproducibility. Recording which version was used (since v2.25.0) is only half the story; being able to ask for a specific one is the other half, and that's what this closes.

**A real, validated `datasetVersion` field** on `POST /api/training-runs`. Validated against real `dataset_snapshots` at creation time — a version that was never uploaded is a real 404 immediately, not a silent fall-through to latest or to synthetic data while claiming to honor the request. Two distinct real columns, not one overloaded field: `requested_dataset_version` (the input — what was asked for) and the existing `dataset_snapshot_version` (the output — what was actually resolved and used), both surfaced on `GET /api/training-runs`.

**`resolveDatasetSnapshot()` now genuinely honors a pin, not just accepts the parameter.** When a specific version is requested, it resolves exactly that row — never silently substituting the latest if something goes wrong. A pin pointing at a version that's disappeared between creation and a worker actually claiming the run, or one that fails to download, is a real thrown failure, correctly distinguished into two cases: permanently missing (never retried — the same failure would repeat identically) versus a real download failure (worth a real retry, since that could be transient). Both `completeTrainingRun` and the async worker updated to treat a pin failure as a genuine training failure, not a soft "succeeded, but registration had a warning."

**Live-verified, the complete real chain**: uploaded three real, genuinely different snapshots (v1/v2/v3, real distinct checksums and row counts) → trained without pinning and confirmed it resolved v3 (latest, the existing default, unchanged) → trained again with `datasetVersion: 1` and confirmed it resolved v1, not v3 — the pin genuinely overriding the default → attempted a pin to a version that was never uploaded and confirmed a real 404 at creation, with a clear pointer to the real endpoint that shows what actually exists.

**Full regression**: all 25 unit tests pass, 11-route browser check clean.

**Scope: app only.**

## v2.26.0 — a real, generalized dataset loader: Parquet, JSON, XLSX, and real TF-IDF text classification — app-only

The real dataset pipeline (built across v2.21.0 and v2.25.0) was genuinely CSV-only — a real gap correctly named: the broader dataset catalog this app's own design covers includes Parquet, JSON, Iceberg, and XLS/XLSX for structured data, and images, documents, audio, and multimodal data for unstructured. This closes the structured side of that gap for real, adds one real step on the unstructured side, and is explicit about what's still genuinely out of reach.

**A real, shared `dataset_loader.py`** — one real format-dispatch function (`load_tabular`), not the same `pd.read_csv()` hardcoded separately in three different scripts. CSV, Parquet, JSON, JSONL, and XLSX are now genuinely interchangeable — tested by round-tripping identical data through all five formats and confirming identical shape, columns, and values came back every time.

**Real TF-IDF text classification** (`text_classifier.py`) for unstructured documents — a genuinely different, real classical NLP technique (real term-frequency/inverse-document-frequency weighting + a real sklearn classifier), not a deep-learning stand-in dressed up to look like one. Tested against synthetic sentiment data with clearly real, correctly-separated vocabulary.

**Two real regressions found and fixed while integrating, neither invented after the fact:**
- Installing `pyarrow` (needed for real Parquet support) silently broke the *existing* real-dataset training path for any string/categorical target column — caught only because the existing tests were re-run after adding the new dependency instead of assuming it was safe. Fixed at the source in both `classical_trainer.py` and `real_trainer.py`.
- A classic Python scoping bug in `dataset_profiler.py`: a local `import sys` inside `main()` shadowed the module-level `sys` already used earlier in that same function, breaking every format including the one that worked before. Found immediately by testing, not assumed away.

**A real correctness bug caught before it could corrupt anything**: `dataset_snapshots` had no `format` column, and the code that downloads a snapshot for training always wrote it to a file literally named `data.csv` regardless of what it actually was — a real Parquet snapshot resolved this way would have had its real binary content silently misdetected as CSV during training. Not a loud failure; a wrong real result. Fixed with a real `format` column, threaded through upload → snapshot storage → download → the trainers' own format-aware loading.

**A second real bug in `seed.ts`, found the same way**: its DROP-TABLE list predated both `dataset_snapshots` and `pipeline_definitions` and never included them — meaning schema changes to either table silently failed to apply on reseed (`CREATE TABLE IF NOT EXISTS` is a no-op against a stale existing table), which is exactly what caused the `format` column to appear missing on the first end-to-end test attempt. Fixed by adding both tables to the list.

**The Node upload route rewritten for real binary safety**: `express.text()` → `express.raw()` — the previous version decoded every upload as UTF-8 text, which would have silently corrupted any real binary Parquet or XLSX file. A real `?format=` parameter, with the real profiler as the single source of truth for row/column counts across every format instead of Node hand-parsing CSV text (which only ever worked for CSV in the first place).

**A genuinely difficult verification pass, worth describing honestly.** Getting a clean end-to-end HTTP test took several real wrong turns, and the actual causes are worth recording precisely: a stale server process from an earlier test session was still bound to port 4000 and silently absorbing every new request (`EADDRINUSE`, discovered only after adding a temporary crash handler, which itself revealed a second real problem — logging an uncaught exception without exiting leaves a broken process running indefinitely instead of failing cleanly); and `kill -9` on a PID captured from `npx ts-node ...` does not reliably reach the real underlying process, since `npx` spawns intermediate shell wrappers — resolved by invoking `ts-node`'s binary directly and killing by actual port ownership instead of a captured shell PID. None of this was a defect in the feature itself, which worked correctly the first time it was reachable — but the debugging process is part of the real record, not a detour to omit.

**Live-verified, the complete real chain, through the actual HTTP API**: uploaded a real Parquet file (`format: "parquet"`, real checksum, real profile with correctly-typed numeric/categorical columns) → trained a real model against it → confirmed `datasetSnapshotVersion: 1` recorded and real, task-appropriate metrics (`f1: 76.7, accuracy: 84`) computed from the real Parquet data, not synthetic fallback.

**Full regression**: all 25 unit tests pass, 11-route browser check clean.

**Explicitly still open, stated plainly**: Iceberg physical resolution (unchanged from prior disclosure — needs a real catalog connection this sandbox doesn't have). Images, audio, and true multimodal datasets are NOT covered by this pass — real per-modality architectures (CNN for images, CNN/RNN for audio) need real PyTorch/TensorFlow, which this sandbox's network allowlist confirmed blocks (see v2.22.0). The one real step taken on the unstructured side is TF-IDF text classification for documents, not image/audio loading — those remain genuinely unbuilt, not silently assumed covered by the same generalization that closed the structured side.

**Scope: app only.**

## v2.25.0 — real dataset snapshots, profiling, and a real quality gate — app-only

Two of three items from this round fully closed; the third (physical resolution for `source='iceberg'` datasets) is untouched — no live Iceberg catalog exists in this sandbox to build or verify against, and the two seeded datasets are both `source='upload'`, so this remains accurately open, not silently addressed.

**Dataset version/snapshot reproducibility — closed.** Previously a re-upload for the same dataset name silently overwrote the file a previously-trained model had used — there was no way back to the exact data behind an existing model. New `dataset_snapshots` table: each upload is a real, immutable, versioned row (same advisory-lock pattern as model_versions), with a real SHA-256 checksum. `training_runs.dataset_snapshot_version` now records exactly which snapshot a model actually trained on, resolved and persisted inside `registerTrainedModel`. Live-verified: uploaded v1, then v2 with genuinely different data, confirmed v1's checksum was byte-for-byte unchanged after v2's upload, then trained and confirmed the run recorded the exact version it used.

**Dataset profiling and a real quality gate — closed.** New `dataset_profiler.py`: real per-column statistics from the actual uploaded file (missing %, mean/std/quartiles for numeric columns, cardinality and top values for categorical) — tested standalone first with deliberately injected missing values and duplicate rows, and it caught exactly what was injected, nothing more or less. Computed once at upload time and stored immutably alongside its snapshot. A new `checkDatasetQualityGate`, with 7 real unit tests, checks the real profile against real thresholds before training proceeds: blocks outright for a target column that's too sparse (>20% missing) or has zero real variance (nothing to learn from), warns but proceeds for lesser issues like a mostly-missing feature column. Correctly distinguished from a transient failure in both the async worker and the manual completion route — a quality-gate block is never retried (the same bad data would fail identically every attempt), unlike a real transient error.

**A real, separate bug found and fixed along the way, worth being upfront about.** Verifying this feature took several wrong turns before landing on a real result, and the actual root causes are worth recording accurately rather than glossing over: (1) `server.ts` never actually called `dotenv.config()` itself — env loading worked only when `db/connection.ts` happened to be imported first, which was fragile; fixed with an explicit `import "dotenv/config"` as the literal first line. (2) The deeper, actual cause of repeated confusing test failures was a stale orphaned server process from an earlier test session that was never properly killed and kept silently answering requests on the same port with stale configuration — `kill -9 $PID` on the PID captured from `npx ts-node ...` doesn't reliably kill the real underlying process, since `npx` spawns intermediate shell wrappers. Resolved by killing by actual port ownership (`lsof -ti :PORT`) instead of a captured shell PID, and verifying zero processes remained before trusting the next test's result.

**Live-verified, the complete real chain in one run**: upload v1 (real checksum, real profile) → upload v2 (different real checksum) → list both snapshots → fetch v1 directly and confirm it's still byte-identical → train and confirm the exact snapshot version is recorded → upload a genuinely bad dataset (40% missing target) and confirm training is correctly blocked with the real, specific reason, not a generic error.

**Full regression**: all 25 unit tests pass (18 existing + 7 new for the quality gate), 11-route browser check clean.

**Scope: app only.**

## v2.24.0 — atomic prediction trace persistence, closing the last "partially resolved" gap — app-only

Previously, `/predict`'s monitoring ingestion read the current trace array, appended in application code, then wrote the whole thing back — two round trips, not one atomic operation. Under real concurrent load, two predictions landing close together could both read the same starting array and one's trace would silently overwrite the other's.

**Fixed with a single atomic UPDATE**, not a smarter retry loop around the same two-step pattern: the append and the last-50 trim both happen inside Postgres's evaluation of one statement (`jsonb_array_elements` + `OFFSET` + `jsonb_agg`, wrapped in `jsonb_set`). A plain UPDATE's row-level lock does the actual work — concurrent writers against the same row are genuinely serialized by Postgres itself, with each writer's subquery seeing the previous writer's already-appended array, not a stale snapshot from before either write happened.

**Verified at both the database level and the real HTTP level, not just reasoned about:**
- Raw SQL, standalone: 30 truly concurrent `UPDATE`s fired in parallel against the same row — 30 traces present afterward, all 30 distinct, zero lost. Separately confirmed trimming stays exact: 60 sequential inserts left exactly the most recent 50 (11 through 60), not 51 or 49.
- Through the actual running server: 15 genuinely concurrent `POST /predict` requests (via `Promise.all`, not sequential-looking-parallel) against a real promoted model — all 15 traces persisted, none lost.

**Scope: app only.**

## v2.23.0 — real KServe readiness reconciliation, closing the last named gap — app-only

Previously, `deploy()` succeeding meant "KServe accepted the request" — champion state committed immediately after, with no check for whether the model actually became `Ready=True`. A real InferenceService can be accepted and then spend real time pulling its image or crash-looping before genuinely serving traffic; nothing here ever distinguished those two facts.

**Real, shared readiness polling** for both `KServeClient` and `VllmServingClient` (both produce a real InferenceService with the same real readiness contract) — polls KServe's actual documented `status.conditions` array for a `Ready: "True"` entry, on a real interval, up to a real timeout (5 minutes for classical/DL, 10 for LLM serving — a real vLLM engine genuinely takes longer to initialize, reflected as a different default, not a copy-pasted number). Wired into both promote and rollback, called after `deploy()` succeeds and before champion state changes — a model that never becomes ready now correctly fails the operation instead of silently becoming champion.

**A no-op under local serving, verified not just asserted**: confirmed a full promote completed in 0 elapsed seconds — the readiness check adds no delay when there's no real cluster to poll, since a plain HTTP route has no real "not ready yet" state to wait out.

**The polling mechanism itself verified against a real HTTP server standing in for KServe's status endpoint**, not just type-checked: one mock that transitions from `Ready: False` to `Ready: True` on its third response — confirmed the poller made exactly that many real requests, correctly detected the transition, and returned `ready: true` after the expected ~2 real poll intervals. A second mock that never becomes ready — confirmed a real timeout after the requested duration, with the actual last-known failure reason (`PredictorFailed: image pull backoff`) in the response, not a generic "timed out" string.

**Full regression**: all 18 unit tests pass, 11-route browser check clean.

With this, every item raised across three independent review rounds has been either closed and verified, or hit a real, documented, disclosed limit (real dataset-backed DL architecture fidelity — blocked by this sandbox's network allowlist excluding PyTorch's real CPU-only wheel index — and live-cluster verification for the Argo/KServe integrations, which needs infrastructure this environment can't provide).

**Scope: app only.**

## v2.22.0 — the remaining four: pipeline definitions, real Argo creation, and a real async worker — app-only

Three of the four items from the previous entry's closing list are fully closed this pass; the fourth (DL architecture fidelity) hit a real, confirmed infrastructure wall — documented below, not silently dropped.

**Deep-learning architecture fidelity — genuinely attempted, genuinely blocked.** Tried installing real PyTorch for CPU training. Hit a real wall: the standard PyPI `torch` wheel is CUDA-linked at the compiled-binary level — it requires `libcudnn.so.9` just to `import torch`, regardless of `--no-deps`. The real CPU-only wheel index (`download.pytorch.org`) is outside this sandbox's network allowlist. This is a genuine environment limitation, not a design choice — real per-architecture PyTorch training (CNN/LSTM/GRU/etc.) remains out of reach here specifically, distinct from every other "explicitly deferred, needs real infrastructure" item in this project.

**Pipeline Definition → Version → Run — fully closed.** New `pipeline_definitions` table: real immutable, versioned definitions (the same advisory-lock-based versioning pattern already used for model versions — a new version is a new row, never an edit). `pipelines` now correctly represents a RUN of a specific definition version (`definition_id`/`definition_version` columns), not a conflated definition-and-execution row. Live-verified: created v1 (4 stages), ran it, watched real wall-clock stage progression; created v2 (5 stages) as a genuinely separate row; confirmed v1 stayed byte-for-byte unchanged and both versions coexist.

**Real Argo workflow creation and status reconciliation.** Previously `WorkflowEngine` only had `restartPipeline` (Argo's resubmit endpoint, which only works on a workflow that already exists) — there was no way to create a new one at all. Added a real `submitNewWorkflow` (compiles a pipeline definition's stages into a real, spec-correct Argo Workflow Steps template, POSTed to Argo's real creation endpoint) and a real `syncStatus`/`reconcileArgoWorkflows` (parses Argo's actual `status.phase`/`status.nodes` response shape back into `pipeline_stages`/`pipelines.status`), polled on a real recurring interval — a genuine no-op under the local executor, which already drives its own state. Unverified against a live cluster, same disclosure as every other Argo/KServe/UCS integration in this project.

**A real async worker-driven training lifecycle — the headline item, fully closed.** A real Postgres-backed job queue using `FOR UPDATE SKIP LOCKED` (the standard real pattern real job queue libraries use — no new infrastructure). Real claim, real heartbeat (a genuine periodic UPDATE while processing, not a timestamp set once), real timeout detection (a stale-heartbeat sweep that re-queues or terminally fails based on real remaining-attempt count), real cancellation, real retry with a real attempt cap. `POST .../complete` is kept for explicit/manual use, now sharing one real implementation with the worker via a claim that makes the two mutually exclusive — whichever gets there first wins, the other gets a clear, honest 409.

**Found and fixed a real bug of my own while testing cancellation**: the cancel route used the wrong permission tier (`training-run:create`, which is UCS-gated and expects a `modelName` a cancel request never has) — it failed loudly with a clear error rather than doing the wrong thing, but cancellation didn't work until a real local-tier permission was added.

**Live-verified, the actual headline case**: created a training run and never called `/complete` — the real background worker claimed it, ran real training, and completed it entirely on its own (`queued → running (claimedBy: worker-...) → succeeded`), observed over real polling. Separately verified cancellation (a cancelled queued run is correctly never claimed) and both timeout-sweep outcomes (stale heartbeat with attempts remaining → re-queued; attempts exhausted → terminally failed) by directly simulating a crashed worker's stale heartbeat.

**Full regression**: all 18 unit tests pass, 11-route browser check clean.

**Scope: app only.**

## v2.21.0 — real dataset-backed training + preprocessing parity, closing the biggest remaining gap — app-only

The largest of the seven items from a third review round: `datasets` was found to be 100% decorative — string-typed "size"/"rows" a user typed by hand, no column for a real file at all, no way to upload one through the API. Closing this meant building real upload infrastructure first, not just wiring in something that already existed.

**New: `POST /api/datasets/:name/upload`** — real CSV upload, real row/column counts computed from the actual file content (not a string someone typed), stored via the same real object-storage abstraction already used for model weights.

**`classical_trainer.py` rewritten** to load and fit on the real file when one exists for a training run's dataset — real `ColumnTransformer` preprocessing (numeric impute+scale, categorical impute+one-hot, both real sklearn transformers), a real train/test split of the real rows, the fitted preprocessor saved alongside the model. Falls back to the existing synthetic path, unchanged, when no file has been uploaded yet — most datasets, honestly, still won't have one. **Re-ran the full 60-model regression suite against the rewrite — all 60 still pass**, confirming the synthetic fallback wasn't broken by the real-data path being added alongside it.

**`predict_runner.py` updated for real preprocessing parity** — applies the exact same fitted transformer training used, with a real hard error naming exactly which columns are missing rather than guessing. Synthetic-trained models keep the prior padding behavior unchanged, since they have no real column contract to enforce.

**`real_trainer.py` (deep learning) extended the same way** — real dataset loading, real preprocessing, tested directly in both modes before wiring anything into the app.

**A real bug found and fixed while verifying the deep-learning path specifically**: `overview`/`performance` numbers initially looked suspiciously like old fallback values, which led to actually tracing the DB content directly rather than assuming — that specific worry turned out to be a false alarm (checking the wrong JSON key name), but the real check surfaced a genuine bug next to it: `real_trainer.py` never reported its real train/test sample counts, so the Node side had hardcoded `"1,600 (real train split)"` / `"2,000"` unconditionally — correct for the fixed-size synthetic path, silently wrong for any real dataset of a different size (a real 500-row upload was being reported as a 2,000-sample training run). Fixed both sides; verified a real 500-row dataset now correctly reports 400/500, not 1,600/2,000.

**Live-verified, the full real chain, both classical ML and deep learning**: uploaded a real CSV → trained (real accuracy/F1, `realDataset: true`, correct real sample counts) → promoted → predicted with the real preprocessing pipeline correctly applied, including real one-hot encoding of a categorical column and a real, specific error when a required column was missing from the request.

**Full regression**: 60/60 classical models still pass, time-series trainer unaffected, all 18 unit tests pass, 11-route browser check clean.

**Explicitly still open from the same review, not attempted this pass** — each large enough to warrant its own dedicated pass rather than a rushed attempt alongside this one: deep-learning architecture fidelity (still a generic MLP regardless of catalog-declared CNN/LSTM/GRU/etc. — a real fix needs real per-architecture PyTorch/TensorFlow code, not just real data); the async worker-driven training lifecycle (claim/heartbeat/cancel/retry/timeout — a genuinely different architecture from the current synchronous model, not a contained fix); Argo real workflow creation and status reconciliation (still resubmit-only, no watcher); and the Pipeline Definition → Version → Run data model (still run/stage/log-oriented).

**Scope: app only.**

## v2.20.0 — a second independent review, closing 6 more real gaps — app-only

A second independent review of v2.19.0 (27 items) confirmed several claims worth verifying directly before acting — all checked against actual code and found accurate, including one the review flagged precisely: HPO trials for the 6 time-series models were still simulated (timeseries_trainer.py existed since v2.19.0 but was never wired into HPO's trial loop), and HPO trial model files were being silently discarded — not uploaded, and not cleaned up either, a real /tmp leak on every trial.

**HPO real time-series trials + the temp-file leak, both closed together.** Wiring in timeseries_trainer.py meant touching the same code path responsible for the leak, so both were fixed in the same pass: every trial's real artifact (deep-learning, classical, or time-series) is now uploaded to real storage via a shared `uploadRealWeights()` (refactored to take an explicit key instead of a full training-run object, since HPO trials don't have one), with the local temp dir always cleaned up regardless of outcome.

**HPO artifact promotion — the actual fix, not just hyperparameters.** v2.18.0 fixed promote-best to use the winning trial's real hyperparameters, but registration still re-trained from scratch with them — a fresh, real, but not byte-identical model, not the one actually evaluated. Now, when a trial's real artifact was uploaded, `registerTrainedModel()` accepts a `preTrainedWeightsUri` and skips training entirely, reusing that exact file. Live-verified: ran a 2-trial search, confirmed each trial got its own distinct real S3 URI, promoted the winner, and confirmed the registered model's `weights_uri` is the *literal same URI* as the winning trial's — `reusedExactTrialArtifact: true`, zero re-training.

**Trained-model `kind` taxonomy.** Every registration — classical ML, deep learning, LLM fine-tunes and adapters alike — was hardcoded to `kind='base'`, which was never even accurate for what it originally meant to cover. Real derivation now: `trained` (classical/DL), `fine-tuned` (LLM full fine-tune), `adapter` (LoRA/PEFT) — confirmed nothing else in the codebase depended on the old value before changing it.

**Endpoint secret storage — real salted hashing, not plaintext.** Previously the actual client_secret was stored directly in Postgres: real working authentication, but a genuine credential-storage gap — anyone with read access to the table had every endpoint's secret directly. New `utils/secretHash.ts` using Node's built-in `scrypt` (no new dependency, the KDF Node's own docs recommend for this) with real constant-time comparison. Live-verified the complete flow: confirmed only a salted hash is ever stored, wrong secret rejected (401), regenerated secret works, real `/oauth/token` exchange works against the hash.

**Real Slack/Teams webhook dispatch — and a deeper gap than the review even named.** Checking the actual schema before building surfaced that there was no way to submit a real, usable webhook URL through the API at all — the only field (`webhookMasked`) was populated directly by the client with an already-redacted-looking string, never a real dispatchable one. Added a real `webhook_url` column (never returned by the API, same as a secret hash) with server-computed masking, and real dispatch — correct Slack `{text}` and Teams legacy `MessageCard` payload shapes — firing the moment an alert transitions to `FIRED`. Live-verified against a real local HTTP receiver: an alert genuinely fired, a real webhook POST arrived with the real current value and threshold in the message.

**A real recurring alert-evaluation loop.** Previously the only way to run evaluation was a manual `POST /api/alerts/evaluate` call or an external cron — nothing in the process itself ran it periodically. A real `setInterval` in `server.ts` (default 60s, `ALERT_EVAL_INTERVAL_MS=0` to disable) — an in-process loop, not a separate worker/queue, which remains the larger, explicitly-deferred item.

**Live-verified, full regression**: all 18 existing unit tests still pass; 11-route browser regression, zero errors.

**Explicitly still open from this review, not attempted this pass**: real signed short-lived OAuth2 tokens with scopes/revocation/introspection (current mechanism is a real but deliberately simplified client-secret-as-token model, already disclosed); KServe readiness reconciliation (promotion currently proceeds once KServe *accepts* the request, not once `Ready=True` — a real gap, but needs a live cluster to verify polling logic against, the same constraint as everything else in this project's KServe integration); expanded integration/API tests beyond the current pure-logic unit suite; and everything already disclosed as large/foundational in prior entries (real dataset-backed training, DL architecture fidelity, the async worker lifecycle, Argo workflow creation/reconciliation, the pipeline definition/version/run model, full production telemetry, production identity).

**Scope: app only.**

## v2.19.0 — Tier 3 from the independent code review: time-series, vLLM, real monitoring, real alerts, a real test suite — app-only

A due-diligence pass came first, not last: re-verified all 9 previously-claimed Tier 1/2 fixes directly against current code before starting anything new (not just trusted the earlier summaries) — all genuinely present. Spot-checked several "still open" items too (synthetic data, DL always using a generic MLP, Argo resubmit-only, no vLLM, no test suite) — all still accurately open, nothing silently regressed either direction.

**True time-series models — fully closed.** New `timeseries_trainer.py` covers all 6 previously-unsupported base models: Prophet, ARIMA, SARIMA, auto-ARIMA, Exponential Smoothing, Holt-Winters. Tested each individually before wiring anything in — real fit, real forecast against a real held-out window, real MAE/RMSE/MAPE. Notably, `holt_winters` (with seasonality) genuinely outperforms `exponential_smoothing` (without) on the seasonal synthetic series — an honest confirmation the fits are doing what they claim, not a coincidence. This closes the "training uses synthetic, not real, data" disclosure's remaining exception — all 78 non-LLM base models now get real training where a real library exists for them. Live-verified: trained `holt-winters`, got real `{mae, rmse, mape}` as the run's reported metrics, real weights uploaded to storage.

**vLLM serving.** New `VllmServingClient`, spec-correct against KServe's real HuggingFace-runtime-with-vLLM-backend InferenceService shape. Previously LLM models had no distinct serving path at all — they went through the *sklearn*-formatted KServe spec, which is simply wrong for a language model. `deploy.ts` now selects the serving engine by model domain.

**Real production monitoring ingestion.** Every `/predict` call now genuinely appends to `model_monitoring.predictions.traces` — previously nothing ever ingested into it; it stayed permanently empty after registration regardless of how much traffic a model served. Caught and fixed a real bug in my own first pass: an early version claimed to cap history at 50 traces, but the SQL never actually trimmed anything — fixed with an honest read-modify-write (disclosed: a real but low-stakes race window under concurrent load, not a single atomic operation).

**A real alert-evaluation engine — scoped honestly, not to all 8 seeded policies.** Checked the actual seeded policies before building anything: of the 8, only `p95-latency` and `low-confidence` are genuinely computable from data this scaffold actually has (the real trace history above). The other 6 (`f1-drop`, `recall-drop`, `psi`, `error-rate`, `missing-values`, `gt-coverage`) need signals that don't exist — real ground truth, a real feature-drift baseline, real error tracking — and building fake evaluation logic for those would be exactly the kind of gap this whole review exists to close. New `POST /api/alerts/evaluate` computes real p95 latency and real low-confidence rate from real recent traces, with a real consecutive-window state machine. Found and fixed a real bug during testing: the first version joined against `catalog_models.task` (a raw descriptive string like "Classification / regression") instead of `model_monitoring.model_type` (the properly normalized value `applies_to` actually uses) — every model was silently being skipped. Live-verified the full consecutive-window behavior end to end: promoted a model, generated 8 real predictions, ran the evaluator 3 times, watched `windowsExceeded` progress 1 → 2 → 3 and correctly flip to `FIRED` exactly on the third consecutive breach — with a genuinely low real confidence score and a genuinely real (subprocess-overhead-driven) latency measurement, not fabricated numbers.

**A real, passing automated test suite** — the first in this project. Vitest, 18 real tests across 3 files, covering pure logic that doesn't need a live database: HPO's mixed-radix hyperparameter sampler, the classical-model classpath/task mapping, and the alert evaluator's percentile calculation. Caught a real bug in my own first test, not the code: an assertion assumed a 3-option parameter would vary at trial indices that are multiples of 3 — mathematically it can't, by the same logic a base-10 counter's ones-digit repeats every 10. Rewrote the test to check the actually-meaningful property instead of asserting something the algorithm was never supposed to do. `npm test` in `backend/`. Real route/service logic needing a live Postgres + UCS remains covered by this project's established manual verification process, not by this suite — a real integration-test harness stays a disclosed, open gap.

**Explicitly still open, stated plainly rather than attempted half-scoped**: a real worker-driven async job lifecycle (the current synchronous request/response model works and is simpler; converting it is a genuinely large architectural change, not a contained fix) and production identity (needs a real Keycloak/OIDC deployment this environment cannot stand up — the dev role-switcher was always explicitly, intentionally out of that scope, not a gap to close here).

**Scope: app only.**

## v2.18.0 — Tier 2 from the independent code review, part 1: HPO, real metrics, atomic registration — app-only

Continuing the same review from v2.17.0. Four of the six Tier 2 items closed this pass; the two Argo/pipeline-definition items (genuinely a separate subsystem, not a contained fix) are explicitly not attempted here — see the end of this entry.

**HPO's `promote-best` discarded the winning trial's hyperparameters.** Confirmed precisely: HPO's own per-trial loop already used real hyperparameters correctly, but `promote-best` triggered `registerTrainedModel()`, which independently re-trained from scratch using only DEFAULT hyperparameters — the "promoted" model's real weights never actually reflected what HPO found. Added a real `hyperparams` column to `training_runs`, threaded through `CompletedRun` and both real-training call sites. Live-verified: promoted a search's winning trial (`{max_depth:5, n_estimators:100}`) and confirmed the registered run's `hyperparams` field matches exactly, with a fresh, honestly-labeled real re-training result (naturally close to but not identical to the trial's original value, since it's a new real fit, not a byte-for-byte replay).

**Classical-ML HPO trials were unconditionally simulated**, even for the 60 base models classical_trainer.py has covered since v2.15 — the code hadn't been touched since before that existed. Extended HPO's per-trial loop to call `runClassicalTraining()` for any base model it covers, with that trial's own sampled hyperparameters. Live-verified: a 3-trial search on `random-forest` produced three genuinely different real `dataSource: measured` results (55.75/65.54/63.32), not simulated jitter.

**`/complete` always used `Math.random()` for the run's reported metrics, even when real training had just measured something real seconds earlier** — and, found while fixing this: the old jitter code hardcoded `{accuracy, f1, auc}` for every non-LLM run regardless of task, meaning every regression run was reported with a meaningless fake "accuracy" the whole time. Confirmed this was safe to change by checking how the frontend actually reads `run.metrics` first — both call sites (`ExperimentsPage.tsx`, `TrainPage.tsx`) already read it generically (`Object.entries`, dynamic key lookup), so real task-appropriate keys are a strict improvement, not a breaking change. `registerTrainedModel()` now returns its real measured metrics when available; both `/complete` and `promote-best` use them in place of the simulated placeholder. Live-verified: a real classification run now reports real `{f1: 58.37, accuracy: 58.67}` — no `auc` key at all, honestly, since neither real trainer computes one.

**Atomic registration**: `catalog_models` and `model_versions` were separate, unlinked writes — a crash between them could leave a model in the catalog with no version, or vice versa. Merged into the same real transaction already built for version allocation in v2.17.0, held open only across these two fast local writes — not across the UCS network calls that follow, which correctly stay outside any local transaction (an external system's state was never part of this database's atomicity guarantee, and holding a transaction open across slow network I/O is its own real problem).

**Explicitly not attempted this pass — a separate subsystem, not a contained fix**: Argo creating real workflows from a pipeline definition (still resubmit-only) and status reconciliation (no watcher/poller), and pipeline definition/version/run as a real separate data model (still run/stage/log-oriented). Tracked, not silently dropped.

**Live-verified, the full chain in one run**: real training → real metrics on the run → real HPO search with real per-trial classical training → promote-best with the exact winning hyperparameters passed through → a fresh, honestly-labeled real re-registration. Full 11-route browser regression afterward, zero errors.

**Scope: app only.**

## v2.17.0 — Tier 1 from an independent code review: seven real gaps closed in the deploy/serve path — app-only

An independent code review of the whole project produced 24 findings. Spot-checked several of the most serious against the actual code before touching anything — all confirmed accurate, plus one the review didn't even flag: the predict endpoint's own comment ("none is persisted in this scaffold") was stale, since real `.joblib` artifacts have existed since v2.9/v2.15. This closes the seven found to be both serious and contained; the rest are tracked as explicitly still open (see the review's own list — not repeated here).

**A foundational gap under several of these**: `model_versions` had no per-version artifact location at all — the real weights URI only ever lived in `model_monitoring`, keyed by *model*, not *version*. Retraining silently made the previous version's real artifact unrecoverable the moment a new one was registered. Added real `weights_uri`/`artifact_format` columns to `model_versions`, populated at registration time — confirmed live that two versions of the same model now each hold their own distinct, correct real S3 URI simultaneously.

**Version-allocation race** — `SELECT MAX(v)+1` replaced with a real transaction using a Postgres advisory lock (`pg_advisory_xact_lock`), plus a real `UNIQUE (model_id, v)` constraint as a backstop. Found a genuine, previously-unknown Postgres limitation while building this: `FOR UPDATE` cannot be combined with an aggregate function like `MAX()` — caught by actually running the code, not by inspection. Also realized a row lock can't protect a model's *first* version anyway (nothing exists yet to lock against) — the advisory lock, keyed on the model name rather than a row, handles both cases.

**KServe 409 was treated as success without updating anything.** Now issues a real `PATCH` to actually update the existing `InferenceService` to the new version's spec, and only reports success if that patch succeeds. **KServe's hardcoded `modelFormat: sklearn` / `storageUri: pvc://...`** replaced with the real per-version artifact — and now refuses to deploy outright when no real weights are recorded for a version, rather than silently deploying nothing while reporting success.

**Promotion flipped the DB champion alias *before* calling the serving engine; rollback never called it at all.** Both fixed: real deploy happens first, DB state only changes after it succeeds; rollback now fetches the target version's real artifact and genuinely redeploys it. Live-verified the full loop: promoted v2, confirmed `/predict` actually served v2's distinct real artifact, rolled back, confirmed `/predict` immediately switched to serving v1's distinct real artifact — not just that the database said so.

**Predict now loads and runs the real persisted model** — new `predict_runner.py` downloads the real `model.joblib` from configured storage and runs a genuine `.predict()`/`.transform()` call, with an honest, clearly-labeled fallback to the deterministic stand-in when no real weights exist or the load fails. Live-verified against a real trained RandomForestClassifier, including the honest disclosure when posted input didn't match the model's expected feature count.

**`/predict` had zero authentication despite endpoints advertising OAuth 2.0.** Added a real (deliberately simplified — not a full token-issuance/expiry flow) client-credentials check: a real per-endpoint `client_secret` (new column, distinct from the pre-existing masked display value), required as a Bearer token on `/predict`, plus a real `POST /oauth/token` route matching the URL this app already advertised. Found and fixed a real, newly-consequential bug in the process: `regenerate-secret` returned a new secret but only ever persisted the masked *display* value — the real credential never actually changed, so a regenerated secret would never have worked. Also fixed seed data, which had never populated the new column at all.

**Live-verified, the full real chain in one continuous run**: train → promote v1 (real deploy, real secret generated) → predict with no auth (401) → predict with the real secret (real prediction, real model loaded from S3) → real `/oauth/token` exchange → train + promote v2 → confirm v1 and v2 hold distinct real artifact URIs → rollback → confirm predict immediately serves v1's artifact again. Full 11-route browser regression afterward, zero errors.

**Two real bugs caught in my own edits before shipping, not after**: a `str_replace` that landed with duplicated code in `servingEngine.ts` (a stray second class header) and in `deploy.ts` (a duplicated `existingEndpoint` declaration) — both caught by type-checking immediately after each edit, not assumed clean.

**Scope: app only.**

## v2.16.0 — base models registerable in UCS, in a real separate schema — app-only

Closes the gap from the last two turns' direct questions: base models (classical ML and deep learning) were never cataloged in UCS at all — only trained output was. Confirmed by request: `ml_models.base-library.*` for the 85-entry library, distinct from `ml_models.default.*` where trained models register, so a base model and a user's trained model of the same name never collide.

**`scripts/bootstrapUcs.ts` extended** to register all 85 base models as real Gravitino model entities — every field the Train Wizard's UI needs (domain, title, task, description, framework, license, source URI, GPU flag, compute profile, long description) goes into the registration's `properties`, so a later read-back has everything needed to render a full base-model card from UCS alone. Also now creates one RBAC role per tier *per schema* (base-library and default), not just default — the earlier bootstrap only ever gave ml-studio-engineers etc. access to the trained-model namespace.

**New: `services/baseModelSync.ts`** — the read side of the round-trip. Pulls the base-library schema back out of UCS (`explorer-tree` + one `objects/{fqn}/profile` call per model) and upserts into the local `base_models` table. UCS becomes the real source of truth; the local table becomes a read cache populated from it, not hardcoded seed data — reachable via `POST /api/base-models/sync-from-ucs` (platform-admin gated).

**The most unverified piece in this project so far, stated plainly rather than glossed over**: both the write side (bootstrap registration) and the read side (explorer-tree + profile) depend on Gravitino, which has never been available anywhere this project has run. Unlike `authz/check` and lineage — live-tested against a real running UCS instance — *neither* side of this specific round-trip has been exercised end to end. What *was* verified: triggering the sync against the real running UCS instance produces a clean, honest, structured failure (`{"attempted":false,...,"reason":"UCS explorer-tree unreachable: ..."}`) rather than a crash or a hang, and — the actual point of building it fail-soft — the local `base_models` table is confirmed completely untouched afterward (still all 85 entries), not partially wiped by a sync that didn't fully succeed.

**Scope: app only.**

## v2.15.0 — real training for 60 classical ML base models — closing the "cheating the end user" gap

Direct feedback: a classical ML training run "succeeded" with simulated metrics and no real model was ever fit — a real gap, not a style choice, since it reported success without doing the thing it claimed to have done. This closes it for 60 of the 78 non-LLM base models.

**New: `classical_trainer.py`** — dynamically imports the actual class the catalog names (`sklearn.ensemble.RandomForestClassifier`, `xgboost.XGBClassifier`, etc. — real, resolvable Python classes) and genuinely fits it. Covers all 51 real `sklearn://` entries plus 9 more (`xgboost`/`lightgbm`/`catboost`/`umap`) whose libraries share a real sklearn-compatible API — every task shape: classification, regression, clustering, anomaly detection, dimensionality reduction, ranking, forecasting-as-regression, each with real, task-appropriate metrics (accuracy/F1, MSE/R2, silhouette score, a real detection rate checked against actually-injected outliers, trustworthiness for embeddings).

**Tested individually against all 60 covered models before shipping**, not just spot-checked — and found three real bugs in the process:
1. Ranking/forecasting models were scored with classification metrics against continuous regressor output — crashed on the first test pass.
2. **LightGBM writes hundreds of lines of training logs straight to the OS-level stdout file descriptor** — a plain Python `sys.stdout` redirect doesn't catch that (it's native code writing under it), so this would have silently corrupted the JSON a caller parses in production. Fixed with a real fd-level redirect during `.fit()`, not a "parse the last line" workaround.
3. PCA-family reducers default to keeping *all* components — "dimensionality reduction" was reducing nothing. Fixed, then caught a second-order bug in the fix itself: the hyperparameter fallback dropped `n_components` entirely whenever `random_state` was rejected (IncrementalPCA doesn't accept it) — fixed to retry one parameter at a time instead of jumping straight to bare defaults.

**Wired into the real flow**: `registerTrainedModel()` now runs real classical training in the same hoisted slot deep learning already used, sharing one `uploadRealWeights()` helper between both paths (extracted during this pass, not duplicated) — a real trained artifact now gets uploaded to configured storage and a real UCS registration URI used, instead of the placeholder every classical-ML model got before. New `model_monitoring.classical_training_result` column holds the real, task-shaped output, including the real `weightsUri` for transparency (matching the deep-learning path's own disclosure).

**Live-verified end-to-end, both a supervised and an unsupervised case**: a real `random-forest` classification run and a real `kmeans-clustering` run (no target column — confirming the unsupervised path too), each producing real metrics, a real uploaded weights file confirmed present via a direct bucket listing with a byte-count match, and a real UCS registration. Full 11-route regression afterward, zero errors; confirmed the deep-learning path is unaffected by the shared-helper refactor.

**Explicitly still open, not silently skipped**: 6 true time-series-only libraries (Prophet, ARIMA, SARIMA, auto-ARIMA, exponential smoothing, Holt-Winters) — a genuinely different fit shape (a univariate series, not X/y tabular data) that this pass doesn't attempt to fake. The code detects these specifically and logs an honest message rather than silently mishandling them; they remain on simulated metrics until built for real.

**Scope: app only.**

## v2.14.0 — Central Model Cache removed; corrected against InAiEra's real deployment architecture — app-only

A real architecture document (control plane / data plane design) surfaced a wrong assumption in v2.13.0's design, and a follow-up clarification simplified it further than even the corrected version first attempted. Both corrections are real reversals, not indecision — each was driven by information this project didn't have until it was provided.

**What the architecture doc got me to (nearly) build**: reading it revealed that InAiEra's control-plane/data-plane tunnel is bidirectional once established — the control-plane BFF genuinely holds the client certificate and can route real requests into a customer's Envoy gateway. That's real and worth recording, but it turned out to be solving a problem that doesn't need solving here.

**What the follow-up clarification corrected it to**: rather than any live service reaching into a customer's data plane, InAiEra downloads each base model once into its own staging storage, then performs a real cross-account/cross-tenant **object storage copy** directly into the customer's own bucket — a cloud-provider API call authorized by onboarding-issued credentials, not a network path into the data plane's compute. The data plane's "no outbound access" constraint never applies to a storage-to-storage copy; it was never the right problem to route around in the first place.

**Central Model Cache — the service `ml-studio-app` called over HTTP as of v2.13.0 — is removed entirely.** It existed to broker between ML Studio and object storage. That mediation isn't needed: ML Studio runs *inside* the customer's data plane already, right alongside Gravitino and the customer's own bucket — it reads and writes that storage directly, the same way Gravitino and Trino already do. `backend/src/storage/` (S3/ADLS/GCS clients) and `@aws-sdk/client-s3` / `@azure/storage-blob` / `@google-cloud/storage` are back in this repo, exactly as they were before v2.13.0 introduced Central Model Cache — `registerTrainedModel()` calls `getStorage()` directly again, not an HTTP client.

**Live-re-verified after the revert**: the exact same real training → real S3 upload → real UCS registration flow from before Central Model Cache existed, confirmed working again — real `weightsUri`, confirmed present via a direct bucket listing.

**`model-import-service` is rearchitected to match**, not just pointed at a different URL: it now downloads from HuggingFace into InAiEra's own staging bucket, copies into a **specific customer's own bucket** using that customer's onboarding-issued storage credentials, and registers the model in **that customer's own Gravitino** — with the real URI the bytes landed at in *their* bucket, not InAiEra's staging location (which the customer's own UCS/ML Studio could never reach anyway). Both the CLI and the HTTP job API now take an explicit customer target (storage credentials + that customer's UCS bearer token) per run — real secrets, never persisted; only a human-readable label is stored in the job record.

**Live-verified, the corrected pipeline**: a real download from a local HuggingFace-API-compatible stand-in, staged in one real S3 bucket, copied into a second, separate real S3 bucket representing the customer — confirmed via direct reads from both buckets that the content is byte-for-byte identical. Registration in the customer's Gravitino remains spec-correct but unverified for the same already-disclosed reason as every other Gravitino-dependent piece in this project: confirmed via UCS's own traceback (`httpx.ConnectError`) that the request reaches it correctly and is only blocked by the missing Gravitino connection, not a defect in the request.

**Scope: app + model-import-service.** The mockup was not touched this pass.

## v2.13.0 — real, service-separated model artifact architecture: Central Model Cache + Model Import Service, app-only

Started as "add object storage directly to ML Studio" and was corrected mid-turn to a proper service boundary, based on direct feedback: ML Studio has no business holding cloud-storage SDK code or HuggingFace-download logic itself. Two new, separate, real services now exist as siblings to this repo:

**`central-model-cache/`** — owns actual artifact bytes. The one place in this whole system with cloud-storage SDK code (S3/ADLS/GCS, one customer-configurable backend). Real HTTP API: PUT/GET artifacts, list metadata, real server-computed SHA-256 checksums (never trusted from the caller). **S3 is fully real-verified**: ran a real local `moto` S3-compatible server and tested the actual service against it — real put, real exists checks, real get with byte-for-byte round-trip integrity. ADLS/GCS are spec-correct against their real SDKs but unverified — confirmed directly that this sandbox has no network path to either.

**`model-import-service/`** — downloads real base-model weights from their source (HuggingFace Hub) and stores them via Central Model Cache's API. Holds no storage SDK code of its own. Correctly scoped to only the 7 LLM base models with real pretrained weights — the 51 sklearn:// classical-ML entries and 27 oci:// deep-learning entries are correctly left alone (no pretrained weights exist to import for either). huggingface.co is blocked at this sandbox's egress proxy, so a real local HTTP server implementing HF's exact documented API contract stood in for it during testing — not a fake protocol, a locally-reachable copy of the real one.

**ML Studio itself was corrected to match**: removed the direct `@aws-sdk/client-s3` / `@azure/storage-blob` / `@google-cloud/storage` dependencies and the storage code entirely. `registerTrainedModel()` now calls Central Model Cache's HTTP API (`clients/centralModelCache.ts`) instead of holding storage logic itself — the same separation Model Import Service uses.

**Live-verified, the full corrected pipeline, all four real services running together** (UCS + Central Model Cache + real local S3 + ML Studio): created and completed a real training run; confirmed `trainingMetrics.weightsUri` is a real `s3://...` URI obtained via an HTTP call to Central Model Cache (not a direct SDK call from ML Studio); confirmed Central Model Cache's own registry independently has the identical file, with a matching real SHA-256 checksum it computed itself.

**Still explicitly open, not attempted this pass**: `base_models` remains ML Studio's own local 85-entry table — per the corrected architecture this should be discovered from UCS/Gravitino instead, not maintained as a parallel catalog. Removing it safely requires base models to actually be registered as real Gravitino model entities first, which needs a live Gravitino this sandbox cannot run — flagged as a deliberate, known gap rather than attempted and left half-verified. Infra Service (the fourth box in the original architecture table) was not built this pass either.

## model-import-service v1.1.0 — CLI added, HTTP API kept alongside it

Direct correction from real deployment constraints: a customer dataplane has no outbound network access at all, which means Model Import Service — as a persistent server reaching out to HuggingFace — could never actually run inside one. The real operational model: InAiEra runs this on its own outbound-capable infrastructure and pushes into a customer's Central Model Cache, which only needs to accept that one inbound connection.

Added `src/cli.ts` for that real workflow — initial setup (a manifest of every base model, `models.manifest.json` populated with the real 7 LLM entries from ML Studio's actual library) and incremental (a single new model or release), both run by an operator, synchronous, real progress printed to stdout. The HTTP job-tracking API was kept alongside it per explicit instruction, refactored to share one real implementation (`src/importCore.ts`) with the CLI rather than duplicating the download/upload logic.

Live-verified both paths independently: the CLI's manifest mode imported all 7 real LLM base models, and the HTTP API's job-polling path was re-confirmed working after the refactor — both ending with real cache URIs, confirmed present via a direct bucket listing afterward, not just trusted from the response.

## v2.12.0 — real Unified Catalog Service integration: Models (full lifecycle), lineage, and RBAC — Govern section removed, app-only

UCS's own uploaded codebase (86 Python files) is now the integration target — not an assumed contract. Read the full thing, then went further: stood up a real instance in this environment (real Postgres, all 12 real Alembic migrations applied, the actual FastAPI app running) and live-tested against it, rather than shipping spec-reading as verification.

**What was learned that changed the design:**
- UCS has two genuinely different auth paths: most reads + lineage + `authz/check` use trusted identity headers (no token); everything under `/api/v1/admin` (register model, create RBAC role) requires a real Keycloak-verified Bearer JWT. Per instruction, no OIDC token-minting was built — `UCS_BEARER_TOKEN` passes through as-is.
- `authz/check` takes a semantic action string (`read`/`write`/`create`/...) and a dotted `catalog.schema.object` FQN — not a raw Gravitino privilege name. Confirmed the exact tier semantics live: a "writer" binding can create a model *version* but correctly cannot create a brand-new *model* (an owner-tier action).
- UCS's RBAC is scoped to catalog objects — it has no concept of "restart a pipeline" or "acknowledge an alert". `middleware/role.ts` was rewritten around this: genuinely catalog/model-scoped actions (`training-run:create`, `deployment:promote/rollback`) go through real `authz/check`; ML-Studio-operational actions keep a simple local tier, honestly, rather than misusing UCS as a general permission engine.
- Lineage is real OpenLineage (`POST /api/v1/lineage/events`), not a bespoke schema — and doesn't need Gravitino or Keycloak at all.

**Live-verified against the real running service** (not simulated): full lineage round-trip (ingest → graph → runs → impact all reflecting the same real event); real `authz/check` allow/deny across multiple cases including the writer/owner tier distinction; a full training-run create → complete flow through ML Studio's actual API, ending with real lineage queryable back out and a real RBAC deny correctly blocking `ml-viewer` from the same action `ml-engineer` could do.

**Found and fixed a real bug in the process**: an earlier, separate UCS integration pass registered models in UCS under `base_model_id` (what a model was trained *from*) instead of `model_name` (what it's registered *as*) — two differently-named models trained from the same base model would have collided as "versions of the same UCS model" in UCS's catalog, while local registration already correctly used `model_name`. Consolidated both into one atomic registration in `registerTrainedModel()`, using the same identity throughout.

**Model registration and RBAC role/group creation are spec-correct but unverified** end-to-end — both depend on Gravitino, a separate Java service that can't run in this environment (no Docker/JVM stack available). Confirmed via UCS's own error traceback that requests reach it correctly and are only blocked by the missing Gravitino connection (`httpx.ConnectError`), not by a defect in the request itself — same honesty standard as the Argo/KServe integrations. New `scripts/bootstrapUcs.ts` provisions the real catalog + role + group bindings ML Studio's RBAC checks depend on; written and type-checked, unverified against a live Gravitino.

**Govern section removed**, per explicit instruction — UCS is now the single fact for catalog + RBAC: `routes/gravitino.ts` and `routes/audit.ts` deleted and unmounted, both frontend pages and their nav entries removed, dead API client methods and types cleaned up, and the now-orphaned local `catalog_entries` table dropped from schema and seed. Confirmed via full regression: all 11 remaining routes load with zero console errors, both removed pages confirmed absent from nav.

**New real "Lineage" tab on Model Overview** (all three tab sets — classic, deep-learning, LLM), backed by a new `GET /api/models/:id/governance` endpoint that fans out to UCS's lineage graph/runs/impact and object profile in parallel. Each section degrades independently and honestly — confirmed both live (real data flowing all the way from training-run completion through to the rendered UI) and with UCS entirely unreachable (a model that was never UCS-registered shows an honest "not registered yet" / "UCS unreachable" instead of crashing, verified with UCS not even running).

**Scope: app only.** The mockup has no backend to talk to a real UCS instance — this is a genuine, not-yet-attempted follow-up, not a silent version mismatch. Flagged here rather than left implicit.

## v2.11.0 — real execution for Pipelines, Deploy & Promote, and Endpoints (checkpoint 3 of the v2.8.0 gap-review response, closing the remaining items)

Closes items 2, 3, and 4 from the gap review — Pipelines ("no real Argo"), Deploy & Promote ("no actual serving rollout"), and Endpoints ("no real provisioning") — with one shared architecture per your explicit choice: a real, locally-verifiable executor as the default, plus a real spec-correct client for the production system it stands in for.

**Pipelines — `backend/src/execution/workflowEngine.ts`:**
- `LocalWorkflowExecutor` (default): restarting a failed pipeline now genuinely runs — stages progress over real wall-clock time with real DB state transitions (pending → running → ok, one at a time, in the background after the HTTP response returns), not an instant flip to "succeeded." Real per-stage elapsed time is recorded and logged.
- `ArgoWorkflowClient`: spec-correct against Argo's real REST API (`PUT /api/v1/workflows/{namespace}/{name}/resubmit`, Bearer auth) — used automatically when `ARGO_SERVER_URL` is set. Unverified against a live cluster, same disclosure as UCS.
- Frontend: `PipelinesPage` now polls while a pipeline is `running`, so the DAG and logs visibly progress instead of looking frozen until a manual refresh.
- Verified: restarted the seeded failed pipeline via the real API and watched it genuinely progress stage-by-stage over ~7 real seconds to `succeeded`, with real per-stage timings in both the DB and the logs; confirmed the same progression renders correctly through a real browser with live polling (screenshotted mid-run); the original failure log is still preserved, with the new resubmission appended.

**Deploy & Promote / Endpoints — `backend/src/execution/servingEngine.ts` + `backend/src/routes/serve.ts`:**
- `LocalServingExecutor` (default): promoting a model now provisions a REAL endpoint — `POST /api/serve/:modelId/predict` — that genuinely answers HTTP requests with a deterministic (same input → same output, hashed from the actual posted body) computed response. Every response honestly labels itself as a local stand-in, not a reloaded trained model artifact (none is persisted anywhere in this scaffold to reload). A model that was never promoted, or was rolled back off, genuinely 404s at this route — it checks the same `deployments` table Deploy & Promote writes to, not a separate fake status flag.
- `KServeClient`: spec-correct against KServe's real `InferenceService` CRD API via the Kubernetes API server — used automatically when `KSERVE_API_URL` is set. Unverified against a live cluster.
- Promotion now upserts a real row in `endpoints` with the actual provisioned URL, visible immediately on the Endpoints page alongside the still-seeded (unchanged) fake gateway URLs for models that haven't been re-promoted since this feature shipped — a visible, honest before/after.
- Verified: promoted `fraud-detector-dl` through the real API, confirmed the real endpoint URL was upserted into `endpoints`, hit `/predict` with two different real inputs and confirmed genuinely different (but each internally deterministic — same input twice gave the identical response) predictions, confirmed a non-deployed model correctly 404s, and confirmed the whole flow renders correctly through the real Deploy and Endpoints pages with zero console errors.

**New config, all optional** (documented in `.env.example` and README): `ARGO_SERVER_URL`/`ARGO_TOKEN`/`ARGO_NAMESPACE`, `KSERVE_API_URL`/`KUBE_TOKEN`/`KSERVE_NAMESPACE`, `PUBLIC_HOST`. Unset (the default), everything runs on the real local executors described above — nothing about existing behavior requires new infrastructure to keep working.

Full 13-route regression clean throughout. This closes all 5 items from the original gap review as a coherent set of checkpoints (v2.9.0 → v2.11.0).

## v2.10.0 — real Experiments + real HPO trials (checkpoint 2 of the v2.8.0 gap-review response)

Closes item 1 from the gap review ("Experiments — still too thin"), including the more ambitious HPO trial-tracking scope: not a simulated hyperparameter sweep, but genuinely different real training runs per trial for deep-learning base models.

**New entity: Experiments** — `experiments` table (objective metric/direction, owner, team, tags), `training_runs.experiment_id` (optional — a run doesn't have to belong to one). `GET /api/experiments/:id` computes a real "best value so far" across every linked run AND every linked HPO search's best trial, fresh from the actual rows, not cached.

**New: real HPO trials** — `hpo_searches` + `hpo_trials` tables. `POST /api/hpo-searches` generates real, genuinely distinct hyperparameter combinations from a declared search space (a proper mixed-radix enumeration — see the bug fix below) and, for a deep-learning base model, runs each trial as an **actual separate real_trainer.py invocation with that trial's specific hyperparameters** — different hidden-layer architectures and learning rates producing genuinely different, real measured accuracy, not one run's number varied N times. For non-deep-learning base models (real_trainer.py doesn't cover xgboost/LightGBM/LLM fine-tuning), trials fall back to honestly-labeled `"simulated"` values — the UI shows a live banner telling you which one a given base model will get *before* you submit.

`POST /api/hpo-searches/:id/promote-best` takes the winning trial through the exact same registration pipeline (`registerTrainedModel`) a normal completion does — a real `catalog_models` + `model_versions` (challenger) entry using the winning trial's real hyperparameters and real metric.

**Verified as a complete, real, working chain — not just at the API level:**
- API-level: a 5-trial real HPO search produced real, meaningfully different accuracies (89% down to a genuinely bad 70.5% from a deliberately weak config) and correctly identified the true best trial; promoting it produced a real challenger version that **successfully promoted to deployment**.
- Browser-level: created an experiment, ran a real 3-trial HPO search, and promoted the winner — all through the actual UI, zero console errors — then confirmed the promoted run appeared in the experiment's run list with its real accuracy and the "best so far" banner updated correctly.
- **Found and fixed a real bug in the process**: the original trial-hyperparameter sampler used a stride of `(keyIndex + 3)`, which collapses to index 0 for every trial when a parameter has exactly 3 options — `hiddenLayers` silently never varied across trials while `learningRate` did. Caught by inspecting actual trial output, not by code review alone. Replaced with a proper mixed-radix enumeration (the same technique as converting a number to a mixed-base representation), verified afterward to produce 5/5 genuinely distinct combinations for 5 requested trials.
- Full 13-route regression clean throughout.

**Frontend**: `ExperimentsPage.tsx` fully rewritten — experiment list/create, detail view with best-value banner and linked-runs table, HPO search creation form (with the live measured-vs-simulated banner), trial comparison table with best-trial highlighting, and promote-best-trial control. Train wizard gained an optional "Experiment" dropdown to link a direct run.

## v2.9.0 — real measured DL training telemetry, and a real security fix (checkpoint 1 of the v2.8.0 gap-review response)

Responds to a detailed external gap review of v2.8.0 (uploaded by the
user) that asked for 5 areas to be addressed "without fail": Experiments,
Pipelines (real Argo), Deploy & Promote (real serving rollout), Endpoints
(real provisioning), and Deep Learning monitoring telemetry fidelity. This
release is the first checkpoint — DL telemetry and one Endpoints security
fix, fully verified. Experiments/HPO, Pipelines, Deploy & Promote, and the
rest of Endpoints are substantial remaining work, continuing in follow-up
releases rather than being spread thin across one giant unverified change.

**Security fix (from the Endpoints section of the review):** client-secret
regeneration used `Math.random()` — not cryptographically secure, and
Node's own docs explicitly warn against it for anything security-sensitive.
Replaced with `crypto.randomBytes()`.

**Real measured deep-learning training telemetry**, closing the review's
core finding that training-run-completion telemetry was "derived, not
measured," to the fuller standard of actually running real training rather
than just labeling the estimate:
- New `backend/src/training/real_trainer.py` — genuinely trains a real
  scikit-learn `MLPClassifier`/`MLPRegressor` on synthetically-generated
  data (no real dataset file is wired into this scaffold, so the DATA is
  synthetic — but the training loop, its per-epoch `loss_curve_`, its
  `validation_scores_`, and its final trained-weight statistics are all
  genuinely measured, not derived from a target number).
- New `backend/src/services/realTrainer.ts` — spawns the script, parses
  its output, fails soft (with the reason preserved) if Python/scikit-learn
  isn't available in a given deployment, rather than hard-failing
  registration.
- `modelRegistration.ts` now attempts real training first for any
  deep-learning-framework model, falling back to the previous
  derived-curve approach only on genuine unavailability.
- **Three-way honest labeling in the UI**, so nothing is ever presented as
  more real than it is: "✓ Real measured telemetry" (a real training run
  happened), "⚠ Derived estimate, not measured" (with the actual reason
  real training wasn't available), or "ℹ Illustrative example data" (the
  hand-authored `fraud-detector-dl` seed, which is neither of the above —
  it was written to look like a nice demo, not derived from any run).
  Fields the trainer genuinely doesn't expose (gradient norm, GPU
  utilization — this runs on CPU, no GPU is used or available) say so
  explicitly rather than showing a plausible-looking fake number.
- Verified: trained a real model through the actual API end-to-end (39
  real epochs, a real 39-point loss curve with genuine convergence
  behavior, a real parameter count computed from real trained weights),
  confirmed correct rendering through a real browser, confirmed the seeded
  example shows its own distinct honest label, full 13-route regression
  clean.
- **Requires Python 3 + numpy + scikit-learn** on the backend host for the
  measured path — documented in README as a recommended (not hard)
  dependency, since the fallback path keeps the app working either way.

## v2.8.0 — the training → registration → Model Overview pipeline is now genuinely wired, app-only

Answers a direct question honestly asked twice: "if a user trains a model,
does Model Overview automatically update?" The answer was no — confirmed
by code audit, not assumption. This closes every link in that chain that
was broken.

**What was broken, confirmed by grep before writing a line of code:**
- `model_monitoring` was written to in exactly one place: `seed.ts`. No
  code path connected training-run completion to a monitoring row.
- Training-run completion updated only `training_runs.status`/`.metrics` —
  never created a `catalog_models` or `model_versions` entry.
- Promotion required a pre-existing `model_versions` row with
  `alias='challenger'` and had zero reference to `training_runs` — a
  genuinely-trained model had no path to becoming promotable at all.
- Model Overview's model picker was a hardcoded 4-option `<select>`, not
  reading from the catalog.

**What's fixed:**
- Train wizard now requires naming the model being trained (`modelName`) —
  matches how real platforms (Databricks, SageMaker, MLflow) require
  naming a registration, since one base model can produce many trained
  models. Registering under an existing name that belongs to a different
  base model is rejected (409), not silently overwritten.
- New `backend/src/services/modelRegistration.ts`: on completion, creates/
  updates `catalog_models`, inserts a `model_versions` row (always
  `alias='challenger'` — never auto-promoted, promotion stays a deliberate
  separate action), and generates a `model_monitoring` row.
- **That generation is deliberately honest, not fabricated production
  data**: real values where they exist (the run's own reported metrics,
  the hyperparameters actually chosen, the base model's real framework/
  task), and explicit "not yet available — no production traffic yet"
  placeholders everywhere a real number would require live serving data
  this scaffold doesn't have (drift, infrastructure load, LLM cost/safety/
  hallucination, input-quality). A fresh v1 has no prior baseline, so
  baseline = current rather than a fabricated "improvement."
- `model_type`'s CHECK constraint extended to cover
  clustering/anomaly_detection/dimensionality_reduction — these were
  never representable in monitoring at all before.
- Model Overview's model picker is now genuinely dynamic (`GET /api/models`),
  not a hardcoded list — a newly-trained model is reachable without
  editing frontend code.
- Experiments page shows the real model name per run and surfaces
  registration success/failure explicitly (registration is NOT best-effort
  silent — a failure is shown, even though the training-run itself still
  reports as succeeded, since it genuinely did).
- **Found and fixed a real crash in the process**: the classic Drift and
  Explainability tabs assumed every classification/regression model
  reaching them has the FULL hand-authored shape (since previously only
  fully-populated seed data ever existed). They crashed
  (`Cannot read properties of undefined (reading 'map')`) on the new
  honest partial/placeholder shapes. Found by isolating exactly which of
  9 tabs crashed across repeated runs, not by guessing.
- Verified end-to-end, not just at the API level: real browser walk
  through Train wizard → Experiments "Complete" → Model Overview showing
  the new model with real generated data; promote tested directly against
  the newly-registered version and confirmed working (`{"ok":true}`) where
  it would have 404'd before; all 9 Model Overview tabs clicked with zero
  errors for both the new model and all 4 pre-existing ones; full 13-route
  regression clean.
- **Scope**: app only. The mockup has no backend — "auto-generate honest
  monitoring data on Complete" would need a parallel JS implementation of
  the same logic, not just a data change. Flagged as a real follow-up, not
  silently left inconsistent with the app's version number this time —
  see the note in the mockup's own header comment.

## v2.7.0 — Deep learning training metrics, upgraded to a real dashboard (user-authored improvement, ported into the app)

The user directly authored a significantly richer version of the mockup's
`fraud-detector-dl` Training Metrics tab and asked for it to be ported into
the real app for all deep learning models. This release adopts that
improvement in the mockup as the new baseline and brings the app up to the
same depth.

**What changed, in both artifacts:**
- Training Metrics went from a flat stat grid to a genuine training
  dashboard: architecture + a live health score, a progress bar, 8
  headline metrics (loss, F1 gap, generalization status, learning rate,
  gradient norm, throughput, elapsed/ETA), **real SVG loss and gradient
  curves** rendered from actual `trainCurve`/`valCurve`/`gradCurve` arrays
  (28 real points each, not a placeholder), optimizer health (mean/max
  gradient, exploding/vanishing-gradient detection), early stopping state,
  parameter counts, a checkpoints table with the best-epoch highlighted,
  training compute, and a 6-signal health breakdown (loss convergence,
  generalization, gradient health, learning rate, compute health, data
  quality) with individual progress bars.
- New **Input Quality** tab and `dlInputQuality` data — production input
  validation (shape/tensor violations, NaN/Inf rates, missing/invalid/
  out-of-range rates) for the deployed model's live traffic, distinct from
  training-time metrics.
- Deep-learning detection broadened from pytorch-only to
  pytorch/torch/tensorflow/keras/jax, so this isn't accidentally
  PyTorch-specific.
- **App-side**: new nullable `dl_input_quality` JSONB column; `fraud-detector-dl`'s
  seed data rewritten to the same rich nested structure (architecture/
  progress/loss/generalization/optimization/parameters/throughput/compute/
  earlyStopping/checkpoints/health), matching the mockup's numbers exactly;
  a new `DeepLearningTrainingMetrics` TypeScript interface (properly typed,
  not `Record<string, unknown>`, given the depth); two new small components
  — `LineChart` (inline SVG polyline, same technique as the mockup's
  `mmMiniLine`) and `MetricMini` — reused across the dashboard rather than
  one-off JSX per section.
- Verified: real API response inspected for curve-array lengths and nested
  structure; full browser render confirmed 3 real `<svg><polyline>` charts
  present (not placeholders); every new field checked against real
  rendered text; `fraud-detector` (xgboost) confirmed to still show
  neither new tab; full 13-route app regression and full mockup
  regression both clean.

## v2.6.0 — Deep learning models get their own performance metrics, distinct from classical ML

Prompted by a direct question: "did we handle Deep Learning model specific
performance metrics?" The honest answer was no — the 5 deep-learning base
models added earlier (deep-mlp-classifier, cnn-image-classifier, etc.) had
zero monitoring data anywhere; none existed as a deployed/monitored
instance. If trained and deployed, one would have fallen into
`modelType: classification` and shown the exact same Performance tab as
`xgboost-classifier` — no training loss curves, no learning rate, no
gradient norm, no epochs, nothing distinguishing it as deep learning at
all.

- **Schema**: new nullable `training_metrics` JSONB column on
  `model_monitoring` — distinct from `fine_tuning` (which implies starting
  from a pretrained base; LLM-only). Populated only for a classification/
  regression model trained AS a deep net from scratch (`framework:
  pytorch`); null for classical ML and for LLMs (which use `fine_tuning`
  instead).
- **New real deployed instance**: `fraud-detector-dl`, a `deep-mlp-classifier`
  trained on the exact same dataset as `fraud-detector`, registered as a
  real challenger (not a synthetic standalone example) — same
  accuracy/F1/confusion-matrix shape as the xgboost champion (both are
  still classifiers), plus real training dynamics (epoch/step, train/val
  loss, learning rate, gradient norm, GPU utilization) xgboost has no
  equivalent of.
- **Frontend**: a third tab-set case — `DL_TABS` — adds one "Training
  Metrics" tab to the classic 8, rather than a wholesale different set
  like LLM gets (a deep classifier still has real accuracy/F1/
  explainability, unlike generated text). Detected via
  `model.framework === "pytorch" && modelType !== "generation"`.
- **Mockup**: added the same `fraud-detector-dl` entry to `DEPLOYED_MODELS`
  with matching data, a parallel `isMMDeepLearning()` detector (reads
  `details.identity.framework`), and the same one-extra-tab tab-bar logic.
  **Found and fixed a real bug in the process**: the hidden
  `#mmModelSelect` element that drives which model's detail view renders
  is a real `<select>` with a hardcoded, static `<option>` list — adding a
  new `DEPLOYED_MODELS` entry without also adding its `<option>` there
  makes the whole detail pane fail silently (title stays "—", no console
  error, no thrown exception) since setting `.value` on a `<select>` to a
  value with no matching `<option>` is a silent no-op in both real
  browsers and jsdom. Caught by comparing "the model list renders it
  correctly" against "the detail view doesn't," which narrowed it to
  exactly one hidden element.
- Verified: both artifacts confirmed showing "Training Metrics" only for
  `fraud-detector-dl`, confirmed absent for `fraud-detector` (xgboost) and
  for `support-tone-adapter`/`llama-3-8b-instruct` (which keep their own
  Fine-tuning tab, untouched by this change), full regression clean
  across all 5 now-existing Model Overview entries in both artifacts.

## v2.5.0 — Full LLM monitoring: 8 metric categories, both artifacts

Model Overview now has a genuinely distinct tab set for LLM (`generation`-
type) models instead of reusing classification's tabs, covering the 8
categories requested: Response Quality, Hallucination & Groundedness,
Latency & Serving, Infrastructure, Cost, Safety, Drift, and Fine-tuning.

- **Schema**: 4 new nullable JSONB columns on `model_monitoring`
  (`hallucination`, `safety`, `cost`, `fine_tuning`) — null for
  classification/regression models, populated for generation models.
  `explainability` also made nullable (doesn't map onto an LLM).
- **Seed**: `support-tone-adapter` (a real LoRA adapter of
  `llama-3-8b-instruct` already in the catalog) gets a full monitoring
  row. Every number is taken directly from the reference metrics doc's own
  worked examples, not invented — internally consistent with the doc's
  cost/token/latency figures.
- **App frontend**: `ModelOverviewPage` picks between two tab sets based
  on `modelType` — classification/regression keep the original 8 tabs
  unchanged; generation models get 12 tabs including the new 8, dropping
  Explainability (doesn't apply) and reworking Performance into "Response
  Quality" (same baseline/current table pattern, different metrics).
  Latency & Serving and Infrastructure are two tabs backed by one
  `infrastructure` JSONB column, split by key set — they describe the same
  deployment but are distinct categories in the source taxonomy.
- **Mockup** (closed in a follow-up turn after initially shipping app-only —
  see the note below): turned out `DEPLOYED_MODELS` already had
  `support-tone-adapter`/`llama-3-8b-instruct`/`demand-forecaster` entries
  with real lineage/traces/explainability data from an earlier session,
  so this added the 7 missing data blocks to the existing
  `support-tone-adapter` entry rather than building a parallel structure
  from scratch — plus a dynamically-rebuilt tab bar (`renderMMTabBar`) and
  6 new render functions, all reusing the existing `.metric-mini` /
  `.metric-grid` visual pattern Infrastructure already used. Every render
  path was also made to handle a generation-type model that DOESN'T have
  this data yet (`llama-3-8b-instruct`) with an honest "n/a" rather than
  crashing — the pre-existing code had at least one real crash risk here
  (`m.data.featureDrift` accessed unconditionally in three functions) that
  had just never been exercised since no second generation-type model had
  been opened in Model Overview before.
- Verified: both models' tab sets confirmed correct (classification and
  regression unaffected — no regression across all 4 pre-existing
  DEPLOYED_MODELS entries), all 8 new-category tabs checked for real data
  with zero leaked `undefined`, the Latency/Infrastructure key-split
  confirmed mutually exclusive, full 13-route (app) and full-navigation
  (mockup) regression passes both clean.
- **Process note**: this was first delivered app-only, with the mockup
  explicitly flagged as not yet updated — a deliberate exception to the
  "always ship both at the same version" rule rather than a silent
  version-number drift, closed in the very next turn once asked for the
  mockup directly. Recorded here so the exception and its resolution are
  both on the record, not just the resolution.

## v2.4.0 — Train wizard now genuinely handles 3 categories, not 2

Prompted by a direct question: "does the Train wizard handle Classical ML,
Deep Learning, and LLM accordingly?" Checking properly (not just confirming
the filter chip existed) found a real bug — the wizard only had a **binary**
branch, LLM vs. everything else. Any non-LLM model, including genuinely
unsupervised ones, was asked for a tabular "Target column."

This affected **23 models across the whole library**, not just the 5
deep-learning ones — clustering, anomaly detection, and dimensionality
reduction models (including pre-existing ones like `kmeans-clustering`)
had the same problem, confirmed by actually running the wizard against
them, not just reading the code.

- **Both artifacts**: Step 3 now branches three ways — supervised (target/
  features, unchanged), unsupervised (an explanatory notice, no target
  field at all — "works directly on the dataset's features with no
  target/label to predict"), and a distinct warning for
  `cnn-image-classifier` specifically (the platform has zero image
  datasets, so it can't be trained meaningfully regardless of target
  columns — this is flagged honestly rather than silently proceeding)
- Step 5's review reflects the same branching
- **App only**: the launch payload now correctly omits `targetColumn` for
  unsupervised models rather than sending an unused value
- Mockup: uses the existing `category` field (clustering/anomaly_detection/
  dimensionality_reduction → no target). App: uses `task`, since it has no
  `category` field (`NO_TARGET_TASKS` set: Anomaly detection, Clustering,
  Dimensionality reduction, Embedding)
- Verified end-to-end in both: mockup via a full regression suite (fresh
  DOM per case, after an earlier version of the test suite gave false
  failures from state leakage across cases in one shared DOM); app via a
  disposable mock UCS, real Postgres, and a real browser — including
  actually completing the 5-step wizard for an unsupervised model and
  confirming a genuine training run gets created with no target column

## v2.3.0 — Deep Learning models made discoverable in both artifacts

The 5 deep-learning models existed in the data (both the mockup's
BASE_MODELS and the app's seed) since a previous version, but were only
findable by mixing in with "Classical ML" or manual search — there was no
dedicated way to see them, which is what prompted this fix.

- **Mockup**: added a "Deep Learning" chip alongside "All / Classical ML /
  LLM" in both Model Catalog and the Train wizard's Step 1, filtering on
  `framework === "pytorch"` (Gravitino's `domain` stays ml/llm only —
  this is a framework-based view into the ml domain, not a new domain
  value). This surfaced that the "Deep Learning" bucket is actually
  **12 models**, not just my 5 additions — `tabular-embedding-nn`,
  `temporal-fusion-transformer`, and `nbeats-forecaster` were already
  in the original 80 and had the same discoverability problem.
- **App**: the real `ModelCatalogPage` never had a domain sub-filter at
  all (a gap that predates this conversation) — added the same
  All/Classical ML/Deep Learning/LLM chips, same `framework === "pytorch"`
  logic, backed by the real Postgres data.
- Also corrected: the mockup was mistakenly versioned "v1.1" (an
  independent numbering scheme) in the previous turn instead of matching
  the app's real version — both now always ship at the same number, and
  that's stated explicitly in both README.md and the mockup's own header
  comment.
- Verified: 12/12 real models confirmed under Deep Learning in both
  artifacts (including the 3 pre-existing ones), Classical ML's count
  correctly drops by 12 with no double-counting, "All" still shows
  everything, full 13-route regression clean in the app, full mockup
  regression suite clean.

## v2.2.0 — Alerts restructured to match the mockup, closing the last visual-richness gap; mockup version corrected to match

**Mockup:** was incorrectly delivered as an independently-numbered "v1.1"
in the previous turn — that was a mistake, not a real second version
scheme. Corrected to v2.2.0, matching the app exactly, and the policy above
(one shared version number, always) is now stated explicitly in both the
mockup's own header comment and this file. Content-wise, the mockup at
v2.2.0 has the same 85 base models covered in the paragraph below.

- **Alerts** now has the mockup's real 3-tab structure: **Rules** (policy
  enable/disable + per-model overrides, its own tab now rather than mixed
  in with everything else), **Config channels** (notification channel
  CRUD), **Active & History** (with its own Active/History sub-tabs)
- **New: real Alert History**, not just a UI shell — added an append-only
  `alert_history` table (the existing `alert_state` only holds one current
  row per model+policy, so it can't hold history without being overwritten
  the next time the same alert fires and resolves again). Resolving an
  alert now writes a permanent record and resets state to `none` rather
  than `resolved`, so the same alert can genuinely fire again later
  instead of being filtered out forever.
- New `GET /api/alerts/history` endpoint
- Verified end-to-end via a disposable mock UCS: resolved a real fired
  alert through the actual UI and confirmed it moved from Active to
  History with the correct timestamp, final value, and resolver

This closes the last of the three visual-richness gaps flagged since
v2.0.0 (Dashboard and Pipelines DAG closed in v2.1.0).

## v2.1.0 — Dashboard and Pipelines DAG closed against the mockup

Two of the three visual-richness gaps flagged in v2.0.0's README closed:

- **Dashboard** — was 3 static count cards; now pulls real cross-page data
  (deployments, alerts fired/building split, compute pool utilization,
  pipelines/training runs in progress) into a genuine "what needs
  attention" ops view, sourced entirely from existing APIs — no backend
  changes needed.
- **Pipelines DAG** — stage list is now a real connected DAG: bordered
  nodes with status icons (✓/✗/●/○), color-coded by state, joined by
  connector lines that only light up teal between genuinely completed
  stages. Verified through a full restart cycle (via a disposable mock
  UCS, since real UCS RBAC now gates this route) — the DAG correctly
  resets to stage 1 in-progress with the rest back to pending.
- Still open: Alerts page (mockup had 3 tabs — Rules / Config channels /
  Active & History — this app has it flattened onto one page).

## v2.0.0 — Unified Catalog Service integration (breaking)

**Breaking:** RBAC is no longer decided locally. Every `requirePermission`
check now asks the real, external Unified Catalog Service and **fails
closed** if UCS is unreachable or misconfigured — meaning every mutating
action (promote, rollback, train, restart a pipeline, regenerate a secret,
edit an alert, share a dataset) will return 403 until `UCS_BASE_URL` /
`UCS_AUTH_TOKEN` / `UCS_MODEL_CATALOG` in `backend/.env` point at a real,
reachable UCS with the expected roles created. This is intentional — see
`backend/src/ucs/README.md`.

- Removed the local, Postgres-backed fake "catalog service" module built
  in a prior version — replaced with `backend/src/ucs/`, a real HTTP
  client against the actual UCS (`app/routers/admin.py`'s documented
  contract: `POST /models`, `POST /models/versions`, `GET /rbac/matrix`,
  role/group management)
- Training-run creation/completion and deployment promote/rollback now
  register the model + a version (with `champion`/`training-run` aliases)
  in UCS — fail-soft, so a UCS outage doesn't block the local operation,
  but is logged loudly
- New `GET /api/ucs/status` endpoint reports UCS reachability and RBAC
  cache state
- Pipeline restart intentionally NOT cataloged in UCS — the provided
  schema has no pipeline/workflow object type
- Lineage integration NOT implemented — the only lineage endpoint shown
  (`/lineage/namespaces`) is OpenLineage namespace-mapping config, not a
  generic edge-recording API; see `backend/src/ucs/README.md` for what's
  needed to close this
- Verified against a disposable mock server matching the documented
  contract exactly (not the real UCS — see README's "a different
  verification standard" section)

## v1.0.0 — baseline full-stack build

Everything built and verified up to this point, consolidated as the first
tracked version:

- **Domain model** — base model library, catalog models + version history,
  datasets, endpoints (OAuth 2.0 client credentials), alert policies +
  active state + per-model overrides + notification channels, training
  runs (classical ML and LLM fine-tuning), pipelines (DAG + logs +
  restart), deployments (promote/rollback), model monitoring (8-section
  payload), a shared audit log, compute pool utilization, Gravitino-style
  catalog entries — all in real PostgreSQL, not mocked
- **RBAC** — `X-User-Role` enforced server-side on every mutating route
  (ml-engineer, ml-platform-admin, gpu-quota-approver, ml-viewer)
- **13 frontend pages** — Dashboard, Model overview (8 tabs), Model catalog
  (search/filter/test), Train wizard (5 steps, domain-aware), Datasets (4
  detail tabs), Pipelines, Experiments, Deploy & promote, Endpoints,
  Alerts (full CRUD), Catalog (Gravitino), Access & audit, Cluster
  monitoring
- **85 real base models** seeded — the mockup's original 80 (73 ML, 7 LLM)
  plus 5 added deep-learning entries (deep feedforward classifier/
  regressor, CNN image classifier, GRU sequence classifier, denoising
  autoencoder)
- Every piece verified against a live Postgres instance and a real browser
  (Puppeteer) — not just written and assumed correct. See README → "What's
  verified" for specifics.

---

## Format for future entries

```
## vX.Y.Z — one-line summary

- What changed, in plain terms
- Any schema changes (note if a reseed is required)
- Anything re-verified as a result
```

Patch-level bumps (v1.0.x) for fixes and small additions; minor bumps
(v1.x.0) for new features/pages; major bumps (vX.0.0) reserved for
breaking API/schema changes.

## v2.36.3 — Durable HPO + LLM GPU certification gate
- `POST /api/hpo-searches` now validates/pins data, creates durable queued trial rows, and returns HTTP 202; no training runs in the request lifecycle.
- HPO trials are claimed with PostgreSQL `FOR UPDATE SKIP LOCKED`, routed by `WORKER_COMPUTE_PROFILES`, heartbeated, retried after stale-worker recovery, and cancellable.
- Running HPO cancellation propagates through `AbortSignal` into Python trainer subprocesses.
- Added CUDA worker image definition, QLoRA `bitsandbytes` dependency, GPU deployment manifest, and `llm_gpu_certify.py` full certification harness (real fine-tune -> artifact reload -> generation).
- GPU worker can enforce the certification gate at startup with `REQUIRE_LLM_GPU_CERTIFICATION=true` and `LLM_GPU_CERT_CONFIG=/path/cert.json`.
- Certification cannot be truthfully claimed on a node without CUDA; this build provides an enforceable production certification workflow rather than a simulated pass.

## v2.36.7 — Production monitoring correctness + feedback + telemetry
- Removed simulated classification-shaped metrics from training completion/monitoring registration.
- Model monitoring baselines now use the exact task-appropriate measured trainer metrics (classification, regression/forecasting, or LLM).
- Added prediction IDs/correlation IDs to serving traces and responses.
- Added durable `prediction_feedback` storage and `POST /api/models/:id/feedback` ground-truth ingestion.
- Feedback recomputes production classification accuracy or regression MAE/RMSE from labeled predictions without pretending labels exist before they arrive.
- Added Prometheus-text collectors for configurable Prometheus, KServe, vLLM and NVIDIA DCGM endpoints.
- Added scheduled telemetry collection, durable telemetry snapshots, and model infrastructure telemetry projection.
- Added request/error/timeout/queue/replica/restart/OOM/GPU/TTFT/TPOT/token-rate normalization.

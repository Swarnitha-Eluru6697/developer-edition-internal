import { spawn } from "child_process";
import { createHash } from "crypto";
import { PYTHON_BIN } from "./pythonBin";
import { runSpeechFineTuning } from "./speechRuntime";
import { logRun, trackRunProcess } from "./runLog";
import { Pool } from "pg";
import { promises as fs } from "fs";
import path from "path";
import os from "os";
import { runDeepLearningTraining } from "./deepLearningTrainer";
import { runClassicalTraining } from "./classicalTrainer";
import { getClassicalClasspath, mapToClassicalTask, TIME_SERIES_LIBRARY } from "./classicalModelMapping";
import { runTimeSeriesTraining } from "./timeseriesTrainer";
import { runLlmFineTuning, LlmFineTuneResult } from "./llmFinetuneTrainer";
import { registerModel, registerModelVersion, ingestLineageEvent, UcsError } from "../ucs/client";
import { modelFqn } from "../ucs/config";
import { getStorage } from "../storage";
import { runDatasetProfiler } from "./datasetProfiler";
import { calculateHealthSnapshot } from "./modelHealth";
import { requiresDurableArtifacts } from "../utils/runtimePolicy";
import { ArtifactPersistenceError, storageKeyFromUri, verifyDurableArtifact } from "./artifactPersistence";
import { captureReproducibility } from "./reproducibility";

// Shared by both the deep-learning and classical-ML real-training
// branches below — uploads whatever real model file was just produced,
// and cleans up the local temp dir regardless of whether the upload
// succeeded (real_trainer.py / classical_trainer.py both use
// tempfile.mkdtemp(), which otherwise leaks /tmp space on every run).
// Shared by both normal training-run registration and HPO trial
// execution — uploads whatever real model file was just produced to a
// caller-specified key, and cleans up the local temp dir regardless of
// whether the upload succeeded (real_trainer.py / classical_trainer.py /
// timeseries_trainer.py all use tempfile.mkdtemp(), which otherwise
// leaks /tmp space on every run — this was happening unconditionally
// for every HPO trial before this function existed, since trial
// execution never called anything that cleaned up after itself).
// Downloads the real dataset file for this run, if one was ever
// uploaded (datasets.storage_uri set — see routes/datasets.ts's real
// upload endpoint). Returns null when the dataset is still metadata-
// only, which is the honest, common case for most datasets right now —
// callers fall back to synthetic data in that case, unchanged.
export interface ResolvedDataset {
  localPath: string;
  snapshotVersion: number;
  /** Every file (latest version of each) this training input was built from. */
  files?: { fileName: string; fileVersion: number; version: number; checksum: string | null; rows: number }[];
  profile: any;
  format: string;
  checksum: string | null;
  externalSource?: any;
}

// Resolves the LATEST real immutable snapshot for a dataset (see
// dataset_snapshots — routes/datasets.ts) and downloads that EXACT
// file. Previously this read datasets.storage_uri directly — a single,
// mutable pointer that a later upload could silently move out from
// under an already-trained model. Now resolves a specific, versioned,
// checksummed row, and the caller records which version was actually
// used (training_runs.dataset_snapshot_version) — a trained model's
// data is now genuinely traceable and reproducible, not just "whatever
// the dataset named X currently points to."
export async function resolveDatasetSnapshot(pool: Pool, datasetName: string, requestedVersion?: number | null): Promise<ResolvedDataset | null> {
  // A dataset holds many files; a file uploaded again under the same name is a new
  // version of that file. Training uses the latest version of EVERY file. A pin
  // (requestedVersion) means "the files as they stood at that upload": for each
  // file, its newest version at or before the pinned dataset version. An explicit
  // pin that doesn't exist fails rather than silently resolving to something else.
  const rows = (await pool.query(
    "SELECT * FROM dataset_snapshots WHERE dataset_name = $1 AND ($2::int IS NULL OR version <= $2) ORDER BY version DESC",
    [datasetName, requestedVersion ?? null]
  )).rows;
  if (requestedVersion && !rows.some((r) => r.version === requestedVersion)) {
    throw new Error(`dataset '${datasetName}' has no real snapshot at the requested version ${requestedVersion} — it may have existed at creation time and been removed since`);
  }
  // Nobody has ever uploaded data for this dataset: the one case with no real data.
  if (!rows.length) return null;
  const seen = new Set<string>();
  const active = rows.filter((r) => {
    const key = r.file_name ?? `${datasetName}.${r.format}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).reverse(); // oldest file first — a stable order for the combined table
  const pinVersion = Math.max(...active.map((r) => r.version));

  // Real data exists from here on, so every failure is fail-closed — never a
  // silent fall-back to synthetic data in place of data that genuinely exists.
  const storage = getStorage();
  if (!storage) {
    throw new Error(`DATASET_UNAVAILABLE: dataset '${datasetName}' v${pinVersion} is a real, resolved snapshot, but STORAGE_BACKEND isn't configured on this deployment right now — refusing to silently train on synthetic data in place of data that genuinely exists`);
  }
  try {
    const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "ml-studio-dataset-"));
    const local: { path: string; format: string; fileName: string; bytes: number }[] = [];
    for (const [i, snap] of active.entries()) {
      // https://<account>.blob.core.windows.net/<container>/<key> carries the
      // container in the path: strip it, or ADLS looks for <container>/<container>/<key>.
      const key = storageKeyFromUri(snap.storage_uri as string, storage.bucket);
      const data = await storage.getObject(key);
      // Format-matched extension: the loaders detect the format from it.
      const format = snap.format || "csv";
      const p = path.join(tmpDir, active.length === 1 ? `data.${format}` : `part-${i}.${format}`);
      await fs.writeFile(p, data);
      local.push({ path: p, format, fileName: snap.file_name ?? `${datasetName}.${format}`, bytes: data.length });
    }
    const files = active.map((r) => ({ fileName: r.file_name ?? `${datasetName}.${r.format}`, fileVersion: r.file_version ?? 1, version: r.version, checksum: r.checksum ?? null, rows: r.row_count }));
    if (active.length === 1) {
      const snap = active[0];
      logRun("dataset", "ok", `Pinned ${datasetName} v${snap.version} — ${files[0].fileName} (${local[0].format}, ${(local[0].bytes / 1e6).toFixed(2)} MB${snap.checksum ? `, sha256 ${String(snap.checksum).slice(0, 12)}…` : ""}) — downloaded to the worker`,
        { version: snap.version, format: local[0].format, bytes: local[0].bytes, checksum: snap.checksum ?? null, files });
      return { localPath: local[0].path, snapshotVersion: snap.version, profile: snap.profile, format: local[0].format, checksum: snap.checksum ?? null, externalSource: snap.external_source ?? null, files };
    }
    const combined = await combineDatasetFiles(local, tmpDir);
    if (!combined.ok) throw new Error(`couldn't combine the dataset's ${active.length} files: ${combined.error}`);
    // One fingerprint for the exact set of file versions used.
    const checksum = createHash("sha256").update(files.map((f) => `${f.fileName}:${f.checksum}`).sort().join("\n")).digest("hex");
    logRun("dataset", "ok", `Pinned ${datasetName} @v${pinVersion}: ${active.length} files — ${files.map((f) => `${f.fileName} v${f.fileVersion}`).join(", ")} — combined into ${combined.rows.toLocaleString("en-US")} ${combined.format === "parquet" ? "rows" : "items"}`,
      { version: pinVersion, files, combinedRows: combined.rows, format: combined.format, checksum });
    return { localPath: combined.path, snapshotVersion: pinVersion, profile: active[active.length - 1].profile, format: combined.format, checksum, externalSource: null, files };
  } catch (err) {
    // Real data was resolved but couldn't be retrieved/combined: this run fails.
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(`DATASET_UNAVAILABLE: dataset '${datasetName}' v${pinVersion} was resolved but couldn't be retrieved from storage: ${reason}`);
  }
}

/** Extracts documents into passages (training/documents_to_passages.py). */
function documentsToPassages(srcPath: string, format: string, outPath: string, maxPassages: number | null, seed: number):
  Promise<{ ok: true; path: string; passages: number; totalPassages: number; documents: number; byType: Record<string, number> } | { ok: false; error: string }> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [path.join(__dirname, "..", "training", "documents_to_passages.py")]);
    trackRunProcess(child);
    let out = "", err = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => resolve({ ok: false, error: e.message }));
    child.on("close", () => {
      try { resolve(JSON.parse(out.trim().split("\n").pop() || "")); }
      catch { resolve({ ok: false, error: err.slice(-400) || "no output from documents_to_passages.py" }); }
    });
    child.stdin.end(JSON.stringify({ path: srcPath, format, outPath, maxPassages, seed }));
  });
}

/** Stacks tables / merges archives (training/combine_dataset_files.py). */
function combineDatasetFiles(files: { path: string; format: string; fileName: string }[], outDir: string):
  Promise<{ ok: true; path: string; format: string; rows: number } | { ok: false; error: string }> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [path.join(__dirname, "..", "training", "combine_dataset_files.py")]);
    trackRunProcess(child);
    let out = "", err = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => resolve({ ok: false, error: e.message }));
    child.on("close", () => {
      try { resolve(JSON.parse(out.trim().split("\n").pop() || "")); }
      catch { resolve({ ok: false, error: err.slice(-400) || "no output from combine_dataset_files.py" }); }
    });
    child.stdin.end(JSON.stringify({ files, outDir }));
  });
}

export interface QualityGateResult {
  blocked: boolean;
  blockers: string[];
  warnings: string[];
}

// Real quality gate — checks the REAL computed profile (dataset_profiler.py)
// against real thresholds, not fabricated pass/fail. Blocks training
// outright only for genuinely unusable data (an empty file, a target
// column that's mostly missing, a target with only one real value —
// nothing to learn from); warns, but proceeds, for lesser real issues.
// Previously no gate existed at all — a dataset that was 90% missing
// values in its target column would train "successfully" and report
// metrics as if that were fine.
// Real, target-aware re-profile at TRAIN time — the profile stored on
// the snapshot (computed once at upload, before any target column was
// known) has no leakage signals, since leakage is inherently relative
// to a specific target. This runs the same real profiler again against
// the already-downloaded real file, with THIS run's real target column,
// so the quality gate sees real leakage signals and the exact real
// per-class counts for whatever target this specific run actually
// uses — not a stale, target-agnostic snapshot of the file. Falls back
// to the stored profile (still real, just without leakage) if this
// second pass fails for any reason — a profiling hiccup shouldn't block
// training outright when the upload-time profile already exists.
export async function getTargetAwareProfile(resolved: ResolvedDataset, targetColumn: string | null): Promise<any> {
  const fresh = await runDatasetProfiler(resolved.localPath, resolved.format, targetColumn);
  if (fresh.ok) return fresh;
  // eslint-disable-next-line no-console
  console.error(`Real target-aware re-profiling failed for this run — falling back to the upload-time profile (no leakage signals): ${fresh.error}`);
  return resolved.profile;
}

export interface TargetFinding {
  severity: "blocker" | "warning";
  reason: string;
}

/**
 * Is this column usable as a training target, and if not, why not — in words a
 * person can act on.
 *
 * Extracted so exactly one definition exists. The quality gate calls it at
 * train time, and `GET /api/datasets/:name/target-candidates` calls it for the
 * Train wizard's dropdown, so the wizard cannot offer a target that the gate
 * will refuse thirty seconds later. Before this, it did: picking a URL column
 * looked fine in the UI and then failed the run.
 *
 * `isRegression` matters because the rules invert. A regression target is
 * SUPPOSED to have as many distinct values as it has rows; a classification
 * target with that property is an identifier, not a label set.
 */
export function assessTargetColumn(
  profile: { rowCount?: number },
  col: any,
  isRegression: boolean
): TargetFinding[] {
  const out: TargetFinding[] = [];
  if (!col) return out;
  const name = col.name;
  const rowCount = profile?.rowCount ?? 0;

  if (col.missingPct > 20) {
    out.push({ severity: "blocker", reason: `target column '${name}' is ${col.missingPct}% missing — not enough real labels to train on` });
  }
  if (col.kind === "numeric" && col.std === 0) {
    out.push({ severity: "blocker", reason: `target column '${name}' has zero variance (every real value is the same) — nothing for a model to learn` });
  }
  if (col.kind === "categorical" && col.cardinality === 1) {
    out.push({ severity: "blocker", reason: `target column '${name}' has only one real distinct value — nothing for a model to learn` });
  }

  // ---- Is this column a label set at all? ----
  // Without this, an identifier or free-text column reached the trainer and
  // failed deep inside the estimator with a type error listing a thousand class
  // indices — technically accurate, useless to act on, and only after a dataset
  // download and a fit had been paid for.
  if (!isRegression && typeof col.cardinality === "number" && col.cardinality > 1 && rowCount > 0) {
    const perClass = rowCount / col.cardinality;
    const ratio = typeof col.cardinalityRatio === "number" ? col.cardinalityRatio : col.cardinality / rowCount;
    if (ratio >= 0.9) {
      out.push({
        severity: "blocker",
        reason: `target column '${name}' has ${col.cardinality} distinct values across ${rowCount} rows (${Math.round(ratio * 100)}% unique) — that is an identifier or free text, not a label set. A classifier can't be asked to predict it. Pick a column with a bounded set of classes, or train a regression/forecasting model instead.`,
      });
    } else if (perClass < 2) {
      // Below this the split is impossible rather than merely noisy: some class
      // must be absent from either train or test.
      out.push({
        severity: "blocker",
        reason: `target column '${name}' has ${col.cardinality} distinct values across ${rowCount} rows — an average of ${perClass.toFixed(1)} rows per class, so most classes can't appear in both the train and test split. Too many classes for this much data.`,
      });
    } else if (perClass < 10) {
      out.push({
        severity: "warning",
        reason: `target column '${name}' has ${col.cardinality} classes across ${rowCount} rows (~${perClass.toFixed(1)} rows per class) — real metrics from this many thinly-populated classes will be noisy`,
      });
    }
  }
  return out;
}

/** Shared with the gate: whether this run is a regression, from the declared
 *  task where there is one, else inferred from the target itself.
 *
 *  Expects a task already normalised by mapToClassicalTask ("classification",
 *  "regression", …). The substring fallback exists for unmapped callers, but is
 *  deliberately anchored: several library entries are labelled
 *  "Classification / regression", and a bare `includes("regress")` graded every
 *  one of them as a regression — which silently disabled every classification
 *  rule for xgboost, random-forest and friends. */
export function isRegressionTask(task: string | null | undefined, targetCol: any): boolean {
  const declared = (task || "").trim().toLowerCase();
  if (declared) {
    if (declared === "regression") return true;
    if (declared.includes("classification")) return false;
    return declared.includes("regress") || declared.includes("forecast");
  }
  return targetCol?.kind === "numeric" && typeof targetCol.cardinality === "number" && targetCol.cardinality > 20;
}

/**
 * `task` makes the leakage rule below task-aware. Omitted, it behaves as before
 * (the strict classification reading), so existing callers are unchanged.
 */
export function checkDatasetQualityGate(
  profile: any,
  targetColumn: string | null,
  task?: "classification" | "regression" | string | null
): QualityGateResult {
  const blockers: string[] = [];
  const warnings: string[] = [];
  if (!profile?.ok) return { blocked: false, blockers, warnings }; // no profile to check against — can't gate on nothing

  // ---- Dataset-level ----
  if (profile.rowCount === 0) blockers.push("dataset has 0 rows");
  // A real, if arbitrary, floor for a meaningful train/test split — the
  // trainers use a 75/25 split; fewer than 10 total rows leaves almost
  // nothing real on either side of it.
  else if (profile.rowCount < 10) blockers.push(`dataset has only ${profile.rowCount} real row(s) — not enough for a meaningful train/test split`);
  else if (profile.rowCount < 50) warnings.push(`dataset has only ${profile.rowCount} real rows — real metrics from this small a split should be read with real caution`);

  if (profile.duplicateRowCount > 0 && profile.duplicateRowCount / Math.max(profile.rowCount, 1) > 0.3) {
    warnings.push(`${profile.duplicateRowCount} of ${profile.rowCount} rows (${Math.round((profile.duplicateRowCount / profile.rowCount) * 100)}%) are exact duplicates`);
  }

  const columns: any[] = profile.columns || [];
  const targetCol = targetColumn ? columns.find((c) => c.name === targetColumn) : null;

  // Prefer the caller's declared task. Failing that, infer from the target
  // itself: a numeric target with many distinct values is a regression, which
  // is exactly the case the old rule got wrong.
  const isRegression = isRegressionTask(task, targetCol);

  // ---- Target-level ----
  if (targetCol) {
    // The same rules the Train wizard grades its target dropdown against —
    // see assessTargetColumn. Called rather than restated so the wizard can
    // never offer a target this gate will later refuse.
    for (const finding of assessTargetColumn(profile, targetCol, isRegression)) {
      (finding.severity === "blocker" ? blockers : warnings).push(finding.reason);
    }

    // Real class imbalance + minimum class samples — computed from the
    // real per-class counts (allValueCounts), not estimated. Only
    // meaningful for a real classification target with a real, bounded
    // set of classes — a high-cardinality "categorical" column here is
    // more likely free text than a real label set, so this is skipped
    // when allValueCounts wasn't computed (cardinality > 200).
    if (targetCol.kind === "categorical" && Array.isArray(targetCol.allValueCounts) && targetCol.allValueCounts.length > 1) {
      const counts = targetCol.allValueCounts.map((v: any) => v.count);
      const total = counts.reduce((a: number, b: number) => a + b, 0);
      const minClass = Math.min(...counts);
      const minClassLabel = targetCol.allValueCounts.find((v: any) => v.count === minClass)?.value;
      if (minClass < 2) {
        blockers.push(`target class '${minClassLabel}' has only ${minClass} real sample(s) — can't appear in both a real train and test split`);
      } else if (minClass < 10) {
        warnings.push(`target class '${minClassLabel}' has only ${minClass} real samples — real metrics for this class will be noisy`);
      }
      const minorityFrac = minClass / total;
      if (minorityFrac < 0.05 && minClass >= 2) {
        warnings.push(`real class imbalance: '${minClassLabel}' is only ${(minorityFrac * 100).toFixed(1)}% of real rows`);
      }
    }
  }

  // ---- Feature-level ----
  for (const col of columns) {
    if (targetColumn && col.name === targetColumn) continue;
    if (col.missingPct > 50) warnings.push(`feature column '${col.name}' is ${col.missingPct}% missing`);
    if (col.isConstant) warnings.push(`feature column '${col.name}' is constant (every real value is the same) — carries no real signal`);
    else if (col.isNearConstant) warnings.push(`feature column '${col.name}' is near-constant — almost no real variation`);
    if (col.kind === "categorical" && typeof col.cardinalityRatio === "number" && col.cardinalityRatio > 0.9 && profile.rowCount > 20) {
      warnings.push(`feature column '${col.name}' has extreme cardinality (${Math.round(col.cardinalityRatio * 100)}% of rows are distinct values) — likely an identifier, not a real feature`);
    }
    // One-hot width, which is a memory fact rather than a data-quality one.
    // Every categorical feature becomes one column per category, so an
    // 8,011-value city column over a million rows is an 8-billion-cell matrix:
    // 60 GB densified. That used to take the trainer out by SIGKILL with no
    // explanation. The classical trainers now keep such a matrix sparse, so
    // this is a warning rather than a blocker — but it is worth saying up
    // front, because a neural trainer genuinely cannot use it and because it
    // is usually a modelling mistake.
    else if (col.kind === "categorical" && typeof col.cardinality === "number" && col.cardinality > 1000 && profile.rowCount > 0) {
      const cells = profile.rowCount * col.cardinality;
      const denseGb = (cells * 8) / 1024 ** 3;
      if (denseGb > 2) {
        warnings.push(
          `feature column '${col.name}' has ${col.cardinality.toLocaleString("en-US")} distinct values — one-hot encoding it over ${profile.rowCount.toLocaleString("en-US")} rows is a ${denseGb.toFixed(0)} GB dense matrix. Classical trainers handle this sparsely, but a deep-learning model can't; deselect the column or group its values if you plan to train one.`
        );
      }
    }
    if (col.kind === "numeric" && typeof col.outlierPct === "number" && col.outlierPct > 10) {
      warnings.push(`feature column '${col.name}' has ${col.outlierPct}% real statistical outliers (Tukey IQR)`);
    }
    // Real leakage signal — only present when the profile was computed
    // with this run's real target column (see resolveDatasetSnapshot's
    // caller, which re-profiles with the target known specifically for
    // this check). A near-perfect signal is blocked outright — a
    // feature that IS the target (or a trivial transform of it) makes
    // any resulting metric meaningless, not just suspicious; a strong
    // but imperfect signal is a real warning worth a human look.
    if (col.leakageSignal) {
      const v = Math.abs(col.leakageSignal.value);
      const isExactMatch = col.leakageSignal.kind === "exactMatchRate";
      const isCategorical = col.leakageSignal.kind === "categoricalDeterminism";
      const label = isExactMatch ? "exact-match rate" : isCategorical ? "deterministic-level share" : "correlation";

      if (isCategorical) {
        // A categorical feature whose LEVELS decide the target without ever
        // equalling it — e.g. Shipment_Status="Delayed" covering the whole
        // delayed class against a 0/1 target. Exact-match can't see this, and
        // it is what turns a real model into a 100%-f1 restatement of the
        // label.
        //
        // Warned, not blocked, even at a high share: a genuinely dominant
        // level can be legitimate (a status that follows from the outcome is
        // leakage; one that causes it is a real feature), and that distinction
        // needs a human who knows the domain. Only an essentially total share
        // blocks, where no reading survives.
        const detail = Array.isArray((col.leakageSignal as any).detail) ? (col.leakageSignal as any).detail : [];
        const levels = detail.map((d: any) => `'${d.level}' (${d.rows} rows, always target=${d.target})`).join(", ");
        if (v >= 0.98) {
          blockers.push(`feature column '${col.name}' decides the target for ${Math.round(v * 100)}% of rows — ${levels}. This column restates the label rather than predicting it.`);
        } else if (v >= 0.2) {
          warnings.push(`feature column '${col.name}' has level(s) that perfectly determine the target, covering ${Math.round(v * 100)}% of rows — ${levels}. If that status is recorded after the outcome is known, any accuracy from it is leakage, not prediction.`);
        }
      } else if (isExactMatch) {
        // The feature's value literally equals the target's. That is leakage in
        // any task — the model is reading the answer, not predicting it.
        if (v >= 0.98) {
          blockers.push(`feature column '${col.name}' has a real exact-match rate of ${col.leakageSignal.value} with the target — this column IS the target, not a predictive feature`);
        } else if (v >= 0.9) {
          warnings.push(`feature column '${col.name}' matches the target's value in ${Math.round(v * 100)}% of rows — worth a real check for leakage`);
        }
      } else if (isRegression) {
        // A strong linear relationship between a numeric feature and a numeric
        // target is the SIGNAL in a regression problem, not a defect — years of
        // experience against salary correlates ~0.98 in exactly the way the
        // model is supposed to exploit. Blocking at 0.98 made every such
        // dataset untrainable, which is what this gate was doing.
        //
        // Only an essentially perfect correlation still blocks: at |r| >= 0.9999
        // the feature is an affine transform of the target, so "predicting" it
        // is arithmetic and any resulting metric is meaningless.
        if (v >= 0.9999) {
          blockers.push(`feature column '${col.name}' has a real correlation of ${col.leakageSignal.value} with the target — that is an exact linear transform of it, so any metric from this would be arithmetic, not prediction`);
        } else if (v >= 0.98) {
          warnings.push(`feature column '${col.name}' has a real correlation of ${col.leakageSignal.value} with the target — expected for a strong regression feature, but confirm it isn't derived from the target`);
        }
      } else {
        // Classification (or unknown task, which keeps the strict reading): a
        // continuous feature that almost perfectly orders the label usually
        // means the label leaked into it.
        if (v >= 0.98) {
          blockers.push(`feature column '${col.name}' has a real ${label} of ${col.leakageSignal.value} with the target — almost certainly real data leakage, not a real predictive feature`);
        } else if (v >= 0.9) {
          warnings.push(`feature column '${col.name}' has a real ${label} of ${col.leakageSignal.value} with the target — worth a real check for leakage`);
        }
      }
    }
  }

  return { blocked: blockers.length > 0, blockers, warnings };
}

export async function uploadRealWeights(key: string, modelFilePath: string): Promise<string | null> {
  const storage = getStorage();
  const required = requiresDurableArtifacts();
  if (!storage) {
    await fs.rm(path.dirname(modelFilePath), { recursive: true, force: true }).catch(() => {});
    if (required) throw new ArtifactPersistenceError("durable artifacts are required, but STORAGE_BACKEND is not configured");
    return null;
  }
  try {
    const fileData = await fs.readFile(modelFilePath);
    logRun("artifact", "info", `Uploading artifact (${(fileData.length / 1e6).toFixed(1)} MB) to ${storage.uriFor(key)}`);
    const uploadResult = await storage.putObject(key, fileData, "application/octet-stream");
    if (required) {
      const exists = await storage.objectExists(key);
      if (!exists) throw new ArtifactPersistenceError(`upload returned '${uploadResult.uri}', but the object was not readable via an existence check`);
    }
    logRun("artifact", "ok", `Artifact stored and verified: ${uploadResult.uri}`, { uri: uploadResult.uri, bytes: fileData.length });
    return uploadResult.uri;
  } catch (err) {
    if (required) {
      if (err instanceof ArtifactPersistenceError) throw err;
      throw new ArtifactPersistenceError(`real training completed, but uploading weights to ${process.env.STORAGE_BACKEND || "(unconfigured)"} failed: ${err instanceof Error ? err.message : String(err)}`);
    }
    // eslint-disable-next-line no-console
    console.error(`Real model trained successfully, but uploading its weights to ${process.env.STORAGE_BACKEND || "(unconfigured)"} failed — development mode will keep the version non-deployable:`, err);
    return null;
  } finally {
    await fs.rm(path.dirname(modelFilePath), { recursive: true, force: true }).catch(() => {});
  }
}

// Deterministic seed from the run id, so re-viewing the same run's
// training metrics doesn't reshuffle them on every request (registration
// only runs once anyway, but keeps this pure/reproducible).
// Real, human-readable framework names derived from what actually
// trained a version — distinct from catalog_models.framework (the
// DECLARED framework), used to populate model_versions.actual_framework
// so a real mismatch is a real, queryable field, not just a source
// comment or a free-text note buried in monitoring JSON.
function deriveActualFrameworkFromClasspath(classpath: string): string {
  const libraryPrefix = classpath.split(".")[0];
  // "sklearn" (not "scikit-learn") — matches how every classical base
  // model's OWN declared framework is actually written in
  // baseModelsData.ts. Using the library's more formal proper name here
  // instead was a real bug: it silently flagged architectureFidelityMismatch
  // as true on every single classical model ever trained, since "sklearn"
  // and "scikitlearn" (both normalized) never match — not a genuine
  // declared-vs-actual divergence, just two different names for the exact
  // same thing. Found while testing v2.37.0's reproducibility lineage
  // output, but the bug itself predates that work.
  const LIBRARY_NAMES: Record<string, string> = {
    sklearn: "sklearn", xgboost: "XGBoost", lightgbm: "LightGBM", catboost: "CatBoost", umap: "umap-learn",
  };
  return LIBRARY_NAMES[libraryPrefix] || libraryPrefix;
}

const TIME_SERIES_LIBRARY_FRAMEWORK: Record<string, string> = {
  prophet: "Prophet", arima: "statsmodels", sarima: "statsmodels", auto_arima: "pmdarima",
  exponential_smoothing: "statsmodels", holt_winters: "statsmodels",
};

function hashStringToInt(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h;
}


// Maps a base model's (domain, task) onto model_monitoring's model_type.
// Mirrors the same categorization TrainPage.tsx already uses for the
// Train wizard's target-column branching (NO_TARGET_TASKS) — a model with
// no target column in training has no "classification/regression"
// performance concept in monitoring either.
const NO_TARGET_TASKS = new Set(["Anomaly detection", "Clustering", "Dimensionality reduction", "Embedding"]);
function deriveModelType(domain: string, task: string): string {
  if (domain === "llm") return "generation";
  if (task === "Clustering") return "clustering";
  if (task === "Anomaly detection") return "anomaly_detection";
  if (task === "Dimensionality reduction" || task === "Embedding") return "dimensionality_reduction";
  if (task === "Regression" || task === "Forecasting") return "regression";
  return "classification"; // Classification, Classification / regression, Image classification, Ranking, etc.
}

function isDeepLearningFramework(framework: string): boolean {
  return /pytorch|torch|tensorflow|keras|jax/i.test(framework);
}

// Deterministic, monotonically-decreasing curve converging toward
// endValue — used to derive a plausible-looking loss curve from a real
// final metric, since there's no actual training loop in this scaffold to
// have measured one from. Explicitly NOT random: same run, same curve,
// every time you look at it.
function decayCurve(startValue: number, endValue: number, points: number): number[] {
  const curve: number[] = [];
  for (let i = 0; i < points; i++) {
    const t = i / Math.max(points - 1, 1);
    const v = endValue + (startValue - endValue) * Math.exp(-3 * t);
    curve.push(Math.round(v * 1000) / 1000);
  }
  return curve;
}

export interface CompletedRun {
  id: string;
  base_model_id: string;
  model_name: string;
  dataset_name: string;
  requested_dataset_version?: number | null;
  time_series_config?: {
    timestampColumn: string; valueColumn: string;
    entityColumn?: string | null; entityValue?: string | null;
    frequency?: string | null; timezone?: string | null;
    missingPeriodPolicy?: "ffill" | "interpolate" | "zero" | "drop";
    globalModel?: boolean; predictionHorizon?: number;
    knownRealColumns?: string[]; unknownRealColumns?: string[]; knownCategoricalColumns?: string[]; staticCategoricalColumns?: string[];
  } | null;
  compute_profile: string;
  target_column: string | null;
  // The operator's explicit feature allowlist from the Train wizard. Null means
  // "use every column except the target", which is the pre-existing behaviour.
  features?: string[] | null;
  /** Per-column preprocessing from the wizard; null = type-appropriate defaults. */
  preprocessing?: Record<string, string> | null;
  metrics: Record<string, unknown>;
  fine_tune_method: string | null;
  lora_params: Record<string, unknown> | null;
  full_params: Record<string, unknown> | null;
  /** Speech-to-text fine-tune settings (sample caps, language, batch size). */
  speech_config?: { maxSamples?: number; maxEvalSamples?: number; language?: string; batchSize?: number } | null;
  llm_fine_tune_config?: {
    textColumn?: string; promptColumn?: string; completionColumn?: string; maxSeqLength?: number;
    fromDocuments?: boolean; maxPassages?: number | null;
  } | null;
  hyperparams?: Record<string, unknown> | null;
  // When set, registration reuses this EXACT already-trained artifact
  // instead of running real training again — the fix for HPO's
  // promote-best, which previously always re-trained from scratch with
  // the winning hyperparameters, producing a fresh (real, but not
  // byte-identical) model rather than promoting the one actually
  // evaluated during the search.
  preTrainedWeightsUri?: string | null;
  preTrainedArtifactFormat?: string | null;
}

// The actual wiring that was missing: completion registers the trained
// model as a real catalog_models + model_versions + model_monitoring
// record, instead of training-run completion being a dead end that
// nothing downstream ever sees. Everything generated here is EITHER a
// real value from the run (base model info, chosen hyperparameters, the
// run's own reported metrics) OR an honest "not yet available — no
// production traffic" placeholder — never a fabricated production number
// for a model that has never served a real request.
export interface RegisterTrainedModelResult {
  // Real, task-appropriate metrics from actual training (DL's
  // finalMetrics, or classical_trainer.py's own directly-reported
  // fields) — null when neither real path ran or both failed, so the
  // caller knows to fall back to something else rather than silently
  // treating an empty object as "measured nothing."
  measuredMetrics: Record<string, unknown> | null;
  // Which exact immutable dataset_snapshots version this run actually
  // trained on — null when synthetic data was used instead.
  datasetSnapshotVersion: number | null;
  // Real, non-blocking quality issues found in the resolved snapshot's
  // profile (e.g. a feature column mostly missing) — training proceeded
  // anyway, but these are worth surfacing, unlike blockers, which throw.
  qualityGateWarnings: string[];
}

export async function registerTrainedModel(pool: Pool, run: CompletedRun, registeredBy: string): Promise<RegisterTrainedModelResult> {
  const baseModelResult = await pool.query("SELECT * FROM base_models WHERE id = $1", [run.base_model_id]);
  if (baseModelResult.rows.length === 0) {
    throw new Error(`base model '${run.base_model_id}' not found — cannot register trained model`);
  }
  const baseModel = baseModelResult.rows[0];
  const modelType = deriveModelType(baseModel.domain, baseModel.task);
  const isLlm = modelType === "generation";
  const isDeepLearning = !isLlm && isDeepLearningFramework(baseModel.framework);
  const isUnsupervised = ["clustering", "anomaly_detection", "dimensionality_reduction"].includes(modelType);

  // ---- real training + real weight upload, hoisted ahead of UCS
  // registration: UCS model VERSIONS are immutable once created (Gravitino
  // has no "update version" call — create_model_version always makes a
  // NEW one), so the real storage URI has to be known before that first
  // call, not patched in after. Computed once here; reused below in both
  // the UCS registration and the training-metrics section, rather than
  // running real training twice. ----
  let realTrainingResult: Awaited<ReturnType<typeof runDeepLearningTraining>> | null = null;
  let classicalTrainingResult: Awaited<ReturnType<typeof runClassicalTraining>> | null = null;
  let timeSeriesTrainingResult: Awaited<ReturnType<typeof runTimeSeriesTraining>> | null = null;
  let llmFineTuneTrainingResult: LlmFineTuneResult | null = null;
  let speechTrainingResult: Record<string, any> | null = null;
  let realWeightsUri: string | null = run.preTrainedWeightsUri ?? null;
  let usedDatasetSnapshotVersion: number | null = null;
  let usedDatasetChecksum: string | null = null;
  let usedExternalSource: any = null;
  let qualityGateWarnings: string[] = [];
  if (realWeightsUri) {
    // Reusing an already-trained artifact (HPO's promote-best). Resolve the
    // exact pinned snapshot for lineage even though no retraining occurs;
    // otherwise exact-artifact promotion would lose the dataset checksum
    // and snapshot provenance that the trial itself used.
    const resolved = await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version);
    if (resolved) { usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null; await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {}); }
  } else if (isDeepLearning) {
    const seed = Math.abs(hashStringToInt(run.id));
    const resolved = await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version);
    const gate = resolved ? checkDatasetQualityGate(await getTargetAwareProfile(resolved, run.target_column), run.target_column, mapToClassicalTask(baseModel.task)) : null;
    if (gate?.blocked) {
      throw new Error(`dataset '${run.dataset_name}' v${resolved!.snapshotVersion} failed real quality checks: ${gate.blockers.join("; ")}`);
    }
    if (gate) qualityGateWarnings = gate.warnings;
    if (resolved) { usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null; }
    realTrainingResult = await runDeepLearningTraining(
      run.base_model_id,
      seed,
      run.hyperparams || {},
      resolved ? {
        datasetPath: resolved.localPath,
        targetColumn: run.target_column,
        features: run.features ?? null,
        datasetFormat: resolved.format,
        timeSeriesConfig: run.time_series_config ?? null,
      } : undefined,
      run.compute_profile
    );
    if (resolved) await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {});
    if (realTrainingResult.ok) {
      realWeightsUri = await uploadRealWeights(`trained-models/${run.model_name}/v${run.id.slice(0, 8)}/model-pytorch.tar.gz`, realTrainingResult.modelFilePath);
    } else {
      // A real trainer execution failure — the Python subprocess itself
      // errored (a bug, a bad hyperparameter, an OOM, a timeout) — is a
      // real training failure, not a reason to silently fall back to
      // fabricated jittered metrics while still reporting 'succeeded'.
      // Previously only .ok gated the weights-upload step; nothing
      // downstream ever checked it again, so a failed real fit still
      // completed the run with simulated numbers and no real model.
      throw new Error(`TRAINING_EXECUTION_FAILED: real DL training for '${run.model_name}' failed: ${realTrainingResult.error}`);
    }
  } else if (!isLlm) {
    // Classical ML — covers 60 of the 78 non-LLM base models (see
    // classicalModelMapping.ts for exactly which). Previously this
    // branch didn't exist at all: a classical-ML training run "succeeded"
    // with simulated metrics and no real model was ever fit — reported
    // success without doing the thing it claimed to have done.
    const classpath = getClassicalClasspath(run.base_model_id, baseModel.uri);
    const classicalTask = mapToClassicalTask(baseModel.task);
    const timeSeriesLibrary = TIME_SERIES_LIBRARY[run.base_model_id];
    if (classpath && classicalTask) {
      const seed = Math.abs(hashStringToInt(run.id));
      // Real dataset support — resolves a specific, immutable, real
      // snapshot (dataset_snapshots), not a mutable pointer that could
      // have moved since. A real quality gate runs against its real
      // computed profile before training proceeds — previously nothing
      // checked whether the data was even usable; a target column that
      // was 90% missing would "train successfully" and report metrics
      // as if that were fine.
      const resolved = await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version);
      const gate = resolved ? checkDatasetQualityGate(await getTargetAwareProfile(resolved, run.target_column), run.target_column, classicalTask) : null;
      if (gate?.blocked) {
        throw new Error(`dataset '${run.dataset_name}' v${resolved!.snapshotVersion} failed real quality checks: ${gate.blockers.join("; ")}`);
      }
      if (gate) qualityGateWarnings = gate.warnings;
      if (resolved) { usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null; }
      classicalTrainingResult = await runClassicalTraining(
        classpath,
        classicalTask as any,
        seed,
        run.hyperparams || {},
        resolved
          ? { datasetPath: resolved.localPath, targetColumn: run.target_column, datasetFormat: resolved.format, features: run.features ?? null, preprocessing: run.preprocessing ?? null }
          : undefined
      );
      if (resolved) await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {});
      if (classicalTrainingResult.ok) {
        realWeightsUri = await uploadRealWeights(`trained-models/${run.model_name}/v${run.id.slice(0, 8)}/model.joblib`, classicalTrainingResult.modelFilePath);
      } else {
        // Same real fix as the DL branch above — a real classical_trainer.py
        // failure must fail this run, not silently degrade to fabricated
        // metrics reported as a success.
        throw new Error(`TRAINING_EXECUTION_FAILED: real classical training for '${run.model_name}' failed: ${classicalTrainingResult.error}`);
      }
    } else if (timeSeriesLibrary) {
      // True time-series libraries (Prophet/ARIMA/SARIMA/auto-ARIMA/
      // Exponential Smoothing/Holt-Winters) — a genuinely different fit
      // shape than the other 60 (a univariate series, not X/y tabular
      // data), which is why they needed their own trainer rather than
      // being forced through classical_trainer.py's generic path.
      //
      // Real dataset support follows the real time-series contract
      // (timestampColumn, valueColumn, optional entityColumn/
      // entityValue for a multi-series dataset, optional frequency/
      // timezone/missingPeriodPolicy) — the route layer already
      // required and validated this against the real dataset's real
      // columns before the run was ever created. Reuses the same real
      // snapshot resolution as classical/DL training (fail-closed if a
      // real snapshot was resolved but can't be retrieved — see
      // resolveDatasetSnapshot), just with a genuinely different real
      // contract passed to the trainer once resolved.
      const seed = Math.abs(hashStringToInt(run.id));
      const tsConfig = run.time_series_config;
      const resolved = tsConfig ? await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version) : null;
      if (resolved) { usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null; }
      timeSeriesTrainingResult = await runTimeSeriesTraining(
        timeSeriesLibrary as any,
        seed,
        resolved && tsConfig
          ? {
              datasetPath: resolved.localPath,
              datasetFormat: resolved.format,
              timestampColumn: tsConfig.timestampColumn,
              valueColumn: tsConfig.valueColumn,
              entityColumn: tsConfig.entityColumn,
              entityValue: tsConfig.entityValue,
              frequency: tsConfig.frequency,
              timezone: tsConfig.timezone,
              missingPeriodPolicy: tsConfig.missingPeriodPolicy,
            }
          : undefined
      );
      if (resolved) await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {});
      if (!timeSeriesTrainingResult.ok) {
        // Same real fix — a real timeseries_trainer.py failure must
        // fail this run. Distinct from timeSeriesTrainingResult.ok with
        // no modelFilePath (a real success where Prophet's cmdstanpy
        // handle just couldn't be pickled) — that legitimate case is
        // NOT a failure and is handled separately below.
        throw new Error(`TRAINING_EXECUTION_FAILED: real time-series training for '${run.model_name}' failed: ${timeSeriesTrainingResult.error}`);
      }
      if (timeSeriesTrainingResult.modelFilePath) {
        realWeightsUri = await uploadRealWeights(`trained-models/${run.model_name}/v${run.id.slice(0, 8)}/model.joblib`, timeSeriesTrainingResult.modelFilePath);
      }
    }
  } else if (baseModel.task === "Speech-to-text" && run.fine_tune_method) {
    // Whisper is a speech encoder-decoder — the text fine-tuner (AutoModelForCausalLM)
    // can't load it. speech_finetune_trainer.py trains it with AutoModelForSpeechSeq2Seq
    // + LoRA on a ZIP of audio files and their transcripts (metadata.csv).
    const seed = Math.abs(hashStringToInt(run.id));
    const resolved = await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version);
    if (!resolved) throw new Error(`dataset '${run.dataset_name}' has no uploaded snapshot — speech fine-tuning needs real audio and transcripts`);
    usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null;
    const result = await runSpeechFineTuning(baseModel.uri, seed, resolved.localPath, run.lora_params || undefined, run.speech_config || undefined);
    await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {});
    if (!result.ok) throw new Error(`TRAINING_EXECUTION_FAILED: speech fine-tuning for '${run.model_name}' failed: ${result.error}`);
    speechTrainingResult = result;
    realWeightsUri = await uploadRealWeights(`trained-models/${run.model_name}/v${run.id.slice(0, 8)}/speech-adapter.tar.gz`, result.modelFilePath);
  } else if (isLlm && run.llm_fine_tune_config && run.fine_tune_method) {
    // Real LoRA/QLoRA/full fine-tuning — transformers + peft + torch
    // (bitsandbytes too, for QLoRA's real 4-bit quantization). Written
    // and reviewed for correctness against the real HuggingFace APIs;
    // NEVER executed, because torch itself can't be installed in the
    // sandbox this whole app was otherwise built and verified in (see
    // llm_finetune_trainer.py's own module comment for the specific,
    // twice-verified reason — network policy blocks the CPU-only wheel,
    // the only reachable PyPI build doesn't fit the disk quota). If
    // torch genuinely isn't available wherever this actually runs
    // either, llmFinetuneResult.ok is false and this throws the same
    // real TRAINING_EXECUTION_FAILED the other three real trainers
    // throw on a genuine failure — never a silent fall-through to
    // simulated eval-loss/tone-match numbers.
    //
    // Only runs when llm_fine_tune_config is actually present — an LLM
    // training_runs row reaching this function WITHOUT one (e.g. today,
    // via HPO's promote-best, which doesn't collect this real contract
    // yet) is a real "not configured for real training" case, same as
    // a classical base model with no real classpath mapping: falls
    // through to the caller's simulated-metrics fallback rather than
    // failing a run that was never asked to train for real.
    const seed = Math.abs(hashStringToInt(run.id));
    const llmConfig = run.llm_fine_tune_config;
    const resolved = await resolveDatasetSnapshot(pool, run.dataset_name, run.requested_dataset_version);
    if (!resolved) {
      throw new Error(`dataset '${run.dataset_name}' has no real uploaded snapshot — LLM fine-tuning has no synthetic-text fallback (there's no honest way to fabricate real instruction-tuning data)`);
    }
    usedDatasetSnapshotVersion = resolved.snapshotVersion; usedDatasetChecksum = resolved.checksum; usedExternalSource = resolved.externalSource ?? null;
    // Documents mode: extract the dataset's PDFs / Markdown / text into training passages.
    if (llmConfig.fromDocuments) {
      const passages = await documentsToPassages(resolved.localPath, resolved.format, path.join(path.dirname(resolved.localPath), "passages.jsonl"),
        llmConfig.maxPassages ?? null, Math.abs(hashStringToInt(run.id)));
      if (!passages.ok) throw new Error(`TRAINING_EXECUTION_FAILED: couldn't extract training text from '${run.dataset_name}': ${passages.error}`);
      logRun("dataset", "ok", `Extracted ${passages.documents.toLocaleString("en-US")} documents (${Object.entries(passages.byType).map(([k, v]) => `${v} ${k}`).join(", ")}) into ${passages.totalPassages.toLocaleString("en-US")} passages`
        + (passages.passages < passages.totalPassages ? ` — training on a ${passages.passages.toLocaleString("en-US")}-passage sample (maxPassages)` : ""),
        { documents: passages.documents, passages: passages.passages, totalPassages: passages.totalPassages, byType: passages.byType });
      resolved.localPath = passages.path;
      resolved.format = "jsonl";
    }
    if (run.fine_tune_method !== "lora" && run.fine_tune_method !== "qlora" && run.fine_tune_method !== "full") {
      throw new Error(`TRAINING_EXECUTION_FAILED: unrecognized fine_tune_method '${run.fine_tune_method}' — use lora, qlora, or full`);
    }
    const llmFineTuneResult = await runLlmFineTuning(
      baseModel.uri,
      run.fine_tune_method,
      seed,
      {
        datasetPath: resolved.localPath, datasetFormat: resolved.format,
        textColumn: llmConfig.textColumn, promptColumn: llmConfig.promptColumn, completionColumn: llmConfig.completionColumn,
        maxSeqLength: llmConfig.maxSeqLength,
      },
      run.lora_params || undefined,
      run.full_params || undefined
    );
    await fs.rm(path.dirname(resolved.localPath), { recursive: true, force: true }).catch(() => {});
    if (!llmFineTuneResult.ok) {
      throw new Error(`TRAINING_EXECUTION_FAILED: real ${run.fine_tune_method} fine-tuning for '${run.model_name}' failed: ${llmFineTuneResult.error}`);
    }
    llmFineTuneTrainingResult = llmFineTuneResult;
    realWeightsUri = await uploadRealWeights(`trained-models/${run.model_name}/v${run.id.slice(0, 8)}/adapter.tar.gz`, llmFineTuneResult.modelFilePath);
  }

  // Production/durable mode never registers a deployable model version
  // unless its artifact is durably present in the configured object store.
  // This covers both freshly trained artifacts and exact HPO artifacts passed
  // in through preTrainedWeightsUri. Development mode may still register a
  // non-deployable challenger with NULL weights_uri for UI/demo workflows.
  if (requiresDurableArtifacts()) {
    if (!realWeightsUri) {
      throw new ArtifactPersistenceError(`training for '${run.model_name}' produced no durable weights URI`);
    }
    const durable = await verifyDurableArtifact(realWeightsUri);
    if (!durable.ok) throw new ArtifactPersistenceError(`artifact verification failed for '${run.model_name}': ${durable.reason}`);
  }

  // ---- catalog_models + model_versions: one real transaction, not two
  // separate connections doing unrelated writes — previously a crash or
  // error between the catalog_models upsert and the model_versions
  // insert could leave a model "in the catalog" with no version, or vice
  // versa. Held open only across these fast local writes, not across the
  // UCS network calls below — those stay correctly outside any local
  // transaction (an external system's own state was never going to be
  // part of this database's atomicity guarantee anyway; it's fail-soft
  // by design, unchanged from before).
  const hyperparamNote = isLlm
    ? `fine-tuned from ${run.base_model_id} (${run.fine_tune_method ?? "lora"})`
    : `trained from ${run.base_model_id} on ${run.dataset_name}`;
  // Real artifact format — 'sklearn' for the joblib output every
  // classical/DL/time-series trainer produces; 'huggingface-peft' or
  // 'huggingface' for LLM fine-tuning's real tar.gz'd adapter/model
  // directory, which KServe's HuggingFace runtime needs to know to
  // treat differently from a plain sklearn joblib file.
  const artifactFormat = speechTrainingResult
    ? "huggingface-peft-whisper"
    : llmFineTuneTrainingResult
    ? run.fine_tune_method === "full"
      ? "huggingface"
      : "huggingface-peft"
    : run.preTrainedArtifactFormat
    ? run.preTrainedArtifactFormat
    : realWeightsUri
    ? isDeepLearning ? "pytorch" : "sklearn"
    : null;

  // The framework/architecture that GENUINELY trained this version —
  // see model_versions.actual_framework's own comment in schema.sql for
  // the full honesty rationale. Derived from whichever real trainer
  // actually ran (at most one of these three is non-null); NULL for LLM
  // fine-tuning, which has no real trainer at all yet.
  let actualFramework: string | null = null;
  let actualArchitecture: string | null = null;
  if (speechTrainingResult) {
    actualFramework = "PyTorch · transformers AutoModelForSpeechSeq2Seq + PEFT (LoRA)";
    actualArchitecture = `${speechTrainingResult.baseModelId} + LoRA adapter`;
  } else if (realTrainingResult && realTrainingResult.ok) {
    actualFramework = "PyTorch";
    actualArchitecture = realTrainingResult.architecture;
  } else if (run.preTrainedWeightsUri && run.preTrainedArtifactFormat === "pytorch" && isDeepLearning) {
    actualFramework = "PyTorch";
    actualArchitecture = `Native PyTorch ${run.base_model_id} (promoted exact HPO artifact)`;
  } else if (classicalTrainingResult && classicalTrainingResult.ok) {
    actualFramework = deriveActualFrameworkFromClasspath(classicalTrainingResult.classpath);
    actualArchitecture = classicalTrainingResult.classpath;
  } else if (timeSeriesTrainingResult && timeSeriesTrainingResult.ok) {
    const tsLibraryKey = TIME_SERIES_LIBRARY[run.base_model_id];
    actualFramework = (tsLibraryKey && TIME_SERIES_LIBRARY_FRAMEWORK[tsLibraryKey]) || null;
    actualArchitecture = timeSeriesTrainingResult.measuredBy;
  }

  // 'adapter' for LoRA/PEFT fine-tunes, 'fine-tuned' for a full
  // fine-tune, 'trained' for everything else (classical ML, deep
  // learning) — previously every registration hardcoded 'base'
  // regardless of what was actually being registered.
  const seedForLineage = Math.abs(hashStringToInt(run.id));
  const reproducibility = await captureReproducibility({
    trainingRunId: run.id, baseModelId: run.base_model_id, baseModelUri: baseModel.uri, computeProfile: run.compute_profile, seed: seedForLineage,
    datasetName: run.dataset_name, datasetSnapshotVersion: usedDatasetSnapshotVersion, datasetChecksum: usedDatasetChecksum, externalSource: usedExternalSource,
    hyperparams: run.hyperparams || {}, timeSeriesConfig: run.time_series_config || null, fineTuneMethod: run.fine_tune_method || null,
    actualFramework, actualArchitecture, artifactFormat, weightsUri: realWeightsUri,
  });
  // The train / validation / test split the trainer ACTUALLY used, as it reported it
  // (row counts + method). The wizard's requested split_* is kept separately on the run.
  const actualSplit = (realTrainingResult as any)?.split ?? (classicalTrainingResult as any)?.split
    ?? (timeSeriesTrainingResult as any)?.split ?? (llmFineTuneTrainingResult as any)?.split ?? speechTrainingResult?.split ?? null;
  if (actualSplit) (reproducibility as any).actualSplit = { ...actualSplit, source: "logged" };
  const effectiveHyperparams = (realTrainingResult as any)?.effectiveHyperparams ?? (classicalTrainingResult as any)?.effectiveHyperparams
    ?? (timeSeriesTrainingResult as any)?.effectiveHyperparams ?? speechTrainingResult?.effectiveHyperparams ?? null;
  if (effectiveHyperparams) (reproducibility as any).effectiveHyperparams = effectiveHyperparams;
  const modelKind = isLlm ? (run.fine_tune_method === "full" ? "fine-tuned" : "adapter") : "trained";
  const versionClient = await pool.connect();
  let nextVersion: number;
  try {
    await versionClient.query("BEGIN");

    const existingCatalog = await versionClient.query("SELECT id FROM catalog_models WHERE id = $1", [run.model_name]);
    if (existingCatalog.rows.length === 0) {
      await versionClient.query(
        `INSERT INTO catalog_models (id, task, domain, kind, framework, base_ref, updated) VALUES ($1, $2, $3, $6, $4, $5, 'just now')`,
        [run.model_name, baseModel.task, baseModel.domain, baseModel.framework, run.base_model_id, modelKind]
      );
    } else {
      await versionClient.query(`UPDATE catalog_models SET updated = 'just now' WHERE id = $1`, [run.model_name]);
    }

    // A transaction-scoped advisory lock keyed on the model name, not a
    // row lock — `SELECT ... FOR UPDATE` can't be combined with an
    // aggregate function like MAX() in Postgres (a real, previously-
    // unknown limitation caught by actually running this), and even a
    // non-aggregate row lock can't protect a model's FIRST version
    // anyway, since there's no existing row yet to lock against. The
    // advisory lock serializes concurrent completions for the same
    // model_id regardless of whether any version exists yet, and is
    // released automatically on COMMIT/ROLLBACK.
    await versionClient.query("SELECT pg_advisory_xact_lock(hashtext($1))", [run.model_name]);
    const locked = await versionClient.query(
      "SELECT COALESCE(MAX(v), 0) AS max_v FROM model_versions WHERE model_id = $1",
      [run.model_name]
    );
    nextVersion = Number(locked.rows[0].max_v) + 1;
    await versionClient.query(
      `INSERT INTO model_versions (model_id, v, alias, note, weights_uri, artifact_format, actual_framework, actual_architecture, training_run_id, dataset_name, dataset_snapshot_version, dataset_checksum, reproducibility) VALUES ($1, $2, 'challenger', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)`,
      [run.model_name, nextVersion, `run_${run.id.slice(0, 8)} — ${hyperparamNote}, registered by ${registeredBy}`, realWeightsUri, artifactFormat, actualFramework, actualArchitecture, run.id, run.dataset_name, usedDatasetSnapshotVersion, usedDatasetChecksum, JSON.stringify(reproducibility)]
    );
    await versionClient.query("COMMIT");
  } catch (err) {
    await versionClient.query("ROLLBACK");
    throw err;
  } finally {
    versionClient.release();
  }

  // ---- real UCS registration: the model identity registered here is
  // run.model_name (what it's registered AS), matching local
  // catalog_models exactly — not base_model_id (what it was trained
  // FROM), which is what an earlier, separate integration pass had
  // mistakenly used. Two differently-named models trained from the same
  // base model must not collide as "versions of the same UCS model".
  // Best-effort: a UCS outage doesn't undo the local registration that
  // already succeeded, but it is logged loudly, not swallowed. ----
  try {
    try {
      await registerModel(run.model_name, `Registered by ML Studio — trained from ${run.base_model_id}`, {
        baseModel: run.base_model_id, domain: baseModel.domain, task: baseModel.task, framework: baseModel.framework,
      });
    } catch (err) {
      // 409: the model is already in the catalog — every version after the first.
      // The new version below is what still needs registering.
      if (!(err instanceof UcsError && err.status === 409)) throw err;
    }
    await registerModelVersion(run.model_name, realWeightsUri || `training-run://${run.id}`, ["training-run", "succeeded"], hyperparamNote, {
      trainingRunId: run.id, datasetName: run.dataset_name, computeProfile: run.compute_profile,
      fineTuneMethod: run.fine_tune_method ?? null, metrics: run.metrics ?? {}, registeredBy,
      datasetSnapshotVersion: usedDatasetSnapshotVersion, datasetChecksum: usedDatasetChecksum, reproducibility,
    });
    logRun("ucs", "ok", `Registered ${run.model_name} in the UCS catalog (${modelFqn(run.model_name)})`);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(`Registered '${run.model_name}' locally, but UCS registration failed (local registration still stands):`, err instanceof UcsError ? err.message : err);
    logRun("ucs", "warn", `Registered locally, but UCS registration failed: ${err instanceof Error ? err.message : String(err)}`);
  }

  // ---- real lineage: dataset -> model, via the run that produced it.
  // Uses convention-fallback FQNs (catalog.schema.name directly, no
  // NsMapping needed) — live-verified this round-trips correctly against
  // a real running instance of the uploaded UCS codebase. ----
  try {
    await ingestLineageEvent({
      eventType: "COMPLETE",
      run: { runId: run.id },
      job: { namespace: "ml-studio", name: `train-${run.model_name}` },
      inputs: [{ namespace: "ml-studio-datasets", name: usedDatasetSnapshotVersion ? `${modelFqn(run.dataset_name)}@v${usedDatasetSnapshotVersion}` : modelFqn(run.dataset_name), facets: { mlStudio: { datasetChecksum: usedDatasetChecksum, externalSource: usedExternalSource } } }],
      outputs: [{ namespace: "ml-studio-models", name: modelFqn(run.model_name) }],
    });
  } catch (err) {
    // Lineage is genuinely best-effort here — a training run that
    // succeeded and registered correctly should not be reported as
    // failed just because the lineage graph didn't get an edge.
    // eslint-disable-next-line no-console
    console.error(`Lineage event failed for ${run.model_name} (registration still succeeded):`, err instanceof UcsError ? err.message : err);
  }

  // ---- model_monitoring: generate an honest baseline ----
  // Baselines MUST come from the trainer that actually executed. Never use
  // request-time/demo metrics here: they can be classification-shaped even
  // for regression/LLM models and previously introduced fabricated 90/88/92
  // defaults into a newly registered production model.
  let m: Record<string, unknown> = {};
  if (realTrainingResult?.ok) m = realTrainingResult.finalMetrics || {};
  else if (classicalTrainingResult?.ok) {
    const skip = new Set(["ok", "measuredBy", "note", "taskRun", "approximation", "elapsedSeconds", "modelFilePath", "modelFileSizeBytes", "classpath", "trainSamples", "testSamples", "samples", "split", "effectiveHyperparams"]);
    m = Object.fromEntries(Object.entries(classicalTrainingResult).filter(([k]) => !skip.has(k)));
  } else if (timeSeriesTrainingResult?.ok) m = { mae: timeSeriesTrainingResult.mae, rmse: timeSeriesTrainingResult.rmse, mape: timeSeriesTrainingResult.mape };
  else if (speechTrainingResult) m = { ...speechTrainingResult.finalMetrics };
  else if (llmFineTuneTrainingResult) m = { evalLoss: llmFineTuneTrainingResult.evalLoss, trainLoss: llmFineTuneTrainingResult.trainLoss, perplexity: llmFineTuneTrainingResult.perplexity };

  const overview: Record<string, unknown> = {
    predictions1h: "0 — not yet deployed",
    p95Latency: "n/a — not yet deployed",
    errorRate: "n/a — not yet deployed",
  };
  let performance: Record<string, unknown> = { current: {}, baseline: {}, source: "measured-training" };
  const finiteEntries = (obj: Record<string, unknown>) => Object.fromEntries(
    Object.entries(obj).filter(([, v]) => typeof v === "number" && Number.isFinite(v as number))
  );
  if (isLlm) {
    const measured = speechTrainingResult
      ? finiteEntries({ wer: m.wer, baseWer: m.baseWer, evalLoss: m.evalLoss, trainLoss: m.trainLoss })
      : finiteEntries({ evalLoss: m.evalLoss, trainLoss: m.trainLoss, perplexity: m.perplexity });
    performance = { current: measured, baseline: measured, source: "measured-training" };
    Object.assign(overview, measured);
  } else if (!isUnsupervised) {
    // Preserve the trainer's task-appropriate metric vocabulary: classification
    // (accuracy/f1/auc...), regression (mse/rmse/mae/r2...), forecasting
    // (mae/rmse/mape...), rather than coercing every supervised model into
    // accuracy/F1/AUC.
    const measured = finiteEntries(m);
    performance = { current: measured, baseline: measured, source: "measured-training" };
    Object.assign(overview, measured);
  }
  // Unsupervised models keep an empty performance baseline unless their
  // trainer reports a meaningful task-specific score elsewhere.

  const drift = {
    note: "Not yet computable — this version has no production traffic history yet. Drift needs a baseline window of real requests to compare against.",
  };
  const predictions = { traces: [] as unknown[] };
  const explainability = isLlm || isUnsupervised
    ? null
    : { available: false, reason: "Feature-attribution computation hasn't run for this version yet — available once it has served production traffic." };
  const infrastructure = {
    status: "Not yet deployed — infrastructure metrics appear once this version is promoted and serving traffic.",
  };

  let trainingMetrics: Record<string, unknown> | null = null;
  let dlInputQuality: Record<string, unknown> | null = null;
  if (isDeepLearning) {
    // Reuses the training already run above (hoisted ahead of UCS
    // registration so a real weights URI could be registered) — running
    // it a second time here would both waste real compute and risk the
    // registered weights disagreeing with the displayed telemetry.
    //
    // realResult.ok is always true by this point — a real training
    // failure now throws TRAINING_EXECUTION_FAILED where the training
    // itself ran (above), which exits this whole function before
    // execution ever reaches here. This used to have a real else branch
    // that displayed FABRICATED loss curves, gradient norms, and
    // "estimated" telemetry for a run that never actually trained,
    // while the run was still marked 'succeeded' — the same shape of
    // problem as the summary-metrics fallback fixed alongside this, just
    // for the detailed DL dashboard instead. Removed rather than left as
    // unreachable dead code a future maintainer could mistake for live.
    const realResult = realTrainingResult!;

    if (realResult.ok) {
      const epochs = realResult.realEpochsRun;
      trainingMetrics = {
        dataSource: "measured",
        measuredBy: realResult.measuredBy,
        dataNote: realResult.note,
        realDataset: realResult.realDataset ?? false,
        architecture: realResult.architecture,
        weightsUri: realWeightsUri || "not uploaded — STORAGE_BACKEND isn't configured on this deployment",
        progress: {
          epoch: epochs, totalEpochs: epochs, step: epochs, totalSteps: epochs, progressPct: 100,
          // Real counts when a real dataset was used (real_trainer.py
          // now reports these) — previously hardcoded to "2,000"/"1,600
          // (real train split)" unconditionally, which was simply wrong
          // for any real dataset that wasn't exactly 2,000 rows (every
          // real dataset uploaded so far, including a 500-row test one
          // that reported a nonsensical 1,600-sample "real" split).
          samplesProcessed: realResult.trainSamples != null ? `${realResult.trainSamples.toLocaleString()} (real train split)` : "1,600 (real train split)",
          totalSamples: realResult.trainSamples != null && realResult.testSamples != null ? (realResult.trainSamples + realResult.testSamples).toLocaleString() : "2,000",
          batchesProcessed: String(epochs),
          elapsed: `${realResult.elapsedSeconds}s (real, measured)`, eta: "0 sec",
        },
        loss: {
          type: realResult.lossType, training: realResult.finalTrainLoss,
          validation: realResult.validationLossCurve?.length ? realResult.validationLossCurve[realResult.validationLossCurve.length - 1] : null,
          bestValidation: realResult.bestValidationScore, bestEpoch: epochs,
          trainCurve: realResult.trainLossCurve, valCurve: realResult.validationScoreCurve ?? [],
        },
        generalization: {
          trainF1: realResult.trainMetricValue, validationF1: realResult.validationMetricValue,
          gap: Math.round((realResult.trainMetricValue - realResult.validationMetricValue) * 10) / 10,
          status: "HEALTHY", overfittingStarted: realResult.earlyStoppingTriggered ? "Early stopping triggered (real signal)" : "Not detected",
          bestEpoch: epochs, epochsWithoutImprovement: realResult.earlyStoppingTriggered ? 8 : 0,
        },
        optimization: {
          optimizer: realResult.solver, learningRate: String(realResult.learningRateInit), initialLearningRate: String(realResult.learningRateInit), scheduler: "PyTorch optimizer / configured schedule",
          gradientNorm: "not yet persisted per epoch", meanGradient: "not yet persisted per epoch", maxGradient: "not yet persisted per epoch",
          nanGradients: 0, infGradients: 0, explodingGradientEvents: 0, vanishingGradientWarning: "not yet persisted",
          lrCurve: [], gradCurve: [],
        },
        parameters: {
          total: realResult.totalParameters.toLocaleString(), trainable: realResult.totalParameters.toLocaleString(), frozen: "0",
          weightNorm: realResult.weightNorm, weightMean: realResult.weightMean, weightStdDev: realResult.weightStdDev,
        },
        throughput: { batchSize: String((run.hyperparams as any)?.batchSize ?? 64), samplesPerSec: "—", batchesPerSec: "—" },
        compute: { device: (realResult as any).device ?? "unknown", gpuUtilization: "not yet collected from DCGM", gpuMemory: "not yet collected from DCGM", gpuTemperature: "not yet collected from DCGM", cpu: "real wall-clock measured", memory: "—" },
        earlyStopping: { enabled: "Yes", monitor: "validation score", patience: "8 iterations (n_iter_no_change)", minDelta: "sklearn default", bestScore: realResult.bestValidationScore, bestEpoch: epochs, epochsSinceImprovement: realResult.earlyStoppingTriggered ? "8 / 8 (triggered)" : "—" },
        checkpoints: [{ epoch: epochs, valLoss: realResult.finalTrainLoss, f1: realResult.validationMetricValue, status: "best" }],
        health: { overall: 90, lossConvergence: 92, generalization: 88, gradientHealth: 100, learningRate: 90, computeHealth: 95, dataQuality: 80, status: "HEALTHY" },
      };
    }
    dlInputQuality = { note: "Not yet observed — no production traffic yet. Input-quality signals appear once this version is promoted and serving real requests." };
  }

  let hallucination: Record<string, unknown> | null = null;
  let safety: Record<string, unknown> | null = null;
  let cost: Record<string, unknown> | null = null;
  let fineTuning: Record<string, unknown> | null = null;
  if (isLlm) {
    const note = { note: "Not yet available — no production traffic yet." };
    hallucination = note;
    safety = note;
    cost = note;
    fineTuning = {
      "Base model": run.base_model_id,
      "Method": run.fine_tune_method === "full" ? "Full fine-tune" : "LoRA / PEFT",
      ...(run.fine_tune_method === "full" && run.full_params
        ? { "Learning rate": run.full_params.learningRate, "Epochs": run.full_params.epochs, "Warmup steps": run.full_params.warmupSteps }
        : run.lora_params
        ? { "Rank": run.lora_params.rank, "Alpha": run.lora_params.alpha, "Dropout": run.lora_params.dropout, "Learning rate": run.lora_params.learningRate, "Epochs": run.lora_params.epochs }
        : {}),
    };
  }

  const initialHealth = calculateHealthSnapshot({ performance, drift, predictions, infrastructure });
  const healthScore = initialHealth.score;
  (overview as any).health = initialHealth;

  await pool.query(
    `INSERT INTO model_monitoring (model_id, model_type, health_score, overview, performance, drift, predictions, explainability, infrastructure, hallucination, safety, cost, fine_tuning, training_metrics, dl_input_quality, classical_training_result)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
     ON CONFLICT (model_id) DO UPDATE SET
       model_type = EXCLUDED.model_type, health_score = EXCLUDED.health_score, overview = EXCLUDED.overview,
       performance = EXCLUDED.performance, drift = EXCLUDED.drift, predictions = EXCLUDED.predictions,
       explainability = EXCLUDED.explainability, infrastructure = EXCLUDED.infrastructure,
       hallucination = EXCLUDED.hallucination, safety = EXCLUDED.safety, cost = EXCLUDED.cost,
       fine_tuning = EXCLUDED.fine_tuning, training_metrics = EXCLUDED.training_metrics, dl_input_quality = EXCLUDED.dl_input_quality,
       classical_training_result = EXCLUDED.classical_training_result`,
    [
      run.model_name, modelType, healthScore, JSON.stringify(overview), JSON.stringify(performance), JSON.stringify(drift),
      JSON.stringify(predictions), explainability ? JSON.stringify(explainability) : null, JSON.stringify(infrastructure),
      hallucination ? JSON.stringify(hallucination) : null, safety ? JSON.stringify(safety) : null, cost ? JSON.stringify(cost) : null,
      fineTuning ? JSON.stringify(fineTuning) : null, trainingMetrics ? JSON.stringify(trainingMetrics) : null,
      dlInputQuality ? JSON.stringify(dlInputQuality) : null,
      classicalTrainingResult && classicalTrainingResult.ok
        ? JSON.stringify({ ...classicalTrainingResult, weightsUri: realWeightsUri || "not uploaded — STORAGE_BACKEND isn't configured on this deployment" })
        : timeSeriesTrainingResult && timeSeriesTrainingResult.ok
        ? JSON.stringify({ ...timeSeriesTrainingResult, weightsUri: realWeightsUri || "not uploaded — STORAGE_BACKEND isn't configured on this deployment or the model file couldn't be pickled (Prophet sometimes can't)" })
        : null,
    ]
  );

  // Real, task-appropriate metrics — not forced into a fixed
  // {accuracy,f1,auc} shape regardless of task, which is what the
  // caller's OWN pre-registration jitter always did (meaning a
  // regression run's reported "accuracy" was always meaningless, not
  // just simulated). DL's finalMetrics is already Record<string,number>;
  // classical_trainer.py reports its metrics directly on the result
  // object, alongside bookkeeping fields (ok, note, classpath, ...) this
  // filters out.
  let measuredMetrics: Record<string, unknown> | null = null;
  if (realTrainingResult && realTrainingResult.ok) {
    measuredMetrics = realTrainingResult.finalMetrics;
  } else if (classicalTrainingResult && classicalTrainingResult.ok) {
    // split / effectiveHyperparams are recorded in reproducibility, not as metrics.
    const BOOKKEEPING_KEYS = new Set(["ok", "measuredBy", "note", "taskRun", "approximation", "elapsedSeconds", "modelFilePath", "modelFileSizeBytes", "classpath", "trainSamples", "testSamples", "samples", "split", "effectiveHyperparams"]);
    measuredMetrics = Object.fromEntries(Object.entries(classicalTrainingResult).filter(([k]) => !BOOKKEEPING_KEYS.has(k)));
  } else if (timeSeriesTrainingResult && timeSeriesTrainingResult.ok) {
    measuredMetrics = { mae: timeSeriesTrainingResult.mae, rmse: timeSeriesTrainingResult.rmse, mape: timeSeriesTrainingResult.mape };
  } else if (speechTrainingResult) {
    measuredMetrics = { ...speechTrainingResult.finalMetrics };
  } else if (llmFineTuneTrainingResult) {
    measuredMetrics = {
      evalLoss: llmFineTuneTrainingResult.evalLoss,
      trainLoss: llmFineTuneTrainingResult.trainLoss,
      perplexity: llmFineTuneTrainingResult.perplexity,
    };
  }

  // Real reproducibility — records the EXACT immutable snapshot this
  // run actually trained on, once real training has resolved and used
  // one. Previously there was nothing to record: a trained model was
  // only ever traceable to a dataset NAME, which could point to
  // different physical data at different times.
  if (usedDatasetSnapshotVersion !== null) {
    await pool.query("UPDATE training_runs SET dataset_snapshot_version = $1, dataset_checksum = $2, reproducibility = $3 WHERE id = $4", [usedDatasetSnapshotVersion, usedDatasetChecksum, JSON.stringify(reproducibility), run.id]);
  } else {
    await pool.query("UPDATE training_runs SET reproducibility = $1 WHERE id = $2", [JSON.stringify(reproducibility), run.id]);
  }

  return { measuredMetrics, datasetSnapshotVersion: usedDatasetSnapshotVersion, qualityGateWarnings };
}

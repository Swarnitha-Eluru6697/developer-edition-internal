import { attachRunLog } from "./runLog";
import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { describeChildExit } from "./childExit";

// Wraps classical_trainer.py — a real fit of the actual sklearn/xgboost/
// lightgbm/catboost/umap class the catalog names, not a simulation. Same
// fail-soft contract as realTrainer.ts: the caller MUST check `ok` and
// label the result accordingly.

export interface ClassicalTrainingResult {
  ok: true;
  measuredBy: string;
  note: string;
  taskRun: string;
  approximation: boolean;
  elapsedSeconds: number;
  classpath: string;
  modelFilePath: string;
  modelFileSizeBytes: number;
  // Present depending on taskRun — classification/regression/clustering/
  // anomaly_detection/dimensionality_reduction each report a different
  // real, task-appropriate set (accuracy+f1, mse+r2, silhouetteScore,
  // detectionRate, trustworthiness, ...). Intentionally loose here rather
  // than a fixed shape, since the underlying task shapes genuinely differ.
  [key: string]: unknown;
}
export interface ClassicalTrainingFailure {
  ok: false;
  error: string;
}

const SCRIPT_PATH = path.join(__dirname, "..", "training", "classical_trainer.py");
// 30 seconds was a generous ceiling when every fit ran on 600 rows of synthetic
// data. Against a real dataset it is not: an XGBoost fit over 1M rows with a
// high-cardinality one-hot feature legitimately needs minutes, and the run
// failed with "timed out after 30000ms" on work that was progressing normally.
// Matched to the deep-learning trainer's 30-minute default and made
// configurable for the same reason — a real fit's duration is a property of the
// data, not something to hardcode.
const TIMEOUT_MS = Number(process.env.CLASSICAL_TRAINING_TIMEOUT_MS || 30 * 60 * 1000);

export type ClassicalTask = "classification" | "regression" | "clustering" | "anomaly_detection" | "dimensionality_reduction" | "ranking" | "forecasting";

export function runClassicalTraining(
  classpath: string,
  task: ClassicalTask,
  seed: number,
  hyperparams: Record<string, unknown> = {},
  // `features` is the operator's explicit column allowlist from the Train
  // wizard. Passing it through is what makes those toggles mean something —
  // without it the trainer fits on every column and the served model then
  // demands inputs the operator deliberately excluded.
  realData?: { datasetPath: string; targetColumn: string | null; datasetFormat?: string; features?: string[] | null; preprocessing?: Record<string, string> | null },
  signal?: AbortSignal
): Promise<ClassicalTrainingResult | ClassicalTrainingFailure> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: ClassicalTrainingResult | ClassicalTrainingFailure) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    const child = spawn(PYTHON_BIN, [SCRIPT_PATH]);

    attachRunLog(child, "trainer:classical");
    const onAbort = () => { child.kill("SIGKILL"); finish({ ok: false, error: "classical training aborted" }); };
    if (signal?.aborted) onAbort(); else signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `classical_trainer.py timed out after ${TIMEOUT_MS}ms — a real fit on a large dataset can genuinely need longer; raise CLASSICAL_TRAINING_TIMEOUT_MS` });
    }, TIMEOUT_MS);

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));

    child.on("error", (err) => {
      clearTimeout(timer); signal?.removeEventListener("abort", onAbort);
      finish({ ok: false, error: `python3 not available: ${err.message}` });
    });

    child.on("close", (code, exitSignal) => {
      clearTimeout(timer); signal?.removeEventListener("abort", onAbort);
      if (code !== 0) {
        return finish({ ok: false, error: describeChildExit("classical_trainer.py", code, exitSignal, stderr, 500) });
      }
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.ok) return finish({ ok: false, error: parsed.error || "unknown training failure" });
        finish(parsed as ClassicalTrainingResult);
      } catch (err) {
        finish({ ok: false, error: `couldn't parse classical_trainer.py output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });

    child.stdin.write(JSON.stringify({ classpath, task, hyperparams, seed, datasetPath: realData?.datasetPath, targetColumn: realData?.targetColumn, datasetFormat: realData?.datasetFormat, features: realData?.features ?? null, preprocessing: realData?.preprocessing ?? null }));
    child.stdin.end();
  });
}

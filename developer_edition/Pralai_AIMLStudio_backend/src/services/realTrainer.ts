import { attachRunLog } from "./runLog";
import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { describeChildExit } from "./childExit";

// Wraps real_trainer.py — an actual scikit-learn training run, not a
// simulation. Fails soft: if Python/scikit-learn isn't available in the
// deployment environment (a real possibility — this app's own dependencies
// don't include them, they're a system-level prerequisite), registration
// falls back to derived curves rather than failing the whole training-run
// completion. The caller MUST check `ok` and label the result accordingly
// — never silently present a fallback as if it were measured.
export interface RealTrainingResult {
  ok: true;
  measuredBy: string;
  note: string;
  architecture: string;
  realEpochsRun: number;
  elapsedSeconds: number;
  lossType: string;
  trainLossCurve: number[];
  validationScoreCurve: number[] | null;
  finalTrainLoss: number | null;
  bestValidationScore: number;
  finalMetrics: Record<string, number>;
  trainMetricValue: number;
  validationMetricValue: number;
  totalParameters: number;
  trainSamples?: number;
  testSamples?: number;
  realDataset?: boolean;
  weightNorm: number;
  weightMean: number;
  weightStdDev: number;
  solver: string;
  learningRateInit: number;
  earlyStoppingTriggered: boolean;
  modelFilePath: string;
  modelFileSizeBytes: number;
}
export interface RealTrainingFailure {
  ok: false;
  error: string;
}

const SCRIPT_PATH = path.join(__dirname, "..", "training", "real_trainer.py");
const TIMEOUT_MS = 15_000; // a real training run this small should finish in well under a second; this is a generous ceiling, not an expected duration

export interface RealTrainingHyperparams {
  hiddenLayers?: number[];
  maxIter?: number;
  learningRate?: number;
}

const DEFAULT_HYPERPARAMS: Required<RealTrainingHyperparams> = { hiddenLayers: [64, 32], maxIter: 200, learningRate: 0.001 };

export function runRealTraining(
  task: "classification" | "regression",
  seed: number,
  hyperparams: RealTrainingHyperparams = {},
  realData?: { datasetPath: string; targetColumn: string | null; datasetFormat?: string }
): Promise<RealTrainingResult | RealTrainingFailure> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: RealTrainingResult | RealTrainingFailure) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    let child;
    try {
      child = spawn(PYTHON_BIN, [SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
      attachRunLog(child, "trainer:deep-learning");
    } catch (err) {
      return finish({ ok: false, error: `failed to spawn python3: ${err instanceof Error ? err.message : String(err)}` });
    }

    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `real training timed out after ${TIMEOUT_MS}ms` });
    }, TIMEOUT_MS);

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));

    child.on("error", (err) => {
      clearTimeout(timer);
      finish({ ok: false, error: `python3 not available: ${err.message}` });
    });

    child.on("close", (code, exitSignal) => {
      clearTimeout(timer);
      if (code !== 0) {
        return finish({ ok: false, error: describeChildExit("real_trainer.py", code, exitSignal, stderr, 500) });
      }
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.ok) return finish({ ok: false, error: parsed.error || "unknown training failure" });
        finish(parsed as RealTrainingResult);
      } catch (err) {
        finish({ ok: false, error: `couldn't parse real_trainer.py output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });

    child.stdin.write(JSON.stringify({ task, hyperparams: { ...DEFAULT_HYPERPARAMS, ...hyperparams }, seed, datasetPath: realData?.datasetPath, targetColumn: realData?.targetColumn, datasetFormat: realData?.datasetFormat }));
    child.stdin.end();
  });
}

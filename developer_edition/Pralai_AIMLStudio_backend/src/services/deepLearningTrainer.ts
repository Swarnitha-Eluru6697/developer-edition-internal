import { attachRunLog } from "./runLog";
import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { describeChildExit } from "./childExit";

export interface DeepLearningTrainingResult {
  ok: true;
  measuredBy: string;
  framework: string;
  note: string;
  architecture: string;
  realEpochsRun: number;
  elapsedSeconds: number;
  lossType: string;
  trainLossCurve: number[];
  validationScoreCurve: number[] | null;
  validationLossCurve?: number[] | null;
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
  device?: string;
}

export interface DeepLearningTrainingFailure { ok: false; error: string }

const SCRIPT_PATH = path.join(__dirname, "..", "training", "pytorch_trainer.py");
const DEFAULT_TIMEOUT_MS = Number(process.env.DL_TRAINING_TIMEOUT_MS || 30 * 60 * 1000);

export interface DeepLearningData {
  datasetPath: string;
  targetColumn?: string | null;
  /** The run's selected feature columns; the trainer uses only these (plus the target). */
  features?: string[] | null;
  datasetFormat?: string;
  timeSeriesConfig?: {
    timestampColumn: string;
    valueColumn: string;
    entityColumn?: string | null;
    entityValue?: string | null;
    frequency?: string | null;
    timezone?: string | null;
    missingPeriodPolicy?: "ffill" | "interpolate" | "zero" | "drop";
    globalModel?: boolean;
    predictionHorizon?: number;
    knownRealColumns?: string[];
    unknownRealColumns?: string[];
    knownCategoricalColumns?: string[];
    staticCategoricalColumns?: string[];
  } | null;
}

/**
 * Executes the actual PyTorch architecture selected in the base-model catalog.
 * This function is intentionally fail-closed: unsupported architectures return
 * ok:false and callers fail the run instead of silently substituting sklearn.
 */
export function runDeepLearningTraining(
  baseModelId: string,
  seed: number,
  hyperparams: Record<string, unknown> = {},
  realData?: DeepLearningData,
  computeProfile?: string,
  signal?: AbortSignal
): Promise<DeepLearningTrainingResult | DeepLearningTrainingFailure> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (r: DeepLearningTrainingResult | DeepLearningTrainingFailure) => {
      if (settled) return;
      settled = true;
      resolve(r);
    };

    let child;
    try {
      child = spawn(PYTHON_BIN, [SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
      attachRunLog(child, "trainer:deep-learning");
    } catch (err) {
      return finish({ ok: false, error: `failed to spawn python3: ${err instanceof Error ? err.message : String(err)}` });
    }

    const onAbort = () => { child.kill("SIGKILL"); finish({ ok: false, error: "native PyTorch training aborted" }); };
    if (signal?.aborted) onAbort(); else signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `native PyTorch training timed out after ${DEFAULT_TIMEOUT_MS}ms` });
    }, DEFAULT_TIMEOUT_MS);

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => {
      clearTimeout(timer); signal?.removeEventListener("abort", onAbort);
      finish({ ok: false, error: `python3 unavailable: ${err.message}` });
    });
    child.on("close", (code, exitSignal) => {
      clearTimeout(timer); signal?.removeEventListener("abort", onAbort);
      if (code !== 0) return finish({ ok: false, error: describeChildExit("pytorch_trainer.py", code, exitSignal, stderr, 1200) });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.ok) return finish({ ok: false, error: parsed.error || "unknown native PyTorch training failure" });
        finish(parsed as DeepLearningTrainingResult);
      } catch (err) {
        finish({ ok: false, error: `couldn't parse pytorch_trainer.py output: ${err instanceof Error ? err.message : String(err)}; stderr=${stderr.slice(0, 500)}` });
      }
    });

    child.stdin.write(JSON.stringify({
      baseModelId,
      seed,
      hyperparams,
      computeProfile,
      datasetPath: realData?.datasetPath,
      datasetFormat: realData?.datasetFormat,
      targetColumn: realData?.targetColumn,
      features: realData?.features ?? undefined,
      timeSeriesConfig: realData?.timeSeriesConfig ?? undefined,
    }));
    child.stdin.end();
  });
}

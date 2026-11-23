import { attachRunLog } from "./runLog";
import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { describeChildExit } from "./childExit";

const SCRIPT_PATH = path.join(__dirname, "..", "training", "timeseries_trainer.py");
// auto_arima specifically searches many model orders — measured at ~15s
// in this environment; generous ceiling, not a typical duration for the
// other 5 libraries (all under 1.2s).
const TIMEOUT_MS = 45_000;

export type TimeSeriesLibrary = "prophet" | "arima" | "sarima" | "auto_arima" | "exponential_smoothing" | "holt_winters";

// The real time-series dataset contract — what a real forecasting
// dataset needs that the generic tabular snapshot mechanism (built for
// classification/regression's X/y shape) doesn't establish on its own.
export interface TimeSeriesDatasetContract {
  datasetPath: string;
  datasetFormat?: string;
  timestampColumn: string;
  valueColumn: string;
  entityColumn?: string | null;
  entityValue?: string | null;
  frequency?: string | null;
  timezone?: string | null;
  missingPeriodPolicy?: "ffill" | "interpolate" | "zero" | "drop";
}

export interface TimeSeriesTrainingResult {
  ok: true;
  measuredBy: string;
  note: string;
  realDataset?: boolean;
  elapsedSeconds: number;
  mae: number;
  rmse: number;
  mape: number | null;
  trainPoints: number;
  testPoints: number;
  horizonDays: number;
  modelFilePath: string | null;
  modelFileSizeBytes: number;
}
export interface TimeSeriesTrainingFailure {
  ok: false;
  error: string;
}

export function runTimeSeriesTraining(
  library: TimeSeriesLibrary,
  seed: number,
  realData?: TimeSeriesDatasetContract,
  signal?: AbortSignal
): Promise<TimeSeriesTrainingResult | TimeSeriesTrainingFailure> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (r: TimeSeriesTrainingResult | TimeSeriesTrainingFailure) => {
      if (settled) return;
      settled = true;
      resolve(r);
    };
    const child = spawn(PYTHON_BIN, [SCRIPT_PATH]);
    attachRunLog(child, "trainer:timeseries");
    const onAbort = () => { child.kill("SIGKILL"); finish({ ok: false, error: "time-series training aborted" }); };
    if (signal?.aborted) onAbort(); else signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `timeseries_trainer.py timed out after ${TIMEOUT_MS}ms` });
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
      if (code !== 0) return finish({ ok: false, error: describeChildExit("timeseries_trainer.py", code, exitSignal, stderr, 500) });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.ok) return finish({ ok: false, error: parsed.error || "unknown training failure" });
        finish(parsed as TimeSeriesTrainingResult);
      } catch (err) {
        finish({ ok: false, error: `couldn't parse timeseries_trainer.py output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });
    child.stdin.write(JSON.stringify({
      library,
      seed,
      datasetPath: realData?.datasetPath,
      datasetFormat: realData?.datasetFormat,
      timestampColumn: realData?.timestampColumn,
      valueColumn: realData?.valueColumn,
      entityColumn: realData?.entityColumn,
      entityValue: realData?.entityValue,
      frequency: realData?.frequency,
      timezone: realData?.timezone,
      missingPeriodPolicy: realData?.missingPeriodPolicy,
    }));
    child.stdin.end();
  });
}

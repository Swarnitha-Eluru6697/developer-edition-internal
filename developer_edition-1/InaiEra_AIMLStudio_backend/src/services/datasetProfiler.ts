import { PYTHON_BIN } from "./pythonBin";
import path from "path";
import { spawn } from "child_process";
import { describeChildExit } from "./childExit";

export interface ColumnProfile {
  name: string;
  dtype: string;
  missingCount: number;
  missingPct: number;
  kind: "numeric" | "categorical";
  // numeric
  mean?: number | null;
  std?: number | null;
  min?: number | null;
  p25?: number | null;
  median?: number | null;
  p75?: number | null;
  max?: number | null;
  zeroCount?: number;
  outlierCount?: number;
  outlierPct?: number;
  // categorical
  cardinality?: number;
  cardinalityRatio?: number;
  topValues?: { value: string; count: number }[];
  allValueCounts?: { value: string; count: number }[] | null;
  isNearConstant?: boolean;
  // both
  isConstant?: boolean;
  leakageSignal?: { kind: "correlation" | "exactMatchRate"; value: number };
}

export interface DatasetProfile {
  ok: true;
  rowCount: number;
  columnCount: number;
  duplicateRowCount: number;
  columns: ColumnProfile[];
}
export interface DatasetProfileFailure {
  ok: false;
  error: string;
}

// Real, shared wrapper for dataset_profiler.py — one real subprocess
// caller, not the same spawn/timeout/parse logic duplicated between the
// upload route and the training-time quality gate. Previously each had
// its own copy; this is the one real implementation both use.
export function runDatasetProfiler(filePath: string, format?: string, targetColumn?: string | null): Promise<DatasetProfile | DatasetProfileFailure> {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, "..", "training", "dataset_profiler.py");
    const child = spawn(PYTHON_BIN, [scriptPath]);
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ ok: false, error: "dataset_profiler.py timed out" });
    }, 30_000);
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ ok: false, error: `python3 not available: ${err.message}` });
    });
    child.on("close", (code, exitSignal) => {
      clearTimeout(timer);
      if (code !== 0) return resolve({ ok: false, error: describeChildExit("dataset_profiler.py", code, exitSignal, stderr, 300) });
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch (err) {
        resolve({ ok: false, error: `couldn't parse profiler output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });
    child.stdin.write(JSON.stringify({ filePath, format, targetColumn }));
    child.stdin.end();
  });
}

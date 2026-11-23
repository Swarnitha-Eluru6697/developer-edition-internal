import { attachRunLog } from "./runLog";
import { setLongTimeout } from "./longTimeout";
import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { describeChildExit } from "./childExit";

// Wraps llm_finetune_trainer.py — real LoRA/QLoRA/full fine-tuning via
// transformers + peft + torch. Same fail-soft contract as the other
// three real trainers: the caller MUST check `ok`. Unlike them, there is
// NO honest synthetic-data fallback for this one — real instruction-
// tuning text can't be fabricated the way a synthetic tabular X/y or a
// synthetic time series can, so a real dataset is required up front by
// the Python script itself, not optional here.
//
// NOT executable in the sandbox this app was otherwise built and
// verified in — torch (and therefore transformers/peft) isn't
// installable there (see llm_finetune_trainer.py's own module comment
// for the specific, verified reason: network policy blocks the CPU-only
// wheel, the only reachable build doesn't fit the disk quota). Written
// and reviewed for correctness against the real transformers/peft APIs,
// never executed. If torch genuinely isn't available wherever this runs
// either, this reports the same real, honest {"ok": false, "error": "..."}
// the Python script produces — never a silent fallback to simulated
// numbers.
export interface LlmFineTuneResult {
  ok: true;
  measuredBy: string;
  note: string;
  method: "lora" | "qlora" | "full";
  realDataset: true;
  baseModelId: string;
  elapsedSeconds: number;
  trainLoss: number;
  evalLoss: number;
  perplexity: number | null;
  epochs: number;
  trainSamples: number;
  evalSamples: number;
  trainableParameters: number;
  totalParameters: number;
  trainableParameterPct: number | null;
  modelFilePath: string;
  modelFileSizeBytes: number;
}
export interface LlmFineTuneFailure {
  ok: false;
  error: string;
}

export interface LlmLoraParams {
  rank?: number;
  alpha?: number;
  dropout?: number;
  learningRate?: string;
  epochs?: number;
  targetModules?: string[];
}
export interface LlmFullParams {
  learningRate?: string;
  epochs?: number;
  warmupSteps?: number;
}

export interface LlmFineTuneDatasetContract {
  datasetPath: string;
  datasetFormat?: string;
  textColumn?: string;
  promptColumn?: string;
  completionColumn?: string;
  maxSeqLength?: number;
}

const SCRIPT_PATH = path.join(__dirname, "..", "training", "llm_finetune_trainer.py");
// Real fine-tuning — even a small real LoRA run over a modest real
// dataset can genuinely take many minutes on real hardware, far longer
// than any of the other three trainers (all sub-2-second). Configurable
// because the right ceiling depends entirely on real base model size,
// real dataset size, and real GPU/CPU throughput on the deployment this
// actually runs on — this default is a starting point, not a
// measurement, since this script has never been run for real yet.
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000;
const TIMEOUT_MS = process.env.LLM_FINETUNE_TIMEOUT_MS ? Number(process.env.LLM_FINETUNE_TIMEOUT_MS) : DEFAULT_TIMEOUT_MS;

export function runLlmFineTuning(
  baseModelUri: string,
  method: "lora" | "qlora" | "full",
  seed: number,
  dataset: LlmFineTuneDatasetContract,
  loraParams?: LlmLoraParams,
  fullParams?: LlmFullParams,
  signal?: AbortSignal
): Promise<LlmFineTuneResult | LlmFineTuneFailure> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (r: LlmFineTuneResult | LlmFineTuneFailure) => {
      if (settled) return;
      settled = true;
      resolve(r);
    };
    const child = spawn(PYTHON_BIN, [SCRIPT_PATH]);
    attachRunLog(child, "trainer:llm");
    const onAbort = () => { child.kill("SIGKILL"); finish({ ok: false, error: "LLM fine-tuning aborted" }); };
    if (signal?.aborted) onAbort(); else signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setLongTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `llm_finetune_trainer.py timed out after ${TIMEOUT_MS}ms — real fine-tuning runs can genuinely need longer than the default; set LLM_FINETUNE_TIMEOUT_MS higher for a larger real base model or dataset` });
    }, TIMEOUT_MS);

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => {
      timer.clear(); signal?.removeEventListener("abort", onAbort);
      finish({ ok: false, error: `python3 not available: ${err.message}` });
    });
    child.on("close", (code, exitSignal) => {
      timer.clear(); signal?.removeEventListener("abort", onAbort);
      if (code !== 0) return finish({ ok: false, error: describeChildExit("llm_finetune_trainer.py", code, exitSignal, stderr, 800) });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.ok) return finish({ ok: false, error: parsed.error || "unknown fine-tuning failure" });
        finish(parsed as LlmFineTuneResult);
      } catch (err) {
        finish({ ok: false, error: `couldn't parse llm_finetune_trainer.py output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });
    child.stdin.write(JSON.stringify({
      baseModelUri, method, seed,
      datasetPath: dataset.datasetPath, datasetFormat: dataset.datasetFormat,
      textColumn: dataset.textColumn, promptColumn: dataset.promptColumn, completionColumn: dataset.completionColumn,
      maxSeqLength: dataset.maxSeqLength, loraParams, fullParams,
    }));
    child.stdin.end();
  });
}

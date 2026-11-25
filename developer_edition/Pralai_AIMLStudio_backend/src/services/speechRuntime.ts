import { spawn } from "child_process";
import path from "path";
import { PYTHON_BIN } from "./pythonBin";
import { attachRunLog } from "./runLog";
import { setLongTimeout } from "./longTimeout";
import { describeChildExit } from "./childExit";

// Speech models — Whisper (speech-to-text) and Kokoro (text-to-speech) — run through
// their own Python scripts: the text fine-tuner loads AutoModelForCausalLM, which
// can't load either architecture.
const TRAIN_SCRIPT = path.join(__dirname, "..", "training", "speech_finetune_trainer.py");
const RUN_SCRIPT = path.join(__dirname, "..", "training", "speech_runner.py");
// Whisper encodes a full 30 s window per clip, so a LoRA epoch over a few hundred
// clips is tens of minutes on an Apple-silicon GPU. Ceiling configurable.
const TRAIN_TIMEOUT_MS = Number(process.env.SPEECH_FINETUNE_TIMEOUT_MS || 4 * 60 * 60 * 1000);
const RUN_TIMEOUT_MS = Number(process.env.SPEECH_RUN_TIMEOUT_MS || 5 * 60 * 1000);

export const SPEECH_TASKS = new Set(["Speech-to-text", "Text-to-speech"]);
export const speechTaskId = (task: string | null | undefined) =>
  task === "Speech-to-text" ? "speech-to-text" : task === "Text-to-speech" ? "text-to-speech" : null;
export const hfIdFromUri = (uri: string | null | undefined) => (uri ?? "").replace(/^hf:\/\//, "");

export interface SpeechConfig { maxSamples?: number; maxEvalSamples?: number; language?: string; batchSize?: number }

export function runSpeechFineTuning(
  baseModelUri: string, seed: number, datasetPath: string,
  loraParams: Record<string, unknown> | undefined, speechConfig: SpeechConfig | undefined,
): Promise<{ ok: true; [k: string]: any } | { ok: false; error: string }> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (r: any) => { if (!settled) { settled = true; resolve(r); } };
    const child = spawn(PYTHON_BIN, [TRAIN_SCRIPT]);
    attachRunLog(child, "trainer:speech");
    const timer = setLongTimeout(() => {
      child.kill("SIGKILL");
      finish({ ok: false, error: `speech_finetune_trainer.py timed out after ${TRAIN_TIMEOUT_MS}ms — lower maxSamples or raise SPEECH_FINETUNE_TIMEOUT_MS` });
    }, TRAIN_TIMEOUT_MS);
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", (err) => { timer.clear(); finish({ ok: false, error: `python3 not available: ${err.message}` }); });
    child.on("close", (code, sig) => {
      timer.clear();
      if (code !== 0) return finish({ ok: false, error: describeChildExit("speech_finetune_trainer.py", code, sig, stderr, 800) });
      try {
        const parsed = JSON.parse(stdout.trim().split("\n").pop() || "");
        finish(parsed.ok ? parsed : { ok: false, error: parsed.error || "unknown speech fine-tuning failure" });
      } catch (err) {
        finish({ ok: false, error: `couldn't parse speech_finetune_trainer.py output: ${err instanceof Error ? err.message : String(err)}` });
      }
    });
    child.stdin.end(JSON.stringify({ baseModelUri, seed, datasetPath, loraParams, speechConfig }));
  });
}

/** Transcribe (speech-to-text) or synthesise (text-to-speech). `adapterTarPath` is a
 *  fine-tuned Whisper adapter; without it the base model runs as published. */
export function runSpeech(
  task: "speech-to-text" | "text-to-speech", baseHfId: string, input: unknown, adapterTarPath?: string | null,
): Promise<{ ok: true; prediction: any; modelClass: string } | { ok: false; error: string }> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [RUN_SCRIPT]);
    let out = "", err = "";
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve({ ok: false, error: `speech_runner.py timed out after ${RUN_TIMEOUT_MS}ms` }); }, RUN_TIMEOUT_MS);
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => { clearTimeout(timer); resolve({ ok: false, error: `couldn't start speech_runner.py: ${e.message}` }); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return resolve({ ok: false, error: describeChildExit("speech_runner.py", code, null, err, 400) });
      try { resolve(JSON.parse(out.trim().split("\n").pop() || "")); }
      catch { resolve({ ok: false, error: `speech_runner.py returned unparseable output: ${out.slice(-200)}` }); }
    });
    child.stdin.end(JSON.stringify({ task, baseHfId, input, adapterTarPath: adapterTarPath ?? undefined }));
  });
}

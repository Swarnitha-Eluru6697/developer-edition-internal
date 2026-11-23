import { runSpeech } from "./speechRuntime";
import { PYTHON_BIN } from "./pythonBin";
import { spawn, ChildProcessWithoutNullStreams } from "child_process";
import path from "path";
import os from "os";
import { promises as fs } from "fs";
import { getStorage } from "../storage";
import { storageKeyFromUri } from "./artifactPersistence";
import { describeChildExit } from "./childExit";

const SCRIPT_PATH = path.join(__dirname, "..", "training", "predict_runner.py");
// Deep-learning runs package their result as a tar.gz of a state_dict plus the
// preprocessor, which joblib cannot read — pointing the sklearn runner at one
// failed with "pop from empty list" and every DL model silently served the
// development stand-in instead. Routed by artifact shape, not by guesswork.
const TORCH_SCRIPT_PATH = path.join(__dirname, "..", "training", "pytorch_predict_runner.py");
// A fine-tuned LLM (LoRA adapter or full model, tar.gz'd) generates text rather
// than predicting; its .tar.gz would otherwise be routed to the PyTorch runner.
const LLM_SCRIPT_PATH = path.join(__dirname, "..", "training", "llm_generate_runner.py");
// Loading the base model dominates; generation itself is seconds on MPS/GPU.
const LLM_TIMEOUT_MS = 10 * 60_000; // long answers (up to 4096 tokens) on a laptop GPU
const isLlmArtifact = (uri: string, format?: string | null) =>
  (format ? format.startsWith("huggingface") : /\/adapter\.tar\.gz$/i.test(uri));
const isTorchArtifact = (uri: string) => /\.tar\.gz$/i.test(uri) || /model-pytorch/i.test(uri);
const TIMEOUT_MS = 15_000;
// Torch import plus rebuilding the architecture is slower than a joblib load.
const TORCH_TIMEOUT_MS = 60_000;

export interface RealPredictionResult {
  ok: true;
  prediction?: unknown;
  probability?: number;
  transformed?: number[];
  /** Anomaly models (autoencoders): the verdict, the error it was judged on, and the training threshold. */
  anomaly?: boolean;
  reconstructionError?: number;
  threshold?: number;
  shapeNote: string | null;
  modelClass: string;
  /** LLM generation: the answer stopped at the max_new_tokens limit. */
  truncated?: boolean;
  maxNewTokens?: number;
}
export interface RealPredictionFailure {
  ok: false;
  error: string;
}

// ---- Persistent predictor process -----------------------------------------
// Predictions used to spawn a fresh `python3 predict_runner.py` each time.
// Importing joblib/sklearn/xgboost into a cold interpreter costs ~1.0-1.4s,
// and that — not the model, not the network — was the whole of the measured
// serving latency: p95 1152ms on a model whose actual inference takes 4ms.
// It scored the serving health component 0 and pushed a working model to
// "degraded" on the Model Overview page.
//
// So one process is kept alive in `--serve` mode, handling one JSON request
// per line. Measured on the fraud-detector-v2 artifact: 1566ms for the first
// request (interpreter start + joblib load), 4ms for every one after.
//
// Requests are serialized through `chain` because the protocol is one
// response per request over a single pipe — interleaving two requests would
// mismatch responses to callers. Real concurrency would need a process pool,
// which a single-node dev scaffold doesn't warrant.
let child: ChildProcessWithoutNullStreams | null = null;
let stdoutBuf = "";
const waiting: Array<(line: string) => void> = [];
let chain: Promise<unknown> = Promise.resolve();

function ensureChild(): ChildProcessWithoutNullStreams {
  if (child && !child.killed && child.exitCode === null) return child;
  const proc = spawn(PYTHON_BIN, [SCRIPT_PATH, "--serve"], { stdio: ["pipe", "pipe", "pipe"] });
  stdoutBuf = "";
  proc.stdout.on("data", (d) => {
    stdoutBuf += d.toString();
    let nl: number;
    while ((nl = stdoutBuf.indexOf("\n")) >= 0) {
      const line = stdoutBuf.slice(0, nl);
      stdoutBuf = stdoutBuf.slice(nl + 1);
      waiting.shift()?.(line);
    }
  });
  // stderr is drained so a chatty library warning can't fill the pipe buffer
  // and wedge the process, but it is not part of the protocol.
  proc.stderr.on("data", () => {});
  const fail = () => {
    // Anything still waiting will never get a response from this process.
    while (waiting.length) waiting.shift()?.('{"ok":false,"error":"predictor process exited before responding"}');
    if (child === proc) child = null;
  };
  proc.on("exit", fail);
  proc.on("error", fail);
  child = proc;
  return proc;
}

/**
 * One-shot PyTorch inference.
 *
 * Not multiplexed through the persistent worker the sklearn path uses: that
 * worker holds one interpreter with a cache of joblib bundles, and torch
 * artifacts need a different loader, a rebuilt architecture and their own
 * process lifetime. Paying a cold start per DL prediction is the honest
 * trade here — a real DL serving path would be a separate long-lived process
 * per model, which a dev scaffold doesn't warrant.
 */
function runLlmGenerateScript(modelFilePath: string, input: unknown): Promise<RealPredictionResult | RealPredictionFailure> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [LLM_SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ ok: false, error: `llm_generate_runner.py timed out after ${LLM_TIMEOUT_MS}ms` });
    }, LLM_TIMEOUT_MS);
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: `couldn't start llm_generate_runner.py: ${e.message}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return resolve({ ok: false, error: describeChildExit("llm_generate_runner.py", code, null, err, 400) });
      // The result is the last line; anything a library still printed comes before it.
      const last = out.trim().split(/\r?\n/).pop() ?? "";
      try {
        const parsed = JSON.parse(last);
        resolve(parsed.ok ? (parsed as RealPredictionResult) : { ok: false, error: String(parsed.error ?? "generation failed") });
      } catch {
        resolve({ ok: false, error: `llm_generate_runner.py returned unparseable output: ${last.slice(0, 200)}` });
      }
    });
    child.stdin.write(JSON.stringify({ modelFilePath, input }));
    child.stdin.end();
  });
}

function runTorchPredictScript(modelFilePath: string, input: unknown): Promise<RealPredictionResult | RealPredictionFailure> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [TORCH_SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ ok: false, error: `pytorch_predict_runner.py timed out after ${TORCH_TIMEOUT_MS}ms` });
    }, TORCH_TIMEOUT_MS);
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: `couldn't start pytorch_predict_runner.py: ${e.message}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return resolve({ ok: false, error: `pytorch_predict_runner.py exited ${code}: ${err.slice(0, 300)}` });
      try {
        const parsed = JSON.parse(out.trim());
        resolve(parsed.ok ? (parsed as RealPredictionResult) : { ok: false, error: String(parsed.error ?? "torch runner reported failure") });
      } catch {
        resolve({ ok: false, error: `pytorch_predict_runner.py returned unparseable output: ${out.slice(0, 200)}` });
      }
    });
    child.stdin.write(JSON.stringify({ modelFilePath, input }));
    child.stdin.end();
  });
}

function runPredictScript(modelFilePath: string, input: unknown): Promise<RealPredictionResult | RealPredictionFailure> {
  const run = async (): Promise<RealPredictionResult | RealPredictionFailure> => {
    let proc: ChildProcessWithoutNullStreams;
    try {
      proc = ensureChild();
    } catch (err) {
      return { ok: false, error: `couldn't start predict_runner.py: ${err instanceof Error ? err.message : String(err)}` };
    }
    return new Promise((resolve) => {
      let settled = false;
      const finish = (r: RealPredictionResult | RealPredictionFailure) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(r);
      };
      const timer = setTimeout(() => {
        // A hung request leaves the pipe out of step with the queue, so the
        // process is killed rather than reused — the next call starts a
        // clean one.
        const idx = waiting.indexOf(onLine);
        if (idx >= 0) waiting.splice(idx, 1);
        proc.kill("SIGKILL");
        if (child === proc) child = null;
        finish({ ok: false, error: `predict_runner.py timed out after ${TIMEOUT_MS}ms` });
      }, TIMEOUT_MS);

      const onLine = (line: string) => {
        try {
          const parsed = JSON.parse(line);
          if (parsed.ok) finish(parsed as RealPredictionResult);
          else finish({ ok: false, error: String(parsed.error ?? "predict_runner.py reported failure") });
        } catch {
          finish({ ok: false, error: `predict_runner.py returned unparseable output: ${line.slice(0, 200)}` });
        }
      };
      waiting.push(onLine);
      proc.stdin.write(JSON.stringify({ modelFilePath, input }) + "\n");
    });
  };

  const queued = chain.then(run, run);
  // The chain must never reject, or every later prediction inherits it.
  chain = queued.catch(() => undefined);
  return queued;
}


// ---- Artifact cache -------------------------------------------------------
// Every prediction used to re-download the model from object storage into a
// fresh temp dir. That download is pure repeated work: an artifact is
// immutable — a new version gets a new key — so the same key always yields
// the same bytes, and the measured p95 it inflated is one of the inputs to
// the model health score shown on the Model Overview page.
//
// Keyed by storage key, so promoting a new version simply misses the cache
// and fetches once. Concurrent first-hits share one in-flight download via
// the promise map rather than racing to write the same file.
const CACHE_DIR = path.join(os.tmpdir(), "ml-studio-artifact-cache");
const inFlight = new Map<string, Promise<string>>();

export async function cachedArtifactPath(key: string, fetch: () => Promise<Buffer>): Promise<string> {
  const existing = inFlight.get(key);
  if (existing) return existing;

  // The key contains slashes; flatten it so the cache stays one directory
  // deep and can't escape CACHE_DIR via a crafted key.
  const fileName = key.replace(/[^a-zA-Z0-9._-]/g, "_");
  const target = path.join(CACHE_DIR, fileName);

  const task = (async () => {
    try {
      await fs.access(target);
      return target;
    } catch {
      // Not cached yet — fetch it.
    }
    const data = await fetch();
    await fs.mkdir(CACHE_DIR, { recursive: true });
    // Write to a unique temp name and rename, so a reader never observes a
    // half-written artifact and joblib never loads a truncated file.
    const staging = `${target}.${process.pid}.${Date.now()}.part`;
    await fs.writeFile(staging, data);
    await fs.rename(staging, target);
    return target;
  })();

  inFlight.set(key, task);
  try {
    return await task;
  } finally {
    inFlight.delete(key);
  }
}

// Loads the real artifact (from cache, downloading once if needed) and runs a real
// prediction against it. Returns null (not a failure) when there's
// nothing real to try — no weights URI recorded for this version, or
// storage isn't configured — so the caller can fall back to the
// deterministic stand-in without treating "nothing to load" as an error.
/** Downloads (once) and returns a local path to a version's real artifact.
 *  Shared with explainability, which needs the same file the predictor loads —
 *  re-fetching it separately would double the storage traffic and could, in
 *  principle, read a different object than the one actually serving. */
export async function localArtifactPath(weightsUri: string | null): Promise<string | null> {
  if (!weightsUri) return null;
  const storage = getStorage();
  if (!storage) return null;
  const key = storageKeyFromUri(weightsUri, storage.bucket);
  return cachedArtifactPath(key, () => storage.getObject(key));
}

export async function tryRealPrediction(weightsUri: string | null, input: unknown, artifactFormat?: string | null): Promise<RealPredictionResult | RealPredictionFailure | null> {
  if (!weightsUri) return null;
  const storage = getStorage();
  if (!storage) return null;

  // weightsUri is a full URI (s3://bucket/key, etc.) — the storage
  // client's getObject wants just the key, matching how putObject was
  // called when this was first uploaded (trained-models/... below the
  // bucket root).
  const key = storageKeyFromUri(weightsUri, storage.bucket);
  let localPath: string;
  try {
    localPath = await cachedArtifactPath(key, () => storage.getObject(key));
  } catch (err) {
    return { ok: false, error: `couldn't download real artifact from storage: ${err instanceof Error ? err.message : String(err)}` };
  }
  // A fine-tuned Whisper adapter transcribes audio rather than generating text.
  if (artifactFormat === "huggingface-peft-whisper") {
    const r = await runSpeech("speech-to-text", "", input, localPath);
    return r.ok ? { ok: true, prediction: r.prediction, shapeNote: null, modelClass: r.modelClass } : r;
  }
  if (isLlmArtifact(weightsUri, artifactFormat)) return await runLlmGenerateScript(localPath, input);
  return isTorchArtifact(weightsUri)
    ? await runTorchPredictScript(localPath, input)
    : await runPredictScript(localPath, input);
}

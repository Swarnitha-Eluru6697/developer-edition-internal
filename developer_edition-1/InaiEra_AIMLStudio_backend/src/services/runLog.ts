import { AsyncLocalStorage } from "async_hooks";
import type { ChildProcess } from "child_process";
import { pool } from "../db/connection";

/**
 * Live, per-run training logs (training_run_logs).
 *
 * A run used to have only the few lines its log page reconstructed from the run row
 * afterwards (queued, claimed, done). Now the worker opens a context for the run it is
 * processing; anything inside it — trainer subprocess output, registration, artifact
 * upload — is written as it happens. Trainers emit `MLLOG {json}` lines on stderr
 * (src/training/runlog.py); other stderr lines are kept when they are warnings or
 * errors, and progress-bar noise is dropped.
 */
const ctx = new AsyncLocalStorage<{ runId: string; seq: number }>();
const MAX_LINES_PER_RUN = 5000;

export function withRunLog<T>(runId: string, fn: () => Promise<T>): Promise<T> {
  return ctx.run({ runId, seq: 0 }, fn);
}

export function logRun(src: string, level: string, msg: string, data?: unknown): void {
  const c = ctx.getStore();
  if (!c || c.seq >= MAX_LINES_PER_RUN) return;
  c.seq += 1;
  pool
    .query("INSERT INTO training_run_logs (run_id, seq, src, level, msg, data) VALUES ($1, $2, $3, $4, $5, $6)",
      [c.runId, c.seq, src, level, msg.slice(0, 4000), data === undefined ? null : JSON.stringify(data)])
    .catch(() => { /* logging must never fail a training run */ });
}

const NOISE = /(\d+%\|)|it\/s\]|s\/it\]|^\s*$|^\s*warnings\.warn\(/;
// Harmless on this platform and repeated every epoch: drop rather than show as warnings.
const BENIGN = /pin_memory' argument is set as true but not supported on MPS|unauthenticated requests to the HF Hub/;
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

// Subprocesses each run has started (trainers, document extraction, file combining),
// so cancelling a run can stop the work itself — not only flag the row.
const runProcesses = new Map<string, Set<ChildProcess>>();

/** Remember `child` as belonging to the current run, until it exits. */
export function trackRunProcess(child: ChildProcess): void {
  const c = ctx.getStore();
  if (!c) return;
  const set = runProcesses.get(c.runId) ?? new Set<ChildProcess>();
  runProcesses.set(c.runId, set);
  set.add(child);
  child.once("exit", () => {
    set.delete(child);
    if (!set.size) runProcesses.delete(c.runId);
  });
}

/** Stop every live subprocess of a run: SIGTERM, then SIGKILL if it's still up after 5 s. */
export function killRunProcesses(runId: string): number {
  const set = runProcesses.get(runId);
  if (!set) return 0;
  for (const child of set) {
    child.kill("SIGTERM");
    setTimeout(() => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); }, 5_000).unref();
  }
  return set.size;
}

/** Stream a trainer subprocess's stderr into the current run's log. */
export function attachRunLog(child: ChildProcess, src: string): void {
  trackRunProcess(child);
  if (!ctx.getStore() || !child.stderr) return;
  let buf = "";
  child.stderr.on("data", (d: Buffer) => {
    buf += d.toString();
    const parts = buf.split(/\r?\n|\r/);
    buf = parts.pop() ?? "";
    for (const raw of parts) {
      const line = raw.replace(ANSI, "");
      // A progress bar redraws with "\r" and no newline, so an MLLOG line can arrive
      // glued to the end of one — look for the marker anywhere, not only at the start.
      const at = line.indexOf("MLLOG {");
      if (at >= 0) {
        try {
          const r = JSON.parse(line.slice(at + 6));
          logRun(src, r.level || "info", `${r.msg}${r.elapsed_s != null ? `  [+${r.elapsed_s}s]` : ""}`, r.data);
        } catch {
          logRun(src, "info", line.slice(at + 6));
        }
      } else if (!NOISE.test(line) && !BENIGN.test(line) && /error|exception|traceback|warn|killed|out of memory/i.test(line)) {
        logRun(src, /error|exception|traceback|killed/i.test(line) ? "error" : "warn", line.trim());
      }
    }
  });
}

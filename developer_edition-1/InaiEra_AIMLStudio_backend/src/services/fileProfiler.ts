import { spawn } from "child_process";
import path from "path";
import { PYTHON_BIN } from "./pythonBin";

const SCRIPT_PATH = path.join(__dirname, "..", "training", "file_profiler.py");

/** Profiles slides, images, audio and archives (training/file_profiler.py):
 *  verifies the file is the declared format and reports what it can measure. */
export function runFileProfiler(filePath: string, format: string): Promise<any> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "", err = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("close", (code) => {
      if (code !== 0) return resolve({ ok: false, error: err || `file profiler exited ${code}` });
      try { resolve(JSON.parse(out.trim())); } catch (e) { resolve({ ok: false, error: `invalid profiler output: ${String(e)}` }); }
    });
    child.on("error", (e) => resolve({ ok: false, error: e.message }));
    child.stdin.end(JSON.stringify({ path: filePath, format }));
  });
}

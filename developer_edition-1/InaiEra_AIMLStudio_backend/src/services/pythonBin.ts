import fs from "fs";
import path from "path";

/**
 * The Python interpreter every trainer/profiler spawns.
 *
 * A bare "python3" resolves through PATH, so it only finds pandas, sklearn,
 * torch… when the server happens to be started from an activated venv. Start
 * it any other way and every dataset upload fails with "pandas not available".
 * Resolve it once instead: PYTHON_BIN wins, then the project's own .venv
 * (LOCAL_STACK.md installs requirements-training.txt there), then PATH.
 */
function resolvePythonBin(): string {
  const fromEnv = process.env.PYTHON_BIN?.trim();
  if (fromEnv) return fromEnv;
  const venvPython = path.resolve(__dirname, "..", "..", ".venv", "bin", "python3");
  if (fs.existsSync(venvPython)) return venvPython;
  return "python3";
}

export const PYTHON_BIN = resolvePythonBin();

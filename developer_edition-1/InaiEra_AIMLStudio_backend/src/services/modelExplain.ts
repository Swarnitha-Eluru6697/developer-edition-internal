import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { Pool } from "pg";
import { localArtifactPath } from "./realPredict";

const SCRIPT_PATH = path.join(__dirname, "..", "training", "explain_runner.py");
const TIMEOUT_MS = 30_000;

export interface Explainability {
  available: boolean;
  reason?: string;
  method?: string;
  grouping?: string;
  modelClass?: string;
  featureCount?: number;
  features?: Array<{ feature: string; importance: number; sharePct: number }>;
  note?: string;
  computedAt?: string;
}

function runExplainScript(modelFilePath: string): Promise<Explainability> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ available: false, reason: `explain_runner.py timed out after ${TIMEOUT_MS}ms` });
    }, TIMEOUT_MS);
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ available: false, reason: `couldn't start explain_runner.py: ${e.message}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        return resolve({ available: false, reason: `explain_runner.py exited ${code}: ${err.slice(0, 300)}` });
      }
      try {
        resolve(JSON.parse(out.trim()) as Explainability);
      } catch {
        resolve({ available: false, reason: `explain_runner.py returned unparseable output: ${out.slice(0, 200)}` });
      }
    });
    child.stdin.write(JSON.stringify({ modelFilePath }));
    child.stdin.end();
  });
}

/**
 * Real global feature attribution for a model's CURRENT deployed version.
 *
 * Replaces a placeholder that said "available once it has served production
 * traffic" — which was never true of global importance. It comes from the
 * fitted model itself and is computable the moment the artifact exists,
 * traffic or not; the placeholder simply meant nothing ever filled it in.
 *
 * Cached in model_monitoring.explainability and recomputed when the cached
 * entry belongs to a different version than the one now deployed, so a
 * promotion doesn't leave the old model's importances on screen.
 */
export async function computeExplainability(pool: Pool, modelId: string, force = false): Promise<Explainability> {
  const dep = await pool.query(
    `SELECT d.current_version, v.weights_uri
       FROM deployments d
       LEFT JOIN model_versions v ON v.model_id = d.model_id AND v.v = d.current_version
      WHERE d.model_id = $1`,
    [modelId]
  );
  if (dep.rows.length === 0) {
    return { available: false, reason: "this model has no deployment, so there is no current version to explain" };
  }
  const { current_version: version, weights_uri: weightsUri } = dep.rows[0];

  const cached = await pool.query("SELECT explainability FROM model_monitoring WHERE model_id = $1", [modelId]);
  const existing = cached.rows[0]?.explainability as (Explainability & { version?: number }) | null;
  if (!force && existing?.available && existing.version === version) return existing;

  if (!weightsUri) {
    return {
      available: false,
      reason: `v${version} has no recorded artifact, so there is no fitted model to read importances from`,
    };
  }

  let localPath: string | null;
  try {
    localPath = await localArtifactPath(weightsUri);
  } catch (err) {
    return { available: false, reason: `couldn't fetch the artifact: ${err instanceof Error ? err.message : String(err)}` };
  }
  if (!localPath) {
    return { available: false, reason: "object storage is not configured, so the artifact can't be read" };
  }

  const result = { ...(await runExplainScript(localPath)), version, computedAt: new Date().toISOString() };
  await pool.query(
    "UPDATE model_monitoring SET explainability = $1::jsonb WHERE model_id = $2",
    [JSON.stringify(result), modelId]
  );
  return result;
}

import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";
import { Pool } from "pg";
import { localArtifactPath } from "./realPredict";

const SCRIPT_PATH = path.join(__dirname, "..", "training", "input_schema_runner.py");
const TIMEOUT_MS = 20_000;

export interface ModelInputSchema {
  available: boolean;
  reason?: string;
  modelId: string;
  version?: number;
  columns?: string[];
  /** A ready-to-send request body built from real values in the training
   *  dataset's own profile — so "Run prediction" works on first click. */
  example?: Record<string, unknown>;
  exampleSource?: string;
}

function runSchemaScript(modelFilePath: string): Promise<{ available: boolean; columns?: string[]; reason?: string }> {
  return new Promise((resolve) => {
    const child = spawn(PYTHON_BIN, [SCRIPT_PATH], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ available: false, reason: `input_schema_runner.py timed out after ${TIMEOUT_MS}ms` });
    }, TIMEOUT_MS);
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => { clearTimeout(timer); resolve({ available: false, reason: e.message }); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return resolve({ available: false, reason: `input_schema_runner.py exited ${code}: ${err.slice(0, 200)}` });
      try { resolve(JSON.parse(out.trim())); }
      catch { resolve({ available: false, reason: `unparseable output: ${out.slice(0, 150)}` }); }
    });
    child.stdin.write(JSON.stringify({ modelFilePath }));
    child.stdin.end();
  });
}

/** One real value for a column, taken from its profile — the most common value
 *  for a categorical, the midpoint of the observed range for a numeric. Never a
 *  made-up constant: a value outside the training distribution would produce a
 *  prediction that looks real and means nothing. */
function exampleValue(col: any): unknown {
  if (!col) return null;
  if (col.kind === "numeric") {
    const mean = col.mean ?? col.median;
    if (typeof mean === "number") return Number(mean.toFixed(4));
    if (typeof col.min === "number" && typeof col.max === "number") return Number(((col.min + col.max) / 2).toFixed(4));
    return 0;
  }
  const top = Array.isArray(col.topValues) ? col.topValues[0] : null;
  return top?.value ?? null;
}

/**
 * The columns a model's deployed version actually requires, plus a usable
 * example body.
 *
 * Exists because the test drawer shipped a single hardcoded fraud-shaped
 * payload for every model. Running it against anything else returned "input is
 * missing required real column(s)" and fell back to the development stand-in,
 * which reads as the model being broken rather than the request being wrong.
 */
export async function getModelInputSchema(pool: Pool, modelId: string): Promise<ModelInputSchema> {
  // The deployed version's contract; for a model not deployed yet, its latest trained
  // version's — the same version test-predict exercises before deployment.
  const dep = await pool.query(
    `SELECT COALESCE(d.current_version, v.v) AS current_version, v.weights_uri, v.artifact_format, v.training_run_id,
            v.dataset_snapshot_version, r.dataset_name, d.model_id IS NOT NULL AS deployed
       FROM model_versions v
       LEFT JOIN deployments d ON d.model_id = v.model_id
       LEFT JOIN training_runs r ON r.id = v.training_run_id
      WHERE v.model_id = $1 AND (d.current_version IS NULL OR v.v = d.current_version)
      ORDER BY v.v DESC LIMIT 1`,
    [modelId]
  );
  if (!dep.rows.length) return { available: false, modelId, reason: "this model has no trained version yet, so there is no input contract" };
  const { current_version: version, weights_uri: weightsUri, dataset_name: datasetName, dataset_snapshot_version: snapshotVersion } = dep.rows[0];

  // A fine-tuned LLM takes a chat message, not columns: offer a real prompt it trained on.
  if (String(dep.rows[0].artifact_format || "").startsWith("huggingface")) {
    const snap = datasetName ? (await pool.query(
      `SELECT version, profile FROM dataset_snapshots WHERE dataset_name = $1 ${snapshotVersion ? "AND version = $2" : ""} ORDER BY version DESC LIMIT 1`,
      snapshotVersion ? [datasetName, snapshotVersion] : [datasetName])).rows[0] : null;
    const sample = snap?.profile?.sampleRows?.[0] ?? {};
    const prompt = typeof sample.prompt === "string" ? sample.prompt : typeof sample.text === "string" ? sample.text.slice(0, 400) : "Hello! What can you help me with?";
    return { available: true, modelId, version, columns: [], example: { messages: [{ role: "user", content: prompt }] },
             exampleSource: snap ? `a real prompt from ${datasetName} v${snap.version} — or just type plain text` : "type plain text, or send {\"messages\": [...]}" };
  }

  if (!weightsUri) {
    return { available: false, modelId, version, reason: `v${version} has no recorded artifact, so its input contract is unknown` };
  }

  let localPath: string | null;
  try { localPath = await localArtifactPath(weightsUri); }
  catch (err) { return { available: false, modelId, version, reason: `couldn't fetch the artifact: ${err instanceof Error ? err.message : String(err)}` }; }
  if (!localPath) return { available: false, modelId, version, reason: "object storage is not configured, so the artifact can't be read" };

  const schema = await runSchemaScript(localPath);
  if (!schema.available || !schema.columns) {
    return { available: false, modelId, version, reason: schema.reason ?? "no named input contract in this artifact" };
  }

  // Example values come from the exact dataset snapshot this version trained
  // on, so they are in-distribution rather than plausible-looking guesses.
  let example: Record<string, unknown> | undefined;
  let exampleSource: string | undefined;
  if (datasetName) {
    const snap = await pool.query(
      `SELECT version, profile FROM dataset_snapshots
        WHERE dataset_name = $1 ${snapshotVersion ? "AND version = $2" : ""}
        ORDER BY version DESC LIMIT 1`,
      snapshotVersion ? [datasetName, snapshotVersion] : [datasetName]
    );
    const cols = snap.rows[0]?.profile?.columns;
    if (Array.isArray(cols)) {
      const byName = new Map(cols.map((c: any) => [c.name, c]));
      example = Object.fromEntries(schema.columns.map((name) => [name, exampleValue(byName.get(name))]));
      exampleSource = `real values from ${datasetName} v${snap.rows[0].version} — most common category / mean numeric`;
    }
  }

  return { available: true, modelId, version, columns: schema.columns, example, exampleSource };
}

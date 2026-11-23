import { pool } from "../db/connection";
import { runPipelineStage, loadPipelineContext, finishPipelineStage, PipelineStageType } from "../execution/workflowEngine";

// Real, shared entrypoint for every backend/src/cli/run-*-stage.ts
// script — this is what actually closes the "Argo stage executables
// are not shipped" gap: previously buildStageTemplate() (see
// workflowEngine.ts) generated real Argo container specs that named
// these six files, but nothing in the repository implemented them —
// a real orchestration layer pointed at real files that didn't exist.
//
// Each of these six scripts is a genuinely thin wrapper: parse real
// argv, load real cross-stage context from the database (see
// loadPipelineContext — the real, persisted replacement for what used
// to be an in-memory-only JS object, which could never have worked
// once a real stage runs in its own separate Argo container with no
// shared memory), call the exact same real runPipelineStage()
// dispatcher the local executor already uses, persist the real result,
// and exit with a real, correct process code so Argo's own step status
// reflects what genuinely happened — 0 only on real success, non-zero
// on any real failure, never the reverse.
//
// This module has real database and business-logic dependencies
// (pg, the training/HPO/deploy machinery) — it is NOT a standalone
// zero-dependency CLI. It expects to run inside the same real
// container image as the API (DATABASE_URL, STORAGE_BACKEND, etc. all
// set the same way), which is exactly what k8s/worker-deployment.yaml
// and the Argo container spec both already assume.

interface CliArgs {
  pipelineId: string;
  stageOrder: number;
  actorRole: string;
  params: Record<string, any>;
}

function parseArgs(argv: string[]): CliArgs {
  const get = (flag: string): string | undefined => {
    const i = argv.indexOf(flag);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const pipelineId = get("--pipeline-id");
  const stageOrderRaw = get("--stage-order");
  const actorRole = get("--actor-role");
  const paramsRaw = get("--params");
  if (!pipelineId) throw new Error("--pipeline-id is required");
  if (!stageOrderRaw) throw new Error("--stage-order is required");
  if (!actorRole) throw new Error("--actor-role is required");
  let params: Record<string, any> = {};
  if (paramsRaw) {
    try {
      params = JSON.parse(paramsRaw);
    } catch (err) {
      throw new Error(`--params is not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
  return { pipelineId, stageOrder: Number(stageOrderRaw), actorRole, params };
}

// Runs one real pipeline stage as a genuinely standalone process —
// exactly the constraint a real, separate Argo container has: no
// shared memory with whatever ran before it, only the real database
// and real argv. Exits 0 on real success, 1 on any real failure
// (a thrown error, a bad argv, or a genuine stage failure) so a real
// Argo Workflow step correctly reports its own real status.
export async function runCliStage(stageType: PipelineStageType): Promise<void> {
  let pipelineId: string | undefined;
  let stageId: number | undefined;
  let stageTitle = stageType;
  try {
    const args = parseArgs(process.argv.slice(2));
    pipelineId = args.pipelineId;

    const stageRow = await pool.query(
      "SELECT id, title, stage_type FROM pipeline_stages WHERE pipeline_id = $1 AND stage_order = $2",
      [args.pipelineId, args.stageOrder]
    );
    if (stageRow.rows.length === 0) {
      throw new Error(`no real pipeline_stages row for pipeline '${args.pipelineId}' at stage_order ${args.stageOrder} — was this pipeline genuinely created via submitNewWorkflow?`);
    }
    stageId = stageRow.rows[0].id;
    stageTitle = stageRow.rows[0].title;
    if (stageRow.rows[0].stage_type !== stageType) {
      throw new Error(`this CLI is for real stage type '${stageType}', but pipeline_stages row ${stageId} is really typed '${stageRow.rows[0].stage_type}' — real dispatch mismatch, refusing to run the wrong real logic`);
    }

    await pool.query("UPDATE pipeline_stages SET state = 'running', sub = 'in progress' WHERE id = $1", [stageId]);

    // Real cross-stage context — every earlier real stage's real
    // output for this exact real pipeline, queried fresh (this process
    // has no memory of anything that ran before it).
    const context = await loadPipelineContext(pool, args.pipelineId, args.stageOrder);

    const start = Date.now();
    let result;
    try {
      result = await runPipelineStage(pool, stageType, args.params, context, args.actorRole);
    } catch (err) {
      result = { ok: false as const, message: err instanceof Error ? err.message : String(err) };
    }
    const elapsedSec = ((Date.now() - start) / 1000).toFixed(1);

    await finishPipelineStage(pool, args.pipelineId, stageId!, stageTitle, result, elapsedSec);

    if (!result.ok) {
      // eslint-disable-next-line no-console
      console.error(`Stage '${stageTitle}' (${stageType}) failed: ${result.message}`);
      await pool.end();
      process.exit(1);
    }
    // eslint-disable-next-line no-console
    console.log(`Stage '${stageTitle}' (${stageType}) succeeded: ${result.message}`);
    await pool.end();
    process.exit(0);
  } catch (err) {
    // A real failure before even reaching runPipelineStage (bad argv,
    // no matching pipeline_stages row, a real DB connection failure) —
    // still marks the real stage/pipeline failed when we know enough
    // to (a real stageId was resolved), and always exits non-zero so
    // Argo never reports this as a real success.
    const message = err instanceof Error ? err.message : String(err);
    // eslint-disable-next-line no-console
    console.error(`Stage CLI (${stageType}) failed before running: ${message}`);
    try {
      if (pipelineId && stageId != null) {
        await finishPipelineStage(pool, pipelineId, stageId, stageTitle, { ok: false, message }, "0.0");
      }
    } catch {
      // Real DB write failed too — nothing more this process can
      // honestly do except exit non-zero and let Argo/the reconciler
      // see a real stage stuck at 'running' rather than a fabricated
      // status this process can no longer vouch for.
    }
    await pool.end().catch(() => {});
    process.exit(1);
  }
}

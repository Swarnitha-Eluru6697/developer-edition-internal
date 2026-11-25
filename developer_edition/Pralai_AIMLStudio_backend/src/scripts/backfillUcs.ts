/**
 * Registers every local champion version into UCS.
 *
 *   npx ts-node src/scripts/backfillUcs.ts            # report only
 *   npx ts-node src/scripts/backfillUcs.ts --apply    # actually register
 *
 * Why this exists: promotion pushes to UCS at the moment you promote, and the
 * push is deliberately non-fatal — a promotion that already committed locally
 * must not be undone because an external catalog was unreachable. The cost is
 * drift: promote against a UCS that is down and the model is live in ML Studio
 * and absent from the catalog, with nothing to reconcile the two afterwards.
 * That is exactly what happened here — eight champions promoted before the UCS
 * connection worked were never registered, and the gap was only visible by
 * comparing the two catalogs by hand.
 *
 * Idempotent: Gravitino answers 409 for both a model and a version that are
 * already present, and both are reported as "already in catalog" rather than
 * as failures. A re-run against a fully-synced catalog therefore registers
 * nothing and fails nothing. Safe to re-run any time drift is suspected.
 *
 * Deliberately NOT automatic on startup. Backfilling writes to a shared
 * catalog, and doing that silently whenever the server boots would make a
 * misconfigured local instance quietly publish into someone else's UCS.
 */
import "dotenv/config";
import { pool } from "../db/connection";
import { ucsConfig } from "../ucs/config";
import { registerModel, registerModelVersion, ingestLineageEvent, ucsHealthy, UcsError } from "../ucs/client";
import { modelFqn } from "../ucs/config";

interface Champion {
  model_id: string;
  v: number;
  weights_uri: string | null;
  deployed_version: number | null;
  training_run_id: string | null;
  dataset_name: string | null;
  dataset_snapshot_version: number | null;
}

async function main(): Promise<void> {
  const apply = process.argv.includes("--apply");
  // --all: every trained version, not only champions — the catalog should know a
  // model exists from the moment it is trained, which is what registration at
  // training completion does when UCS is reachable.
  const all = process.argv.includes("--all");

  if (!(await ucsHealthy())) {
    console.error(`UCS at ${ucsConfig.baseUrl} is not reachable — nothing to backfill into. Start it first.`);
    process.exitCode = 1;
    return;
  }

  const { rows } = await pool.query<Champion>(
    `SELECT v.model_id, v.v, v.alias, v.weights_uri, d.current_version AS deployed_version,
            v.training_run_id, r.dataset_name, v.dataset_snapshot_version
       FROM model_versions v
       LEFT JOIN deployments d ON d.model_id = v.model_id
       LEFT JOIN training_runs r ON r.id = v.training_run_id
      WHERE ($1::boolean OR v.alias = 'champion')
      ORDER BY v.model_id, v.v`,
    [all]
  );

  console.log(`UCS ${ucsConfig.baseUrl} — catalog '${ucsConfig.modelCatalog}', schema '${ucsConfig.modelSchema}'`);
  console.log(`${rows.length} local ${all ? "" : "champion "}version(s)${apply ? "" : " — dry run, pass --apply to register"}\n`);

  let registered = 0;
  let already = 0;
  let lineage = 0;
  let failed = 0;

  for (const row of rows) {
    const label = `${row.model_id} v${row.v}`;
    if (!apply) {
      console.log(`  would register  ${label}`);
      continue;
    }
    try {
      try {
        await registerModel(row.model_id, `${row.deployed_version != null ? "Deployed" : "Trained"} model: ${row.model_id}`, {});
      } catch (err) {
        // 409 = the model is already in the catalog. That is the normal case on
        // a re-run and for anything promoted while UCS was reachable; the
        // version link below is still what needs doing, so don't abort here.
        if (!(err instanceof UcsError && err.status === 409)) throw err;
      }
      try {
        await registerModelVersion(
          row.model_id,
          // Matches the URI shape promote writes, so a backfilled entry and a
          // freshly-promoted one are indistinguishable in the catalog.
          `model://${row.model_id}/v${row.v}`,
          // An alias is unique per model in Gravitino; versions without one get none.
          (row as any).alias ? [(row as any).alias] : [],
          `Backfilled from ML Studio (local alias: ${(row as any).alias || "none"})`,
          { version: row.v, backfilled: true }
        );
        console.log(`  ✓ ${label}`);
        registered++;
      } catch (err) {
        // This exact version URI is already linked — nothing to add, and not a
        // failure. Reported separately so a re-run reads as "already synced"
        // rather than as ten errors.
        if (err instanceof UcsError && err.status === 409) {
          console.log(`  · ${label} — already in catalog`);
          already++;
        } else {
          throw err;
        }
      }

      // Lineage is emitted at registration too, but the same outage that lost
      // the catalog entries lost these edges, and the Lineage tab reads UCS
      // directly — without them it reports "not registered in UCS's lineage
      // graph" for a model that plainly came from a real dataset. Replays the
      // same dataset -> model edge registration would have written.
      // Best-effort: a missing edge is worse than a missing model, not fatal.
      if (row.training_run_id && row.dataset_name) {
        try {
          const dataset = row.dataset_snapshot_version
            ? `${modelFqn(row.dataset_name)}@v${row.dataset_snapshot_version}`
            : modelFqn(row.dataset_name);
          await ingestLineageEvent({
            eventType: "COMPLETE",
            run: { runId: row.training_run_id },
            job: { namespace: "ml-studio", name: `train-${row.model_id}` },
            inputs: [{ namespace: "ml-studio-datasets", name: dataset }],
            outputs: [{ namespace: "ml-studio-models", name: modelFqn(row.model_id) }],
          });
          lineage++;
        } catch (err) {
          console.error(`    (lineage edge failed for ${label}: ${err instanceof Error ? err.message : String(err)})`);
        }
      }
    } catch (err) {
      const message = err instanceof UcsError ? err.message : err instanceof Error ? err.message : String(err);
      console.error(`  ✗ ${label} — ${message}`);
      failed++;
    }
  }

  if (apply) {
    console.log(`\nregistered ${registered}, already present ${already}, lineage edges ${lineage}, failed ${failed}`);
    if (failed > 0) process.exitCode = 1;
  }

  await pool.end();
}

main().catch((err) => {
  console.error("Backfill failed:", err instanceof Error ? err.message : err);
  process.exitCode = 1;
});

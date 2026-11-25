import { initSchema, pool } from "./connection";
import { loadReferenceData } from "./referenceData";
import { loadSampleDatasetsAtStartup } from "./sampleDatasets";

// `npm run seed:reference` (or `node dist/db/seedReference.js` in the image) —
// the same non-destructive load the server runs at startup, on demand: upserts
// the reference catalog, inserts missing compute pools, and loads any sample
// dataset not loaded before (unless SEED_SAMPLE_DATASETS=false). Deletes
// nothing, so it is safe against a real deployment — unlike `npm run seed`.
// Unlike startup it exits non-zero if the catalog load fails, since running it
// is an explicit request for that load.
async function main() {
  await initSchema();
  const summary = await loadReferenceData(pool);
  // eslint-disable-next-line no-console
  console.log("Reference data loaded:", JSON.stringify(summary));
  await loadSampleDatasetsAtStartup();
  await pool.end();
}

main().catch(async (err) => {
  // eslint-disable-next-line no-console
  console.error("Reference data load failed:", err);
  await pool.end().catch(() => {});
  process.exit(1);
});

/**
 * One-time (idempotent) provisioning of the real UCS state ML Studio's
 * integration depends on: a "model" catalog, one RBAC role per ML Studio
 * access tier (on both the trained-model schema and the base-library
 * schema), a group binding per ML Studio role, and — new — all 85 base
 * models registered as real Gravitino model entities under
 * ml_models.base-library.*, distinct from where trained models register
 * (ml_models.default.*) so the two namespaces never collide.
 *
 * Run this against a real UCS deployment (with Gravitino actually
 * reachable) before ML Studio's authz/check calls will return anything
 * but "no matching grant" — those checks only see bindings that already
 * exist; nothing creates them automatically on first use. The base-model
 * registration below is in the same boat: every one of these calls is
 * admin-gated AND depends on Gravitino, so unlike authz/check and
 * lineage (verified locally — see CHANGELOG.md), NONE of this script,
 * including the read side a Train Wizard synced from UCS would use
 * afterward, has ever been exercised against a real Gravitino. This is a
 * larger unverified surface than anything else in this project — stated
 * plainly, not glossed over.
 *
 * Requires UCS_GATEWAY_SECRET; calls are made as UCS_SERVICE_USERNAME with
 * the permissions ucs:catalogs:manage,ucs:rbac:manage (see ucs/config.ts).
 *
 * Usage: npx ts-node src/scripts/bootstrapUcs.ts
 */
import "dotenv/config";
import { registerCatalog, createSchema, createRole, bindGroupToRole, registerModel, registerModelVersion, UcsError } from "../ucs/client";
import { ucsConfig } from "../ucs/config";
import { pool } from "../db/connection";

const TIERS: { role: string; group: string; access_level: "select" | "writer" | "owner" }[] = [
  { role: "ml-studio-viewer", group: "ml-studio-viewers", access_level: "select" },
  { role: "ml-studio-writer", group: "ml-studio-engineers", access_level: "writer" },
  { role: "ml-studio-owner", group: "ml-studio-admins", access_level: "owner" },
];

/** Role and binding steps that failed, so the summary reports them instead of claiming
 *  the grants are in place. */
const skipped: string[] = [];

/** Role provisioning must not block model registration: a role or binding UCS refuses is
 *  reported, recorded in `skipped`, and the run carries on. */
async function warnOnRoleError<T>(fn: () => Promise<T>, what: string): Promise<boolean> {
  try {
    await ignoreAlreadyExists(fn, what);
    return true;
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    // eslint-disable-next-line no-console
    console.warn(`  ! ${what} — skipped: ${reason}`);
    skipped.push(`${what}: ${reason}`);
    return false;
  }
}

async function ignoreAlreadyExists<T>(fn: () => Promise<T>, what: string): Promise<void> {
  try {
    await fn();
    // eslint-disable-next-line no-console
    console.log(`  ✓ ${what}`);
  } catch (err) {
    if (err instanceof UcsError && err.status === 409) {
      // eslint-disable-next-line no-console
      console.log(`  = ${what} (already exists)`);
      return;
    }
    throw err;
  }
}

async function registerBaseModels(): Promise<{ registered: number; failed: number }> {
  const rows = (await pool.query("SELECT * FROM base_models ORDER BY id")).rows;
  // eslint-disable-next-line no-console
  console.log(`\nRegistering ${rows.length} base models into ${ucsConfig.modelCatalog}.${ucsConfig.baseModelSchema}.*`);

  let registered = 0;
  let failed = 0;
  for (const row of rows) {
    try {
      // Every rich field the Train Wizard's UI needs goes into
      // properties — the plan for a synced-from-UCS Train Wizard reads
      // these back from the object profile's raw properties, so nothing
      // about how a base model renders depends on anything beyond what
      // UCS itself holds.
      await ignoreAlreadyExists(
        () =>
          registerModel(row.id, row.description || "", {
            domain: row.domain,
            title: row.title,
            task: row.task,
            description: row.description,
            framework: row.framework,
            license: row.license,
            sourceUri: row.uri,
            gpu: row.gpu,
            computeProfile: row.compute_profile,
            longDescription: row.long_description,
          }, ucsConfig.baseModelSchema),
        `model '${row.id}'`
      );
      await ignoreAlreadyExists(
        () => registerModelVersion(row.id, row.uri, ["base-library"], `Base model library entry — ${row.framework}`, { domain: row.domain, task: row.task }, ucsConfig.baseModelSchema),
        `  version of '${row.id}'`
      );
      registered++;
    } catch (err) {
      failed++;
      // eslint-disable-next-line no-console
      console.error(`  FAILED '${row.id}': ${err instanceof UcsError ? `${err.message} (HTTP ${err.status})` : err}`);
    }
  }
  return { registered, failed };
}

async function main() {
  // eslint-disable-next-line no-console
  console.log(`Bootstrapping UCS at ${ucsConfig.baseUrl} — catalog '${ucsConfig.modelCatalog}'`);

  await ignoreAlreadyExists(
    () => registerCatalog({ name: ucsConfig.modelCatalog, engine: "model", comment: "ML Studio's model registry — base library + trained models" }),
    `catalog '${ucsConfig.modelCatalog}' (engine=model)`
  );

  // The schemas trained and base models register under. Nothing created them, so on a
  // fresh UCS every registration into ml_models.default failed with "schema not found".
  for (const [schema, comment] of [[ucsConfig.modelSchema, "Models trained in ML Studio"],
                                   [ucsConfig.baseModelSchema, "ML Studio's base-model library"]]) {
    await ignoreAlreadyExists(() => createSchema(ucsConfig.modelCatalog, schema, comment),
      `schema '${ucsConfig.modelCatalog}.${schema}'`);
  }

  for (const t of TIERS) {
    for (const schema of [ucsConfig.modelSchema, ucsConfig.baseModelSchema]) {
      const roleName = `${t.role}-${schema}`;
      // A grant on every model in a schema is a SCHEMA securable in Gravitino
      // (ml_models.default), not a MODEL one: a two-part name must be SCHEMA. UCS expands
      // the tier at that level for the model engine — USE_SCHEMA + USE_MODEL, plus
      // CREATE_MODEL_VERSION (writer) and CREATE_MODEL (owner).
      const created = await warnOnRoleError(
        () =>
          createRole({
            role_name: roleName,
            catalog: ucsConfig.modelCatalog,
            schema_name: schema,
            object_name: "*",
            object_type: "schema",
            access_level: t.access_level,
          }),
        `role '${roleName}' (${t.access_level} on schema ${ucsConfig.modelCatalog}.${schema})`
      );
      const what = `bind '${t.group}' -> '${roleName}'`;
      if (created) await warnOnRoleError(() => bindGroupToRole(t.group, roleName, "custom"), what);
      else {
        // eslint-disable-next-line no-console
        console.warn(`  ! ${what} — skipped: the role was not created`);
        skipped.push(`${what}: the role was not created`);
      }
    }
  }

  const { registered, failed } = await registerBaseModels();

  /* eslint-disable no-console */
  console.log(`\nDone. ${registered} base model(s) registered${failed ? `, ${failed} failed` : ""}.`);
  if (skipped.length) {
    console.warn(`${skipped.length} role step(s) were skipped, so those grants are NOT in place:`);
    for (const s of skipped) console.warn(`  - ${s}`);
    process.exitCode = 1;
  } else {
    console.log(`The UCS groups ${TIERS.map((t) => t.group).join(", ")} hold their grants on both schemas (add users to these groups in Keycloak to give them catalog access to models).`);
  }
  if (failed) process.exitCode = 1;
  /* eslint-enable no-console */
}

main()
  .catch((err) => {
    // eslint-disable-next-line no-console
    console.error("Bootstrap failed:", err instanceof UcsError ? `${err.message} (HTTP ${err.status})` : err);
    process.exit(1);
  })
  .finally(() => pool.end());

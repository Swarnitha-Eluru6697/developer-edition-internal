// Pulls base models FROM UCS into the local base_models table — the read
// side of the round-trip bootstrapUcs.ts's write side sets up. UCS
// becomes the real source of truth; the local table becomes a read
// cache populated from it, not the other way around.
//
// UNVERIFIED, more so than most of this project's UCS work: both
// scripts/bootstrapUcs.ts (the write side, registering base models) and
// this file's reads (GET /user/explorer-tree + /user/objects/{fqn}/profile)
// depend on Gravitino, which isn't available anywhere this project has
// been able to run things. Unlike authz/check and lineage — live-tested
// against a real running UCS instance — NEITHER side of this specific
// round-trip has ever been exercised end to end. Real, documented HTTP
// calls against the real, verified API shapes; genuinely untested.
//
// Real fail-soft, the same standard as everywhere else real training/
// storage can fail in this app: if UCS is unreachable, or the
// base-library schema comes back empty, or any individual model's
// profile fetch fails, this leaves the existing local table exactly as
// it was — never wipes or partially corrupts it because of a sync that
// didn't fully succeed.

import { pool } from "../db/connection";
import { getExplorerTree, getObjectProfile, UcsError } from "../ucs/client";
import type { UcsIdentity } from "../ucs/config";
import { ucsConfig } from "../ucs/config";

export interface BaseModelSyncResult {
  attempted: boolean;
  synced: number;
  failed: number;
  reason?: string;
}

interface ExplorerTreeSchemaEntry {
  name: string;
  type: string;
}
interface ExplorerTreeCatalog {
  engine: string;
  views_supported: boolean;
  schemas: Record<string, ExplorerTreeSchemaEntry[]>;
}

export async function syncBaseModelsFromUcs(identity: UcsIdentity): Promise<BaseModelSyncResult> {
  let tree: { tree: Record<string, ExplorerTreeCatalog> };
  try {
    tree = (await getExplorerTree(identity)) as { tree: Record<string, ExplorerTreeCatalog> };
  } catch (err) {
    return { attempted: false, synced: 0, failed: 0, reason: `UCS explorer-tree unreachable: ${err instanceof UcsError ? err.message : String(err)}` };
  }

  const catalog = tree.tree?.[ucsConfig.modelCatalog];
  const entries = catalog?.schemas?.[ucsConfig.baseModelSchema];
  if (!entries || entries.length === 0) {
    return {
      attempted: true,
      synced: 0,
      failed: 0,
      reason: `no models found under ${ucsConfig.modelCatalog}.${ucsConfig.baseModelSchema} — has scripts/bootstrapUcs.ts been run against this UCS deployment?`,
    };
  }

  let synced = 0;
  let failed = 0;
  for (const entry of entries) {
    const fqn = `${ucsConfig.modelCatalog}.${ucsConfig.baseModelSchema}.${entry.name}`;
    try {
      const profile = await getObjectProfile(identity, fqn);
      const props = ((profile.schema as Record<string, unknown> | undefined)?.properties || {}) as Record<string, unknown>;
      if (!props.domain || !props.task || !props.framework) {
        throw new Error("profile is missing required fields (domain/task/framework) — was this registered by bootstrapUcs.ts?");
      }
      // UCS is authoritative for the descriptive fields only. The training
      // metadata (category, algorithm_key, default_hp, supported_hp, …) is
      // local to ML Studio and has no counterpart in a UCS profile, so the
      // ON CONFLICT clause below deliberately leaves those columns alone — a
      // sync must not blank out an entry's hyperparameter surface.
      await pool.query(
        `INSERT INTO base_models (id, domain, title, task, description, framework, license, uri, gpu, compute_profile, long_description)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
         ON CONFLICT (id) DO UPDATE SET
           domain = EXCLUDED.domain, title = EXCLUDED.title, task = EXCLUDED.task, description = EXCLUDED.description,
           framework = EXCLUDED.framework, license = EXCLUDED.license, uri = EXCLUDED.uri, gpu = EXCLUDED.gpu,
           compute_profile = EXCLUDED.compute_profile, long_description = EXCLUDED.long_description`,
        [
          entry.name,
          props.domain,
          props.title || entry.name,
          props.task,
          props.description || "",
          props.framework,
          props.license || "",
          props.sourceUri || "",
          !!props.gpu,
          props.computeProfile || "",
          props.longDescription || "",
        ]
      );
      synced++;
    } catch (err) {
      failed++;
      // eslint-disable-next-line no-console
      console.error(`Base model sync: '${entry.name}' failed — ${err instanceof UcsError ? err.message : String(err)}`);
    }
  }
  return { attempted: true, synced, failed };
}

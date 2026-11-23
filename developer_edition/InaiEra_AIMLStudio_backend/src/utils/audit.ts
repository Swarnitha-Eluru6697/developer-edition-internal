import { pool } from "../db/connection";

// One shared append-only log for every RBAC-gated mutation across the app —
// called from wherever a mutation already knows "who did what to what",
// rather than trying to reconstruct it later from scattered tables.
export async function logAudit(actor: string, action: string, target: string) {
  await pool.query("INSERT INTO audit_log (actor, action, target) VALUES ($1, $2, $3)", [actor, action, target]);
}

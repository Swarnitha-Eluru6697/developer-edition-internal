/*
 * Kafka Connect worker pools, as the UI shows them (backend: app/core/connect_pools.py).
 *
 * Connectors run in separate Connect worker pools by workload — a pipeline's pool is set by its connector and never
 * changes. The backend sends a name and workload for the pools list (Monitoring); the pipeline record carries only the id,
 * so this is the one place an id becomes a label. An unknown id (a newer backend) is shown as-is, never hidden.
 */

const POOL_LABELS: Record<string, string> = {
  "connect-cdc-pool": "CDC",
  "connect-jdbc-pool": "JDBC",
  "connect-file-pool": "File",
  "connect-api-pool": "API",
  "connect-stream-pool": "Stream",
  "connect-sink-pool": "Sink",
  // retired name of the stream pool, in case an old record is still around
  "connect-queue-pool": "Stream",
};

export function poolLabel(poolId: string | null | undefined): string {
  if (!poolId) return "—";
  return POOL_LABELS[poolId] ?? poolId;
}

/** Pill + dot styling for a pool's status: only `healthy` is green; `idle` (nothing of this org's runs there) is neutral. */
export function poolStatusStyle(status: string): { pill: string; dot: string } {
  if (status === "healthy") return { pill: "bg", dot: "dg" };
  if (status === "idle") return { pill: "bgr", dot: "dgr" };
  return { pill: "ba", dot: "da" };
}

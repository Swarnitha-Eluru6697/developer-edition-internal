/**
 * Status pill. The stylesheet defines four states — queued / running / success
 * / failed — so anything else (a status a route adds later) falls back to the
 * neutral queued styling rather than rendering an unstyled pill.
 */
const CLASS_FOR: Record<string, string> = {
  queued: "status-queued",
  pending: "status-queued",
  provisioning: "status-running",
  running: "status-running",
  active: "status-success",
  healthy: "status-success",
  succeeded: "status-success",
  success: "status-success",
  passed: "status-success",
  // The model-health vocabulary (services/modelHealth.ts) is healthy / watch /
  // degraded / critical / insufficient-data — map its two extra states rather
  // than letting them fall through to the neutral grey, which would make a
  // critical model look merely unscored.
  watch: "status-running",
  degraded: "status-running",
  critical: "status-failed",
  stale: "status-failed",
  failed: "status-failed",
  error: "status-failed",
  cancelled: "status-queued",
};

export function StatusPill({ status, label, compact }: { status: string; label?: string; compact?: boolean }) {
  const cls = CLASS_FOR[status.toLowerCase()] ?? "status-queued";
  return (
    <span className={`status-pill ${cls}`} style={compact ? { fontSize: 10, padding: "2px 7px" } : undefined}>
      <span className="status-dot" />
      {label ?? status}
    </span>
  );
}

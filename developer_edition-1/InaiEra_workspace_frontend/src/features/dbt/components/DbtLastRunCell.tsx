import type { DbtLastRun } from "../../../types";
import { timeAgo } from "../../../shared/format";
import { failedModelsText, plural, type RunUnit } from "../runCounts";
import { STATUS_BG, STATUS_COLOR } from "../styles";

/** The "Last run" cell of the project list.
 *
 *  Outcome first, then age: scanning the list you are looking for the project
 *  that broke, not for a date. A failed run also states how many models failed,
 *  because "failed" alone doesn't distinguish one bad model from a project that
 *  never compiled — and the run's error is put in the tooltip so the common
 *  question can be answered without opening the project.
 *
 *  A project with no runs says so explicitly. An empty cell here would read as
 *  data that failed to load rather than a project nobody has built yet.
 */
export function DbtLastRunCell({ run, unit = "model" }: { run: DbtLastRun | null; unit?: RunUnit }) {
  if (!run) {
    return <div style={{ fontSize: 12, color: "#c7c7c2" }}>never run</div>;
  }
  const when = run.started_at ?? run.completed_at;
  const failed = (run.models_failed ?? 0) > 0;
  const detail = failedModelsText(run, unit) ?? (run.models_total != null ? plural(run.models_total, unit) : null);
  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}
      title={run.error_message ?? (when ? new Date(when).toLocaleString() : undefined)}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{
          background: STATUS_BG[run.status] ?? "#f3f4f6",
          color: STATUS_COLOR[run.status] ?? "#6b7280",
          padding: "1px 7px", borderRadius: 4, fontSize: 11, fontWeight: 600,
        }}>{run.status}</span>
        {when && <span style={{ fontSize: 11.5, color: "#9ca3af" }}>{timeAgo(when)}</span>}
      </div>
      {detail && (
        <div style={{
          fontSize: 10.5, color: failed ? "#dc2626" : "#9ca3af",
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>{detail}</div>
      )}
    </div>
  );
}

import type { DbtRunEvent } from "../../types";

/** What the run-events list shows for one event: a node_finish event's
 *  outcome, and its execution time when the runner reported one. A Python or SQL job step also carries what it printed
 *  (`output`) and, when it failed, why (`message`); a dbt node has no `output`, so nothing extra is shown for it. */
export function nodeEventSummary(e: Pick<DbtRunEvent, "payload">): { ok: boolean; seconds: number | null; message: string | null; output: string | null } {
  const { status, execution_time, message, output } = e.payload as { status?: unknown; execution_time?: unknown; message?: unknown; output?: unknown };
  const text = typeof output === "string" && output.trim() ? output : null;
  return {
    message: typeof message === "string" && message && (status !== "success" || text) ? message : null,
    output: text,
    ok: status === "success",
    // Truthy check kept from the original rendering: a 0s timing isn't shown.
    seconds: typeof execution_time === "number" && execution_time ? execution_time : null,
  };
}

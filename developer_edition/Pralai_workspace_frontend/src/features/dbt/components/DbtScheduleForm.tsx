/** The project's Schedule tab: cron expression + enabled toggle. */
import { useState } from "react";
import type { DbtProject } from "../../../types";

export function DbtScheduleForm({ project, onSave }: { project: DbtProject; onSave: (cron: string, enabled: boolean) => void }) {
  const [cron, setCron] = useState(project.schedule_cron ?? "0 */6 * * *");
  const [enabled, setEnabled] = useState(project.schedule_enabled);
  return (
    <div style={{ maxWidth: 420 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 14 }}>{project.name} — Schedule</div>
      <div style={{ marginBottom: 12 }}>
        <label style={{ fontSize: 12, fontWeight: 600, display: "block", marginBottom: 5 }}>Cron expression</label>
        <input value={cron} onChange={(e) => setCron(e.target.value)}
          style={{ width: "100%", height: 32, fontSize: 13, fontFamily: "monospace", border: "0.5px solid var(--ws-line-strong, #cfd8d1)", borderRadius: 6, padding: "0 9px" }} />
        <div style={{ fontSize: 11, color: "var(--ws-subtle, #73827a)", marginTop: 4 }}>
          Examples: <code>0 */6 * * *</code> (every 6h) · <code>0 1 * * *</code> (daily 1am) · <code>30 8 * * 1-5</code> (weekdays 8:30am)
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14 }}>
        <input type="checkbox" id="sched-enabled" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        <label htmlFor="sched-enabled" style={{ fontSize: 13, cursor: "pointer" }}>Schedule enabled</label>
      </div>
      <button onClick={() => onSave(cron, enabled)}
        style={{ height: 32, padding: "0 18px", fontSize: 13, background: "var(--ws-primary, #0A7D5A)", color: "var(--ws-on-primary, #fff)", border: "none", borderRadius: 6, cursor: "pointer" }}>
        Save schedule
      </button>
    </div>
  );
}

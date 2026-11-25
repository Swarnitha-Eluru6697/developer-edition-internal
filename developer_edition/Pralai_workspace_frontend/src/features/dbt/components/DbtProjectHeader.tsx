/** Header of a drilled-in project (back arrow, name, engine, Run now) plus its tabs. */
import type { DbtProject } from "../../../types";
import { ROW_PAD, primaryBtn } from "../styles";

export type DbtTab = "runs" | "lineage" | "schedule" | "dependents";
const DBT_TABS: DbtTab[] = ["runs", "lineage", "schedule", "dependents"];
const SQL_TABS: DbtTab[] = ["runs", "schedule", "dependents"];          // a SQL job reports no lineage

interface Props {
  project: DbtProject;
  tab: DbtTab;
  onTab: (t: DbtTab) => void;
  onBack: () => void;
  /** Omitted without the job kind's run permission (e.g. workspace:dbt:run) — no Run now button. */
  onRun?: () => void;
  /** A SQL job has no Lineage tab. */
  hasLineage?: boolean;
}

export function DbtProjectHeader({ project, tab, onTab, onBack, onRun, hasLineage = true }: Props) {
  const tabs = hasLineage ? DBT_TABS : SQL_TABS;
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: ROW_PAD, borderBottom: "0.5px solid #E3E8E2", flexShrink: 0 }}>
        <button
          onClick={onBack}
          title="Back to all projects"
          style={{ background: "none", border: "0.5px solid #cfd8d1", borderRadius: 6, cursor: "pointer", color: "#5b6d62", height: 26, width: 26, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}
        ><i className="ti ti-arrow-left" style={{ fontSize: 14 }} /></button>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{project.name}</div>
          <div style={{ fontSize: 11, color: "#73827a", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {project.git_url}@{project.git_ref}
          </div>
        </div>
        <span style={{ background: "#E3F6EA", color: "#1e40af", padding: "2px 8px", borderRadius: 4, fontSize: 11.5, fontWeight: 500, flexShrink: 0 }}>{project.engine}</span>
        {onRun && (
          <button onClick={onRun} style={{ ...primaryBtn, marginLeft: "auto", flexShrink: 0 }}>
            <i className="ti ti-player-play" style={{ fontSize: 12 }} />Run now
          </button>
        )}
      </div>

      <div style={{ display: "flex", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9", flexShrink: 0 }}>
        {tabs.map((t) => (
          <div
            key={t}
            onClick={() => onTab(t)}
            style={{
              padding: ROW_PAD, fontSize: 12.5, cursor: "pointer", textTransform: "capitalize",
              color: tab === t ? "#14201B" : "#5b6d62",
              borderBottom: tab === t ? "2px solid #0A7D5A" : "2px solid transparent",
              fontWeight: tab === t ? 500 : 400,
            }}
          >{t}</div>
        ))}
      </div>
    </>
  );
}

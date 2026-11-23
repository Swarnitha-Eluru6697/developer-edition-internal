/** Every registered dbt project (or SQL job); clicking a row drills into it. */
import type { DbtProject } from "../../../types";
import type { JobKind } from "../jobKinds";
import { JOB_KINDS } from "../jobKinds";
import { EMPTY_PAD, HEAD_STYLE, PROJECT_COLS, ROW_PAD, gridRow } from "../styles";
import { DbtLastRunCell } from "./DbtLastRunCell";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

interface Props {
  projects: DbtProject[] | undefined;
  loading: boolean;
  onOpen: (p: DbtProject) => void;
  /** Omitted = no delete action (the caller may not edit this kind of job). */
  onDelete?: (p: DbtProject) => void;
  kind?: JobKind;
}

export function DbtProjectList({ projects, loading, onOpen, onDelete, kind = "dbt" }: Props) {
  const k = JOB_KINDS[kind];
  return (
    <div>
      {loading && (
        <div style={{ padding: EMPTY_PAD, textAlign: "center", color: "#9ca3af", fontSize: 13 }}>Loading projects…</div>
      )}
      {!loading && !projects?.length && (
        <div style={{ padding: EMPTY_PAD, textAlign: "center" }}>
          <i className={`ti ${k.icon}`} style={{ fontSize: 32, color: "#d1d5db", display: "block", marginBottom: 10 }} />
          <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 5 }}>{k.emptyTitle}</div>
          {/* Registering lives only in the page header's top-right button. */}
          <div style={{ fontSize: 12.5, color: "#6b7280" }}>{k.emptyHint}</div>
        </div>
      )}
      {projects && projects.length > 0 && (
        <>
          <div style={{ ...HEAD_STYLE, ...gridRow(PROJECT_COLS) }}>
            <div>Name</div><div>Engine</div><div>Schedule</div><div>Last run</div><div />
          </div>
          {projects.map((p) => (
            <div
              key={p.id}
              onClick={() => onOpen(p)}
              style={{ ...gridRow(PROJECT_COLS), padding: ROW_PAD, fontSize: 13, cursor: "pointer", borderBottom: "0.5px solid #f3f4f6", background: "white" }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "#F9FAFB")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "white")}
            >
              <div>
                <div style={{ fontWeight: 600 }}>{p.name}</div>
                <div style={{ fontSize: 11, color: "#9ca3af", fontFamily: "monospace" }}>{p.git_url.split("/").slice(-2).join("/")}</div>
              </div>
              <div><span style={{ background: "#FFF7ED", color: "#1e40af", padding: "2px 8px", borderRadius: 4, fontSize: 11.5, fontWeight: 500 }}>{p.engine}</span></div>
              <div style={{ fontSize: 12, color: "#6b7280", fontFamily: "monospace" }}>{p.schedule_enabled ? p.schedule_cron : <span style={{ color: "#9ca3af" }}>manual</span>}</div>
              <DbtLastRunCell run={p.last_run} unit={k.unit} />
              <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
                {onDelete && (
                  <IconAction icon={ACTION_ICON.delete} label="Delete project" danger
                    onClick={(e) => { e.stopPropagation(); onDelete(p); }} />
                )}
                <i className="ti ti-chevron-right" style={{ color: "#d1d5db", fontSize: 14 }} />
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/** Bindings tab: every ruleset ↔ job binding, filterable by job type. */
import type { DqBinding, UUID } from "../../../types";
import { JOB_TYPE_STYLE, MODE_STYLE, cellEllipsis, chip, jobTypeLabel } from "../dqUi";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

interface Props {
  bindings: DqBinding[] | undefined;
  loading: boolean;
  filtered: boolean;
  selectedId: UUID | null;
  resolving: boolean;
  /** dq_publish: resolve / remove / create bindings (W-009). Read-only viewers get the list and the rendered artifact only. */
  canPublish: boolean;
  onOpen: (b: DqBinding) => void;
  onResolve: (b: DqBinding) => void;
  onDelete: (b: DqBinding) => void;
}

export function DqBindingsTable({ bindings, loading: loadingBindings, filtered, selectedId, resolving, canPublish, onOpen, onResolve, onDelete }: Props) {
  return (
    <div>
    {loadingBindings && (
      <div style={{ padding: 24, textAlign: "center", color: "#73827a", fontSize: 13 }}>Loading bindings…</div>
    )}
    {!loadingBindings && !bindings?.length && (
      <div style={{ padding: 32, textAlign: "center" }}>
        <i className="ti ti-checkup-list" style={{ fontSize: 32, color: "#cfd8d1", display: "block", marginBottom: 10 }} />
        <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 5 }}>
          {filtered ? "No bindings for this job type" : "No bindings yet"}
        </div>
        <div style={{ fontSize: 12.5, color: "#5b6d62" }}>
          Attach a ruleset to a dbt project, FlinkSQL job or Python job to run its checks.
        </div>
      </div>
    )}
    {bindings && bindings.length > 0 && (
      <>
        <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr 1fr 1.4fr 0.8fr 120px", gap: 12, padding: "6px 16px", fontSize: 10.5, color: "#73827a", textTransform: "uppercase", letterSpacing: ".06em", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9" }}>
          <div>Ruleset</div><div>Job type</div><div>Target table</div>
          <div>Dataset</div><div>Enforcement</div><div />
        </div>
        {bindings.map((b) => {
          const js = JOB_TYPE_STYLE[b.job_type];
          const ms = MODE_STYLE[b.enforcement_mode];
          const isSelected = selectedId === b.id;
          return (
            <div
              key={b.id}
              onClick={() => onOpen(b)}
              style={{
                display: "grid", gridTemplateColumns: "1.3fr 1fr 1fr 1.4fr 0.8fr 120px", gap: 12,
                alignItems: "center", padding: "9px 16px", fontSize: 12.5,
                borderBottom: "0.5px solid #f0f0ed", cursor: "pointer",
                background: isSelected ? "#f5f9ff" : "transparent",
              }}
            >
              <div style={cellEllipsis}>{b.ruleset?.name ?? b.ruleset_id}</div>
              <div>
                <span style={chip(js.fg, js.bg)}>
                  <i className={`ti ${js.icon}`} style={{ fontSize: 12 }} />
                  {jobTypeLabel(b.job_type)}
                </span>
              </div>
              <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 12 }}>{b.target_table}</div>
              <div style={{ ...cellEllipsis, fontFamily: "monospace", fontSize: 11.5, color: "#5b6d62" }}>{b.ucs_fqn}</div>
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                <span style={chip(ms.fg, ms.bg)}>{b.enforcement_mode}</span>
                {!b.resolved_at && (
                  <span title="Not resolved against its job and dataset — apply is refused until resolved" style={chip("#a8470a", "#fdebdd")}>unresolved</span>
                )}
              </div>
              <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                {canPublish && !b.resolved_at && (
                  <IconAction size="sm" icon="ti-link" label="Resolve the job and dataset (required before apply)"
                    disabled={resolving} onClick={(e) => { e.stopPropagation(); onResolve(b); }} />
                )}
                <IconAction size="sm" icon={ACTION_ICON.view} label="View the rendered artifact"
                  onClick={(e) => { e.stopPropagation(); onOpen(b); }} />
                {canPublish && (
                  <IconAction size="sm" icon={ACTION_ICON.delete} label="Remove binding" danger
                    onClick={(e) => { e.stopPropagation(); onDelete(b); }} />
                )}
              </div>
            </div>
          );
        })}
      </>
    )}
    </div>
  );
}

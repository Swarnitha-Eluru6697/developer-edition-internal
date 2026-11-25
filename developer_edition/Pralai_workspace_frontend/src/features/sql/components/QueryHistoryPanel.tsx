/**
 * QueryHistoryPanel — the SQL query-history sidebar shown in the editor's
 * bottom panel. Lists the user's past SQL executions (most recent first);
 * clicking one loads that query's results into the Results pane.
 */
import { useVisibleExecution } from "../hooks/useVisibleExecution";
import { useListExecutionsQuery } from "../api";
import { timeAgo } from "../../../shared/format";
import type { ExecutionHistoryItem, UUID } from "../../../types";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

interface Props {
  workspaceId: UUID;
  onSelect: (item: ExecutionHistoryItem) => void;
  onClose: () => void;
  // Take the full bottom-panel width when the results table is closed.
  fullWidth?: boolean;
}

const firstLine = (sql: string) => {
  const line = sql.split("\n").find((l) => l.trim()) ?? sql;
  return line.trim();
};

export function QueryHistoryPanel({ workspaceId, onSelect, onClose, fullWidth }: Props) {
  const activeId = useVisibleExecution().id;
  // Poll so a query just run shows up (and flips pending → completed) shortly.
  const { data: items, isLoading, isError, refetch } = useListExecutionsQuery(
    { workspace_id: workspaceId, language: "sql", limit: 50 },
    { pollingInterval: 4000 },
  );

  return (
    <div style={{ ...(fullWidth ? { flex: 1, minWidth: 0 } : { width: 288, flexShrink: 0 }), borderLeft: "0.5px solid #E3E8E2", display: "flex", flexDirection: "column", background: "#F8FAF9", overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "5px 10px", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9", flexShrink: 0 }}>
        <i className="ti ti-history" style={{ fontSize: 13, color: "#5b6d62" }} />
        <span style={{ fontSize: 11.5, fontWeight: 600, color: "#2E3D36" }}>Query history</span>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 2 }}>
          <IconAction size="sm" icon={ACTION_ICON.refresh} label="Refresh" onClick={() => refetch()} />
          <IconAction size="sm" icon={ACTION_ICON.close} label="Hide history" onClick={onClose} />
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {isLoading && <div style={{ padding: 12, fontSize: 11.5, color: "#73827a" }}>Loading…</div>}
        {isError && <div style={{ padding: 12, fontSize: 11.5, color: "#b23b3b" }}>Couldn't load history.</div>}
        {items && items.length === 0 && <div style={{ padding: 12, fontSize: 11.5, color: "#73827a" }}>No queries yet. Run a SQL statement to see it here.</div>}
        {(items ?? []).map((it) => {
          const failed = it.status === "failed";
          const running = it.status === "pending" || it.status === "running";
          const dot = failed ? "#b23b3b" : running ? "#d97706" : "#16a34a";
          const active = it.id === activeId;
          return (
            <div
              key={it.id}
              onClick={() => onSelect(it)}
              title={it.code_text}
              style={{
                padding: "7px 10px", borderBottom: "0.5px solid #efefec", cursor: "pointer",
                background: active ? "#eef2ff" : "transparent",
                borderLeft: active ? "2px solid #0A7D5A" : "2px solid transparent",
              }}
              onMouseEnter={(e) => { if (!active) (e.currentTarget as HTMLDivElement).style.background = "#f3f3f1"; }}
              onMouseLeave={(e) => { if (!active) (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                <span style={{ width: 6, height: 6, borderRadius: "50%", background: dot, flexShrink: 0 }} />
                <span style={{ fontFamily: "monospace", fontSize: 11, color: "#14201B", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{firstLine(it.code_text) || "(empty)"}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 10, color: "#73827a", paddingLeft: 12 }}>
                <span>{timeAgo(it.created_at)}</span>
                {failed
                  ? <span style={{ color: "#b23b3b" }}>failed</span>
                  : running
                    ? <span style={{ color: "#d97706" }}>{it.status}</span>
                    : <span>{(it.row_count ?? 0).toLocaleString()} rows{it.duration_ms ? ` · ${it.duration_ms}ms` : ""}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

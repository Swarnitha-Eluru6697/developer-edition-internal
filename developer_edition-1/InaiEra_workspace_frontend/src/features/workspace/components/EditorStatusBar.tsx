/** Bottom status bar: Git branch/sync state, Push, and the Results / History toggles. */
import { useDispatch, useSelector } from "react-redux";
import { GitBranchStatus } from "../../git";
import { useVisibleExecution } from "../../sql";
import { setResultsOpen, toggleHistory } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { UUID } from "../../../types";

const smallBtn = { height: 20, fontSize: 10.5, padding: "0 7px", border: "0.5px solid #d1d5db", borderRadius: 4, background: "#fff", cursor: "pointer", color: "#6b7280" } as const;

export function EditorStatusBar({ workspaceId }: { workspaceId: UUID | null }) {
  const dispatch = useDispatch<AppDispatch>();
  const resultsOpen = useSelector((s: RootState) => s.ui.resultsOpen);
  const historyOpen = useSelector((s: RootState) => s.ui.historyOpen);
  const hasExecution = useVisibleExecution().id !== null;

  return (
    <div style={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 9, padding: "4px 12px", borderTop: "0.5px solid #E5E7EB", background: "#F9FAFB", fontSize: 11.5 }}>
      <GitBranchStatus workspaceId={workspaceId} />
      <button style={smallBtn}>Push</button>
      {!resultsOpen && hasExecution && (
        <button onClick={() => dispatch(setResultsOpen(true))} title="Show results" style={smallBtn}>
          <i className="ti ti-table" style={{ fontSize: 11, marginRight: 3 }} />Results
        </button>
      )}
      <button onClick={() => dispatch(toggleHistory())} title="Query history" style={{ height: 20, display: "flex", alignItems: "center", background: "none", border: "none", cursor: "pointer", color: historyOpen ? "#F97316" : "#9ca3af", padding: 0 }}>
        <i className="ti ti-history" style={{ fontSize: 13 }} />
      </button>
    </div>
  );
}

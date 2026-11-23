/**
 * The editor's bottom panel: Results and/or Query history, under a draggable
 * divider (drag up = taller, double-click = reset).
 */
import { useCallback } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useDragResize } from "../../../shared/hooks/useDragResize";
import { setHistoryOpen } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { UUID } from "../../../types";
import { useResultsActions } from "../hooks/useResultsActions";
import { RESULTS_HEIGHT_DEFAULT } from "../results";
import { QueryHistoryPanel } from "./QueryHistoryPanel";
import { ResultsPanel } from "./ResultsPanel";

interface Props {
  workspaceId: UUID | null;
  filePath: string | null;
  height: number;
  onHeightChange: (h: number) => void;
}

export function ResultsDock({ workspaceId, filePath, height, onHeightChange }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const resultsOpen = useSelector((s: RootState) => s.ui.resultsOpen);
  const historyOpen = useSelector((s: RootState) => s.ui.historyOpen);
  const { downloadResults, copyResults, selectHistory } = useResultsActions();
  const compute = useCallback(
    (start: number, delta: number) => Math.min(Math.max(start + delta, 80), window.innerHeight - 180),
    [],
  );
  const { start } = useDragResize({ axis: "y", current: height, compute, onResize: onHeightChange });

  return (
    <>
      <div
        onMouseDown={start}
        onDoubleClick={() => onHeightChange(RESULTS_HEIGHT_DEFAULT)}
        title="Drag to resize · double-click to reset"
        style={{ height: 6, flexShrink: 0, cursor: "row-resize", background: "#E5E7EB", borderTop: "0.5px solid #E5E7EB" }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "#FED7AA")}
        onMouseLeave={(e) => (e.currentTarget.style.background = "#E5E7EB")}
      />
      <div style={{ height, display: "flex", overflow: "hidden", flexShrink: 0 }}>
        {resultsOpen && <ResultsPanel onDownload={() => downloadResults(filePath)} onCopy={copyResults} />}
        {historyOpen && workspaceId && (
          <QueryHistoryPanel
            workspaceId={workspaceId}
            fullWidth={!resultsOpen}
            onSelect={selectHistory}
            onClose={() => dispatch(setHistoryOpen(false))}
          />
        )}
      </div>
    </>
  );
}

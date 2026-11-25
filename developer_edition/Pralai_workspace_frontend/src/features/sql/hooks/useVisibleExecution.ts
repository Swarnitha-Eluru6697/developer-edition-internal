import { useSelector } from "react-redux";
import type { RootState } from "../../../store";
import { visibleExecution, type ExecutionUIState } from "../executionSlice";

/** The execution the Results pane should show right now: the one that belongs to the ACTIVE editor tab, or an empty one. */
export function useVisibleExecution(): ExecutionUIState {
  return useSelector((s: RootState) => visibleExecution(s.execution, s.ui.activeTab));
}

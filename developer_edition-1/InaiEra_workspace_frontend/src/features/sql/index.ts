/** SQL feature module: execution sessions, SQL runs, results and query history. */
export { ResultsDock } from "./components/ResultsDock";
export { ResultsPanel } from "./components/ResultsPanel";
export { QueryHistoryPanel } from "./components/QueryHistoryPanel";
export { useExecutionSession } from "./hooks/useExecutionSession";
export { useResultsActions } from "./hooks/useResultsActions";
export { useVisibleExecution } from "./hooks/useVisibleExecution";
export { useRunCode, FILE_CELL_ID } from "./hooks/useRunCode";
export {
  default as executionReducer,
  beginExecution,
  resetExecution,
  reassignExecutionOwner,
  setExecutionResult,
  showHistoricalExecution,
  visibleExecution,
  type ExecutionUIState,
} from "./executionSlice";
export {
  RESULTS_HEIGHT_DEFAULT, historySeed, isTerminal, resultSets, resultSetsFromWire, resultSheetName,
  resultsFileName, resultsToSheetRows, type ResultSet,
} from "./results";
export * from "./api";

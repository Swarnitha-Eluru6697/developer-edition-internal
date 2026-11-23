/**
 * Codegen feature module: the SQL / dbt / FlinkSQL authoring chat.
 *
 * CodegenChatPanel.tsx still physically lives in src/components/ (another
 * engineer owns that file right now); it is re-exported here so callers depend
 * on the feature's public API, not on its location.
 */
export { CodegenChatPanel } from "./components/CodegenChatPanel";
export { CodegenChatColumn } from "./components/CodegenChatColumn";
export {
  default as codegenReducer,
  beginCodegenTurn,
  clearCodegenHistory,
  clearCodegenSession,
  endCodegenTurn,
  failCodegenTurn,
  setCodegenSession,
  setCodegenEngine,
  setCodegenTarget,
} from "./codegenSlice";
export { DEFAULT_ENGINE, ENGINE_OPTIONS, SELECTABLE_ENGINES, isSelectableEngine, targetUsesEngine, type CodegenEngine } from "./engines";
export * from "./api";

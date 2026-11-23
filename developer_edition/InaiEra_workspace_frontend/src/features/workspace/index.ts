/**
 * Workspace feature module: workspaces, files and the editor screen
 * (file panel, tabs, Monaco editor, home screen) plus the controller hook
 * that wires the sql / notebook / flink / codegen features into it.
 */
export { WorkspaceFilePanel } from "./components/WorkspaceFilePanel";
export { EditorArea } from "./components/EditorArea";
export { HomeScreen } from "./components/HomeScreen";
export { CodeEditor } from "./components/CodeEditor";
export { FilePanelTabs } from "./components/FilePanelTabs";
export { useWorkspaceEditor, type WorkspaceEditor } from "./hooks/useWorkspaceEditor";
export { useEditorDrafts } from "./hooks/useEditorDrafts";
export { flattenMe } from "./me";
export { baseName, collectFileIds, collectFilePaths, emptyNotebook, fileIcon, fileTreeView, findNodeByPath, languageFromFileName, type FileIcon } from "./fileTree";
export {
  NEW_FILE_OPTIONS,
  allowedNewFileOptions,
  isUntitledPath,
  seedContent,
  suggestedSaveName,
  untitledFileName,
  type NewFileLanguage,
  type NewFileOption,
} from "./newFiles";
export { deleteWorkspaceConfirm, filterWorkspaces, resolveActiveWorkspace,
  pickInitialWorkspace,
  readRememberedWorkspace,
  rememberWorkspace,
  activeWorkspaceStorageKey, workspaceLabel, type WorkspaceLabel } from "./workspaces";
export * from "./api";

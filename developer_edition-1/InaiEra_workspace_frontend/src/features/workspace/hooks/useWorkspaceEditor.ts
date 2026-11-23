import { useCallback, useEffect, useMemo, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useGetFileQuery, workspaceFeatureApi } from "../api";
import { useFlinkValidation } from "../../flink";
import { RESULTS_HEIGHT_DEFAULT, useExecutionSession, useResultsActions, useRunCode } from "../../sql";
import { askSaveChanges } from "../../../shared/ui/dialogStore";
import { closeTab, notify, requestCellInsert, setNavSection } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { CodegenTarget, CurrentUser, FileLanguage, UUID } from "../../../types";
import { useActiveWorkspace } from "./useActiveWorkspace";
import { useEditorDrafts } from "./useEditorDrafts";
import { useFileActions } from "./useFileActions";
import { useLocalBuffers } from "./useLocalBuffers";
import { useEditorTabsMemory } from "./useEditorTabsMemory";
import { useOpenFilePaths } from "./useOpenFilePaths";
import { baseName } from "../fileTree";
import { isLocalBufferId, localBufferFile } from "../localBuffers";

/**
 * Everything the Workspace editor screen shares between its file panel, tab
 * area and codegen column: the active workspace, the open file and its
 * draft, the execution session, and the run / save / create / insert
 * handlers. One instance lives in the App shell.
 */
export function useWorkspaceEditor(me: CurrentUser | undefined) {
  const dispatch = useDispatch<AppDispatch>();
  const activeTabId = useSelector((s: RootState) => s.ui.activeTab);
  const activeConnectionId = useSelector((s: RootState) => s.ui.activeConnectionId);
  const { workspaces, activeWs, setActiveWs, dropWorkspace } = useActiveWorkspace(me);

  const localBuffers = useLocalBuffers();
  const isLocalTab = isLocalBufferId(activeTabId);
  const { data: serverFile } = useGetFileQuery(
    { workspaceId: activeWs ?? "", fileId: activeTabId ?? "" },
    { skip: !activeTabId || !activeWs || isLocalTab },
  );

  const drafts = useEditorDrafts(serverFile, activeTabId);
  const { setDraft, appendToDraft, isDirty, forget } = drafts;
  const openTabs = useSelector((s: RootState) => s.ui.openTabs);
  // Only tabs that are still open count: a deleted file's leftover text must not keep the page "dirty".
  const dirtyIds = useMemo(() => new Set(openTabs.filter((id) => drafts.dirtyIds.has(id))), [openTabs, drafts.dirtyIds]);
  const draft = activeTabId ? (drafts.drafts[activeTabId] ?? "") : "";
  // An unsaved new file (Untitled-N) is presented as a file so the rest of the editor needs no special case.
  const localBuffer = isLocalTab && activeTabId ? localBuffers.buffers[activeTabId] : undefined;
  const file = useMemo(
    () => (localBuffer && activeWs ? localBufferFile(localBuffer, activeWs, draft) : serverFile),
    [localBuffer, activeWs, draft, serverFile],
  );
  // `data` can briefly be the previous tab's file while the new one loads —
  // fine to keep rendering, but never act (save/run) on the wrong file.
  const activeFile = file && file.id === activeTabId ? file : undefined;
  const [selectedSql, setSelectedSql] = useState("");
  // Height of the Results/History dock; kept here so it survives tab switches.
  const [resultsHeight, setResultsHeight] = useState(RESULTS_HEIGHT_DEFAULT);

  const execSessionId = useExecutionSession(me, activeWs);
  const { pollResult } = useResultsActions();
  const runCode = useRunCode(execSessionId, pollResult);
  const flink = useFlinkValidation(activeWs, activeTabId);
  const { save: saveFile, newFile, newDbtProject, newWorkspace, renameWorkspace, deleteWorkspace } = useFileActions({ activeWs, setActiveWs, dropWorkspace, drafts, localBuffers });

  const save = useCallback(() => { void saveFile(activeFile, draft); }, [saveFile, activeFile, draft]);

  const validateFlink = flink.validate;
  const run = useCallback(async () => {
    if (!activeWs || !activeFile) return;
    // Notebooks run per-cell inside NotebookView, not through the shared runner.
    if (activeFile.language === "ipynb") return;
    if (activeFile.language === "flinksql") { await validateFlink(draft); return; }
    // Only SQL (lakehouse / connection) and Python (notebook kernel) execute. Markdown, YAML, JSON and
    // other text used to be sent to the Python kernel and fail there with a confusing traceback.
    if (activeFile.language !== "sql" && activeFile.language !== "py") {
      dispatch(notify({ kind: "info", message: `${baseName(activeFile.path)} isn't runnable — only SQL and Python files run.` }));
      return;
    }
    // Run the highlighted selection if any (DBeaver/pgAdmin style); otherwise
    // the whole script (multi-statement → last result).
    await runCode({
      fileId: isLocalTab ? null : activeFile.id,
      language: activeFile.language as FileLanguage,
      code: selectedSql.trim() || draft,
      connectionId: activeConnectionId,
      ownerTab: activeFile.id,
    });
  }, [activeWs, activeFile, isLocalTab, validateFlink, draft, runCode, selectedSql, activeConnectionId, dispatch]);

  // Claude-style "edit in code": drop generated code where it belongs for the
  // chosen target — the open FlinkSQL editor (or a new *.flink.sql file), a
  // new notebook cell, or the open text editor's draft.
  const insertGeneratedCode = useCallback(async (code: string, target: CodegenTarget) => {
    if (target === "flinksql") {
      dispatch(setNavSection("ws"));
      if (activeTabId && file?.language === "flinksql") {
        setDraft(activeTabId, code);
        dispatch(notify({ kind: "success", message: "Inserted into the FlinkSQL editor." }));
      } else {
        await newFile("flinksql", code);
        dispatch(notify({ kind: "success", message: "Opened the generated FlinkSQL in a new editor." }));
      }
      return;
    }
    if (!activeTabId || !file) {
      dispatch(notify({ kind: "info", message: "Open a file first, then insert." }));
      return;
    }
    if (file.language === "ipynb") {
      dispatch(requestCellInsert({ code, language: target === "python" ? "python" : "sql" }));
      dispatch(notify({ kind: "success", message: "Added as a new notebook cell." }));
    } else {
      setDraft(activeTabId, code);
      dispatch(notify({ kind: "success", message: "Inserted into the editor." }));
    }
  }, [activeTabId, dispatch, file, newFile, setDraft]);

  // ── Unsaved changes ────────────────────────────────────────────────────
  const filePaths = useOpenFilePaths(activeWs);
  useEditorTabsMemory(activeWs, filePaths);         // the open files come back after a refresh
  /** What a tab is called: the file's name (an unsaved new file is "Untitled-N"). */
  const tabLabel = useCallback((id: UUID): string => {
    if (isLocalBufferId(id)) return localBuffers.buffers[id]?.name ?? "Untitled";
    const path = filePaths.get(id);
    return path ? baseName(path) : `file_${id.slice(0, 8)}`;
  }, [filePaths, localBuffers.buffers]);
  const tabTitle = useCallback((id: UUID): string | undefined => filePaths.get(id), [filePaths]);

  // Save the text of ANY open tab (not only the active one), for "Save" in the close prompt.
  const saveTab = useCallback(async (id: UUID): Promise<boolean> => {
    const text = drafts.drafts[id] ?? "";
    if (id === activeTabId && activeFile) return saveFile(activeFile, text);
    if (!activeWs) return false;
    if (isLocalBufferId(id)) {
      const buffer = localBuffers.buffers[id];
      return buffer ? saveFile(localBufferFile(buffer, activeWs, text), text) : false;
    }
    try {
      const stored = await dispatch(
        workspaceFeatureApi.endpoints.getFile.initiate({ workspaceId: activeWs, fileId: id }, { subscribe: false, forceRefetch: true }),
      ).unwrap();
      return saveFile(stored, text);
    } catch {
      dispatch(notify({ kind: "error", message: "Could not load the file to save it." }));
      return false;
    }
  }, [activeTabId, activeFile, activeWs, dispatch, drafts.drafts, localBuffers.buffers, saveFile]);

  /** Close a tab. With unsaved changes, ask first: Save / Don't Save / Cancel. */
  const requestCloseTab = useCallback(async (id: UUID) => {
    if (isDirty(id)) {
      const choice = await askSaveChanges(tabLabel(id));
      if (choice === "cancel") return;
      if (choice === "save" && !(await saveTab(id))) return;   // a cancelled or failed save keeps the tab open
    }
    dispatch(closeTab(id));
    forget(id);                                  // "Don't Save": reopening shows the saved file again
    if (isLocalBufferId(id)) localBuffers.remove(id);
  }, [dispatch, forget, isDirty, localBuffers, saveTab, tabLabel]);

  // Closing or reloading the page would silently drop unsaved text: let the browser ask.
  useEffect(() => {
    if (dirtyIds.size === 0) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyIds]);

  return {
    workspaces, activeWs, setActiveWs,
    activeTabId, file, draft, drafts: drafts.drafts, setDraft, appendToDraft, setSelectedSql,
    dirtyIds, tabLabel, tabTitle, requestCloseTab,
    resultsHeight, setResultsHeight,
    execSessionId,
    flinkCheck: flink.result, validatingFlink: flink.validating, dismissFlinkCheck: flink.dismiss,
    validateFlink: () => validateFlink(draft),
    run, save, insertGeneratedCode,
    newFile, newDbtProject, newWorkspace,
    renameActiveWorkspace: () => renameWorkspace(workspaces?.find((w) => w.id === activeWs)),
    deleteActiveWorkspace: () => deleteWorkspace(workspaces?.find((w) => w.id === activeWs)),
  };
}

export type WorkspaceEditor = ReturnType<typeof useWorkspaceEditor>;

import { useEffect, useRef } from "react";
import { useDispatch, useSelector } from "react-redux";
import { restoreTabs } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { UUID } from "../../../types";
import { readEditorTabs, saveEditorTabs, tabsToRestore } from "../editorTabs";

/**
 * Keep the editor's open files across a refresh (see editorTabs.ts).
 *
 * Once the workspace's files are known (`filePaths`, the file tree), reopen the remembered tabs that still exist, then keep what is
 * open remembered. Nothing is written before that first look, or the empty start-up state would erase what is about to be restored.
 */
export function useEditorTabsMemory(activeWs: UUID | null, filePaths: ReadonlyMap<UUID, string>) {
  const dispatch = useDispatch<AppDispatch>();
  const openTabs = useSelector((s: RootState) => s.ui.openTabs);
  const activeTab = useSelector((s: RootState) => s.ui.activeTab);
  const lookedAt = useRef<UUID | null>(null);

  useEffect(() => {
    if (!activeWs) return;
    if (lookedAt.current !== activeWs) {
      if (filePaths.size === 0) return;                 // the file tree is not in yet (or the workspace has no files)
      lookedAt.current = activeWs;
      if (openTabs.length === 0) {
        const back = tabsToRestore(readEditorTabs(), activeWs, new Set(filePaths.keys()));
        if (back) dispatch(restoreTabs(back));
      }
      return;
    }
    saveEditorTabs(activeWs, openTabs, activeTab);
  }, [activeWs, filePaths, openTabs, activeTab, dispatch]);
}

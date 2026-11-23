/**
 * The files open in the editor, and the Files / Changes tab of the file panel, survive a refresh. Tabs come back only for the SAME
 * workspace, only for files that still exist in it, never an unsaved new file, and never before the workspace's files are known.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { describe, expect, it } from "vitest";
import uiReducer, { closeTab, openTab, readStoredFilePanelTab, restoreTabs, setFilePanelTab, FILE_PANEL_TAB_KEY } from "../../src/store/uiSlice";
import { readEditorTabs, saveEditorTabs, tabsToRestore } from "../../src/features/workspace/editorTabs";
import { useEditorTabsMemory } from "../../src/features/workspace/hooks/useEditorTabsMemory";
import { writePersisted } from "../../src/shared/usePersistedState";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const WS = "ws-1";
const [A, B, C] = ["a-file", "b-file", "c-file"];
const FILES = new Map([[A, "a.sql"], [B, "b.sql"], [C, "c.sql"]]);

describe("the file panel's Files / Changes tab", () => {
  it("starts on Files, comes back as it was left, and ignores junk", () => {
    expect(readStoredFilePanelTab()).toBe("files");
    writePersisted(FILE_PANEL_TAB_KEY, "changes", "files");
    expect(readStoredFilePanelTab()).toBe("changes");
    window.sessionStorage.setItem(`inaiera.ui.${FILE_PANEL_TAB_KEY}`, '"gone"');
    expect(readStoredFilePanelTab()).toBe("files");
  });
});

describe("saving and choosing what to reopen", () => {
  it("remembers the open files with their workspace, and nothing once none is open", () => {
    saveEditorTabs(WS, [A, B], B);
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A, B], activeTab: B });
    saveEditorTabs(WS, [], null);
    expect(readEditorTabs()).toBeNull();
  });

  it("leaves out an unsaved new file, and falls back to the last real tab as active", () => {
    saveEditorTabs(WS, [A, "local:Untitled-1"], "local:Untitled-1");
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A], activeTab: A });
    saveEditorTabs(WS, ["local:Untitled-1"], "local:Untitled-1");
    expect(readEditorTabs()).toBeNull();
  });

  it("reopens only the same workspace's tabs, and only files that still exist", () => {
    const stored = { workspaceId: WS, openTabs: [A, B, "deleted"], activeTab: "deleted" };
    expect(tabsToRestore(stored, WS, new Set([A, B, C]))).toEqual({ openTabs: [A, B], activeTab: B });
    expect(tabsToRestore(stored, "another-ws", new Set([A, B]))).toBeNull();
    expect(tabsToRestore({ ...stored, openTabs: ["deleted"] }, WS, new Set([A]))).toBeNull();
    expect(tabsToRestore(null, WS, new Set([A]))).toBeNull();
    expect(tabsToRestore({ ...stored, activeTab: A }, WS, new Set([A, B]))?.activeTab).toBe(A);
  });

  it("ignores a stored value of the wrong shape", () => {
    window.sessionStorage.setItem("inaiera.ui.ws.editorTabs", '{"workspaceId":1,"openTabs":"x"}');
    expect(readEditorTabs()).toBeNull();
  });
});

describe("the slice", () => {
  it("restoreTabs reopens files only when nothing is open yet", () => {
    let state = uiReducer(undefined, { type: "@@INIT" });
    state = uiReducer(state, restoreTabs({ openTabs: [A, B], activeTab: A }));
    expect([state.openTabs, state.activeTab]).toEqual([[A, B], A]);
    state = uiReducer(state, restoreTabs({ openTabs: [C], activeTab: C }));          // something is open now: left alone
    expect([state.openTabs, state.activeTab]).toEqual([[A, B], A]);
  });

  it("setFilePanelTab still switches the tab", () => {
    expect(uiReducer(undefined, setFilePanelTab("changes")).filePanelTab).toBe("changes");
  });
});

// ── the hook, with the real slice ───────────────────────────────────────────────────────────────────────────────────────
function Harness({ ws, files }: { ws: string | null; files: Map<string, string> }) {
  useEditorTabsMemory(ws, files);
  return null;
}
function boot(ws: string | null, files: Map<string, string>) {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const render = (w: string | null, f: Map<string, string>) =>
    act(() => root.render(createElement(Provider, { store, children: createElement(Harness, { ws: w, files: f }) })));
  render(ws, files);
  return { store, render, tabs: () => (store.getState() as { ui: { openTabs: string[]; activeTab: string | null } }).ui };
}

describe("useEditorTabsMemory", () => {
  it("reopens the remembered files after a refresh, and the one that was active", () => {
    saveEditorTabs(WS, [A, B], A);
    const app = boot(WS, FILES);
    expect(app.tabs()).toMatchObject({ openTabs: [A, B], activeTab: A });
  });

  it("waits for the workspace's files: nothing is reopened, and nothing is erased, before they are known", () => {
    saveEditorTabs(WS, [A, B], A);
    const app = boot(WS, new Map());
    expect(app.tabs().openTabs).toEqual([]);
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A, B], activeTab: A });      // still remembered
    app.render(WS, FILES);                                                                       // the file tree arrives
    expect(app.tabs().openTabs).toEqual([A, B]);
  });

  it("does not reopen a file that no longer exists, and drops it from what is remembered", () => {
    saveEditorTabs(WS, [A, "gone"], "gone");
    const app = boot(WS, FILES);
    expect(app.tabs()).toMatchObject({ openTabs: [A], activeTab: A });
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A], activeTab: A });
  });

  it("does not reopen another workspace's files", () => {
    saveEditorTabs("other-ws", [A], A);
    expect(boot(WS, FILES).tabs().openTabs).toEqual([]);
  });

  it("keeps what is open remembered as files are opened and closed, and forgets it when the last is closed", () => {
    const app = boot(WS, FILES);
    act(() => { app.store.dispatch(openTab(A)); app.store.dispatch(openTab(B)); });
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A, B], activeTab: B });
    act(() => { app.store.dispatch(closeTab(B)); });
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A], activeTab: A });
    act(() => { app.store.dispatch(closeTab(A)); });
    expect(readEditorTabs()).toBeNull();
  });

  it("does nothing without a workspace", () => {
    saveEditorTabs(WS, [A], A);
    expect(boot(null, FILES).tabs().openTabs).toEqual([]);
    expect(readEditorTabs()).toEqual({ workspaceId: WS, openTabs: [A], activeTab: A });
  });
});

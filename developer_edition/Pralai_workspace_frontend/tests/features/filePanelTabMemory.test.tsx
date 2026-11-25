/** Workspace → the file panel's Files / Changes tab: a refresh keeps the tab you were on. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../src/features/workspace/api", () => ({
  useGetFileTreeQuery: () => ({ data: undefined, isLoading: false, refetch: vi.fn() }),
  useCreateFileMutation: () => [vi.fn()], useDeleteFileMutation: () => [vi.fn()],
}));
vi.mock("../../src/rbac/hooks", () => ({ useHasPermission: () => true }));
vi.mock("../../src/features/git", () => ({
  GitChangesPanel: () => createElement("div", { "data-testid": "changes-panel" }),
  dirtyFileCount: () => 0,
  useGetGitBindingQuery: () => ({ data: undefined }),
  useGetGitStatusQuery: () => ({ data: undefined }),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const KEY = "inaiera.ui.ws.filePanelTab";

beforeEach(() => { document.body.innerHTML = ""; vi.resetModules(); });

/** Load the slice and the panel fresh, as a page load does (the slice reads its remembered tab when it is first imported). */
async function boot() {
  const { default: ui } = await import("../../src/store/uiSlice");
  const { FilePanelTabs } = await import("../../src/features/workspace/components/FilePanelTabs");
  const store = configureStore({ reducer: { ui } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(FilePanelTabs, { workspaceId: "ws-1", activeFileId: null }) })));
  const click = async (name: string) => act(async () => { [...host.querySelectorAll<HTMLElement>("div")].find((d) => d.textContent?.startsWith(name) && d.children.length <= 1 && d.style.cursor === "pointer")!.click(); });
  return { host, store, click, tab: () => (store.getState() as { ui: { filePanelTab: string } }).ui.filePanelTab };
}

describe("the file panel's Files / Changes tab", () => {
  it("opens on Files the first time", async () => {
    expect((await boot()).tab()).toBe("files");
  });

  it("saves the tab you switch to, and a refresh opens on it", async () => {
    const first = await boot();
    await first.click("Changes");
    expect(first.tab()).toBe("changes");
    expect(window.sessionStorage.getItem(KEY)).toBe('"changes"');
    document.body.innerHTML = "";
    vi.resetModules();                                                                  // a refresh: a new page load
    const again = await boot();
    expect(again.tab()).toBe("changes");
    expect(again.host.querySelector('[data-testid="changes-panel"]')).toBeTruthy();
  });

  it("switching back to Files leaves nothing remembered", async () => {
    const screen = await boot();
    await screen.click("Changes");
    await screen.click("Files");
    expect(screen.tab()).toBe("files");
    expect(window.sessionStorage.getItem(KEY)).toBeNull();
  });

  it("ignores a remembered tab that is not one of the two", async () => {
    window.sessionStorage.setItem(KEY, '"history"');
    expect((await boot()).tab()).toBe("files");
  });
});

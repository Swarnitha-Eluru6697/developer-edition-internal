/** Without workspace:access:view the app is not shown at all: a clear "no access" screen takes its place. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  permissions: [] as string[], status: "ready" as "loading" | "ready" | "error", editorMe: [] as unknown[],
}));
vi.mock("../../src/rbac/hooks", () => ({
  usePermissionsState: () => ({ permissions: state.permissions, heldPermissions: state.permissions, status: state.status, error: state.status === "error" ? { status: 503 } : undefined }),
  usePermissions: () => state.permissions,
  useHasPermission: (p: string) => state.permissions.includes(p),
  useHasAnyPermission: (ps: string[]) => ps.some((p) => state.permissions.includes(p)),
}));
vi.mock("../../src/api/client", async (orig) => ({
  ...(await orig<typeof import("../../src/api/client")>()),
  useGetMeQuery: () => ({ data: { user_id: "sub-1", org_id: "org-1", email: "ada@example.com", display_name: "Ada" }, error: undefined }),
}));
vi.mock("../../src/features/workspace", async (orig) => ({
  ...(await orig<typeof import("../../src/features/workspace")>()),
  useWorkspaceEditor: (me: unknown) => {
    state.editorMe.push(me);
    return { workspaces: [], activeWs: null, setActiveWs() {}, file: null, activeTabId: null, save() {}, run() {}, newFile() {}, newDbtProject() {} };
  },
  HomeScreen: () => createElement("div", null, "HOME"),
  WorkspaceFilePanel: () => null,
  EditorArea: () => null,
}));
vi.mock("../../src/app/AppHeader", () => ({ AppHeader: () => createElement("div", null, "HEADER") }));

import { App } from "../../src/app/App";
import { store } from "../../src/store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(App) })));
  return host;
}

beforeEach(() => {
  document.body.innerHTML = "";
  state.editorMe = [];
});

describe("workspace:access:view", () => {
  it("without it: the no-access screen, no sidebar, and the editor never loads the user's workspaces", () => {
    state.status = "ready";
    state.permissions = ["workspace:sql:run"];
    const host = mount();
    expect(host.textContent).toContain("You don't have access to Workspace");
    expect(host.textContent).not.toContain("PralaiWorkspace");
    expect(host.textContent).not.toContain("HOME");
    expect(state.editorMe.every((m) => m === undefined)).toBe(true);
  });

  it("with it: the app", () => {
    state.status = "ready";
    state.permissions = ["workspace:access:view"];
    const host = mount();
    expect(host.textContent).not.toContain("You don't have access");
    expect(host.textContent).toContain("PralaiWorkspace");
    expect(host.textContent).toContain("HOME");
    expect(state.editorMe.at(-1)).toBeTruthy();
  });

  it("while permissions load: neither the app nor the no-access screen", () => {
    state.status = "loading";
    state.permissions = [];
    const host = mount();
    expect(host.textContent).toContain("Authenticating");
    expect(host.textContent).not.toContain("You don't have access");
  });

  it("if they cannot be loaded: no access (never a privileged default)", () => {
    state.status = "error";
    state.permissions = [];
    const host = mount();
    expect(host.textContent).toContain("You don't have access to Workspace");
    expect(host.textContent).toContain("could not be loaded");
  });
});

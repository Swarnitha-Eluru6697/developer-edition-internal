/**
 * "View as role": the portal passes `viewPermissions` (the chosen role's permission IDs) and the UI shows what held ∩ viewPermissions
 * opens. Real hooks over a stubbed GET /api/v1/me, rendered through the same ViewPermissionsProvider WorkspaceApp uses, so the
 * narrowing in rbac/hooks.ts is what drives the sidebar and the nav guard.
 */
import { createRoot } from "react-dom/client";
import { act, createElement, useEffect } from "react";
import { Provider, useSelector } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { workspaceApi } from "../src/api/client";
import uiReducer, { setNavSection } from "../src/store/uiSlice";
import { Sidebar } from "../src/app/Sidebar";
import { useNavGuard } from "../src/app/hooks/useShellEffects";
import { usePermissionsState } from "../src/rbac/hooks";
import { narrowPermissions } from "../src/rbac/permissions";
import { ViewPermissionsProvider } from "../src/rbac/ViewPermissionsProvider";
import type { RootState } from "../src/store";
import { FakeRequest } from "./helpers/apiHarness";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HELD = ["workspace:access:view", "workspace:dbt:edit", "workspace:alerts:view"];

/** Answer GET /api/v1/me with `permissions`. */
function serveMe(permissions: string[]) {
  vi.stubGlobal("Request", FakeRequest);
  vi.stubGlobal("fetch", vi.fn(async (req: FakeRequest) => {
    if (!req.url.endsWith("/api/v1/me")) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify({ user: "ada", userId: "sub-1", tenant: "org-1", permissions }), { status: 200, headers: { "content-type": "application/json" } });
  }));
}

let mounts = 0;
function Shell() {
  useNavGuard();
  useEffect(() => { mounts += 1; }, []);
  const navSection = useSelector((s: RootState) => s.ui.navSection);
  const { heldPermissions } = usePermissionsState();
  return createElement("div", null,
    createElement(Sidebar),
    createElement("div", { "data-testid": "page", "data-section": navSection, "data-held": heldPermissions.length }));
}

function mount(viewPermissions?: string[] | null) {
  const store = configureStore({
    reducer: { [workspaceApi.reducerPath]: workspaceApi.reducer, ui: uiReducer },
    middleware: (gDM) => gDM({ serializableCheck: false }).concat(workspaceApi.middleware),
  });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const render = (view?: string[] | null) =>
    act(() => root.render(createElement(Provider, { store, children: createElement(ViewPermissionsProvider, { viewPermissions: view, children: createElement(Shell) }) })));
  render(viewPermissions);
  const navLabels = () => [...host.querySelectorAll<HTMLElement>("div")].filter((d) => d.style.cursor === "pointer").map((d) => d.textContent?.trim());
  const page = () => host.querySelector<HTMLElement>("[data-testid='page']")!;
  return { host, store, render, navLabels, page, unmount: () => act(() => root.unmount()) };
}
const settle = () => act(async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setTimeout(r, 0)); });

beforeEach(() => {
  document.body.innerHTML = "";
  localStorage.clear();
  mounts = 0;
  serveMe(HELD);
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("narrowPermissions", () => {
  it("is held ∩ viewAs, and null / undefined leave held untouched", () => {
    const held = ["workspace:access:view", "workspace:dbt:edit"] as const;
    expect(narrowPermissions(held, ["workspace:dbt:edit"])).toEqual(["workspace:dbt:edit"]);
    expect(narrowPermissions(held, null)).toBe(held);
    expect(narrowPermissions(held, undefined)).toBe(held);
  });

  it("never adds a permission the user lacks", () => {
    expect(narrowPermissions(["workspace:access:view"], ["workspace:access:view", "workspace:settings:edit"])).toEqual(["workspace:access:view"]);
  });
});

describe("view as role", () => {
  it("narrowing hides a tab the full set shows", async () => {
    const full = mount(null);
    await settle();
    expect(full.navLabels()).toEqual(["Workspace", "dbt", "Alerts"]);
    full.unmount();

    const narrowed = mount(["workspace:access:view", "workspace:alerts:view"]);
    await settle();
    expect(narrowed.navLabels()).toEqual(["Workspace", "Alerts"]);
    narrowed.unmount();
  });

  it("viewPermissions can never add a permission the user lacks", async () => {
    const t = mount(["workspace:access:view", "workspace:settings:edit", "workspace:flink:deploy", "workspace:alerts:view"]);
    await settle();
    expect(t.navLabels()).toEqual(["Workspace", "Alerts"]);
    t.unmount();
  });

  it("null and undefined change nothing", async () => {
    for (const view of [null, undefined]) {
      const t = mount(view);
      await settle();
      expect(t.navLabels()).toEqual(["Workspace", "dbt", "Alerts"]);
      t.unmount();
    }
  });

  it("switching updates in place (no remount), and a page no longer allowed falls back to Workspace", async () => {
    const t = mount(null);
    await settle();
    act(() => { t.store.dispatch(setNavSection("dbt")); });
    expect(t.page().dataset.section).toBe("dbt");

    t.render(["workspace:access:view", "workspace:alerts:view"]);
    await settle();
    expect(t.navLabels()).toEqual(["Workspace", "Alerts"]);
    expect(t.page().dataset.section).toBe("ws");
    // The held set (what opening the app at all is decided on) is untouched.
    expect(t.page().dataset.held).toBe("3");

    t.render(null);
    await settle();
    expect(t.navLabels()).toEqual(["Workspace", "dbt", "Alerts"]);
    expect(mounts).toBe(1);
    t.unmount();
  });
});

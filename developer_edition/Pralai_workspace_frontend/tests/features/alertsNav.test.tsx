/** Alerts in the sidebar: who sees it, the open-alerts badge, that a refresh stays on it, and that arriving from the sidebar opens the first tab. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import uiReducer, { readStoredNavSection, saveNavSection, setNavSection } from "../../src/store/uiSlice";
import { canOpenSection, visibleNavSections } from "../../src/app/navigation";
import { forgetPlaceOnNavigate } from "../../src/store/placeMemory";
import { writePersisted } from "../../src/shared/usePersistedState";

const ALL = ["workspace:access:view", "workspace:alerts:view", "workspace:alerts:edit", "workspace:alerts:manage", "workspace:dq:view", "workspace:settings:edit"];
const state = vi.hoisted(() => ({ permissions: [] as string[], open: 3 }));
vi.mock("../../src/rbac/hooks", () => ({ usePermissions: () => state.permissions, useHasPermission: () => true }));
vi.mock("../../src/features/alerts/api", () => ({
  useGetAlertCountsQuery: (_: unknown, o?: { skip?: boolean }) => ({ data: o?.skip ? undefined : { open: state.open, acknowledged: 0, resolved: 0, undelivered: 0 } }),
}));

import { Sidebar } from "../../src/app/Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => { document.body.innerHTML = ""; sessionStorage.clear(); state.permissions = ALL; state.open = 3; });

function mountSidebar() {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(Sidebar) })));
  return { store, host };
}

describe("navigation", () => {
  it("Alerts sits under Govern, between Data Quality and Settings", () => {
    const govern = visibleNavSections(ALL).find((g) => g.label === "Govern")!;
    expect(govern.items.map((i) => i.id)).toEqual(["dq", "alerts", "settings"]);
    expect(govern.items[1]).toMatchObject({ label: "Alerts", icon: "pi-bell" });
  });

  it("opens for anyone holding workspace:alerts:view (acknowledging and managing are gated inside the page); other permissions do not open it", () => {
    expect(canOpenSection("alerts", ["workspace:alerts:view"])).toBe(true);
    expect(canOpenSection("alerts", ["workspace:alerts:edit", "workspace:alerts:manage", "workspace:dq:view", "workspace:access:view"])).toBe(false);
    expect(canOpenSection("alerts", [])).toBe(false);
  });
});

describe("sidebar", () => {
  it("shows how many alerts are open on the Alerts entry", () => {
    const t = mountSidebar();
    const badge = t.host.querySelector("[aria-label='3 open alerts']");
    expect(badge?.textContent).toBe("3");
    expect(badge?.parentElement?.textContent).toContain("Alerts");
  });

  it("shows no badge when nothing is open, and none on any other entry", () => {
    state.open = 0;
    const t = mountSidebar();
    expect(t.host.querySelector("[aria-label$='open alerts']")).toBeNull();
  });

  it("a click on Alerts opens the page", () => {
    const t = mountSidebar();
    const row = [...t.host.querySelectorAll<HTMLElement>("div")].find((d) => d.textContent === "Alerts3")!;
    act(() => row.click());
    expect(t.store.getState().ui.navSection).toBe("alerts");
  });
});

describe("staying and arriving", () => {
  it("a refresh stays on Alerts (the section is remembered), and an unknown value falls back to Workspace", () => {
    saveNavSection("alerts");
    expect(readStoredNavSection()).toBe("alerts");
    sessionStorage.setItem("inaiera.navSection", "nonsense");
    expect(readStoredNavSection()).toBe("ws");
  });

  it("arriving from the sidebar forgets the tab you were on, so the page opens on Open; a refresh keeps it", () => {
    const store = configureStore({ reducer: { ui: uiReducer }, middleware: (g) => g().concat(forgetPlaceOnNavigate) });
    writePersisted("alerts.tab", "channels", "open");
    store.dispatch(setNavSection("dbt"));
    store.dispatch(setNavSection("alerts"));
    expect(sessionStorage.getItem("inaiera.ui.alerts.tab")).toBeNull();
    writePersisted("alerts.tab", "rules", "open");
    store.dispatch(setNavSection("alerts"));                                        // already there: nothing is forgotten
    expect(sessionStorage.getItem("inaiera.ui.alerts.tab")).toBe('"rules"');
  });
});

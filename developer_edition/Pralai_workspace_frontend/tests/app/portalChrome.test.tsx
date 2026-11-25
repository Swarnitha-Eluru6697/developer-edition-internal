/**
 * Portal chrome: hosted by the Control Portal (it passes `roleBadges`), the header shows the portal's role-badge strip and leaves out
 * the bell and the avatar (the portal rail carries both). Standalone (`roleBadges` undefined) keeps the bell and the avatar. An older
 * portal's `accountMenu` is still drawn. The Workspace mark stays at the top of the sidebar either way.
 */
import { createRoot } from "react-dom/client";
import { act, createElement, type ReactNode } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import uiReducer from "../../src/store/uiSlice";

vi.mock("../../src/rbac/hooks", () => ({ usePermissions: () => [], useHasPermission: () => false }));
vi.mock("../../src/features/workspace/api", () => ({
  useGetMeQuery: () => ({ data: { display_name: "Ada Lovelace", email: "ada@example.com" } }),
  useListFilesQuery: () => ({ data: [] }),
}));
vi.mock("../../src/features/dbt/api", () => ({ useListDbtProjectsQuery: () => ({ data: [] }) }));
vi.mock("../../src/features/flink/api", () => ({ useListFlinkJobsQuery: () => ({ data: [] }) }));
vi.mock("../../src/features/alerts/api", () => ({ useGetAlertCountsQuery: () => ({ data: undefined }) }));

import { setRuntime } from "../../src/api/apiBase";
import { AppHeader } from "../../src/app/AppHeader";
import { Sidebar } from "../../src/app/Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount(children: ReactNode) {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children })));
  return {
    host,
    bell: () => host.querySelector("button[title='Notifications']"),
    avatar: () => host.querySelector("button[aria-label='Account menu']"),
    badges: () => host.querySelector<HTMLElement>("[data-testid='portal-role-badges']"),
  };
}

const header = () => createElement(AppHeader, { workspaces: [], activeWs: null, onSelectWorkspace: () => {} });
const badgeStrip = createElement("span", { className: "role-badges" }, "Builder | Agent Admin | End User");

beforeEach(() => {
  document.body.innerHTML = "";
  setRuntime({ embedded: false, accountMenu: null, roleBadges: undefined });
});

describe("portal chrome", () => {
  it("hosted (roleBadges given): shows the badges on one line, no header bell or avatar", () => {
    setRuntime({ embedded: true, roleBadges: badgeStrip });
    const t = mount(header());
    expect(t.badges()?.querySelector(".role-badges")?.textContent).toBe("Builder | Agent Admin | End User");
    expect(t.badges()?.style.whiteSpace).toBe("nowrap");
    expect(t.bell()).toBeNull();
    expect(t.avatar()).toBeNull();
  });

  it("hosted by an older portal that still passes accountMenu: that menu is drawn", () => {
    setRuntime({ embedded: true, roleBadges: badgeStrip, accountMenu: createElement("button", { "aria-label": "Portal account" }) });
    const t = mount(header());
    expect(t.host.querySelector("button[aria-label='Portal account']")).not.toBeNull();
    expect(t.bell()).toBeNull();
  });

  it("standalone (roleBadges undefined): keeps the bell and the avatar, no badge strip", () => {
    const t = mount(header());
    expect(t.badges()).toBeNull();
    expect(t.bell()).not.toBeNull();
    expect(t.avatar()).not.toBeNull();
  });

  it("the Workspace mark is the first thing at the top of the sidebar", () => {
    const t = mount(createElement(Sidebar));
    const top = t.host.firstElementChild!.firstElementChild!;
    expect(top.textContent).toBe("PralaiWorkspace");
  });
});

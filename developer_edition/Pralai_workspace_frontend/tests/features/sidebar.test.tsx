/** The Workspace sidebar shows the product mark and the navigation only — no signed-in user row at the bottom. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => "workspace" }));
const held = vi.hoisted(() => ({ permissions: [] as string[] }));
vi.mock("../../src/rbac/hooks", () => ({ usePermissions: () => held.permissions }));

// The Alerts entry shows how many alerts are open; this test is about the user row, so no alerts are open.
vi.mock("../../src/features/alerts/api", () => ({ useGetAlertCountsQuery: () => ({ data: undefined }) }));

import { Sidebar } from "../../src/app/Sidebar";
import { PERMISSIONS } from "../../src/rbac/generated/permissions";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("sidebar", () => {
  it("has no user row: no user icon and no user id or name", () => {
    held.permissions = [...PERMISSIONS];
    const host = document.createElement("div");
    act(() => createRoot(host).render(createElement(Sidebar)));
    expect(host.textContent).toContain("PralaiWorkspace");
    expect(host.querySelector(".ti-user-circle")).toBeNull();
    expect(host.textContent).not.toContain("4a5ef138");
  });

  const labels = (permissions: string[]) => {
    held.permissions = permissions;
    const host = document.createElement("div");
    act(() => createRoot(host).render(createElement(Sidebar)));
    return [...host.querySelectorAll<HTMLElement>("div[style*='cursor: pointer']")].map((d) => d.textContent);
  };

  it("lists every page for a holder of every permission", () => {
    expect(labels([...PERMISSIONS])).toEqual(["Workspace", "dbt", "SQL", "Python", "Flink", "Data Quality", "Alerts", "Settings"]);
  });

  it("hides the pages whose permission the user lacks (and the empty Jobs group)", () => {
    const host = document.createElement("div");
    held.permissions = ["workspace:access:view"];
    act(() => createRoot(host).render(createElement(Sidebar)));
    expect(host.textContent).not.toContain("Jobs");
    expect(labels(["workspace:access:view"])).toEqual(["Workspace"]);
    expect(labels(["workspace:access:view", "workspace:flink:deploy", "workspace:dq:view"])).toEqual(["Workspace", "Flink", "Data Quality"]);
    expect(labels(["workspace:access:view", "workspace:settings:edit"])).toEqual(["Workspace", "Settings"]);
  });
});

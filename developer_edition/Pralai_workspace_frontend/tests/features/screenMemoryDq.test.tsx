/** Govern → Data Quality: refreshing keeps the tab, the job-type filter and the binding you had open. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ bindings: undefined as unknown[] | undefined, filter: [] as unknown[] }));
vi.mock("../../src/features/dq/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDqBindingsQuery: (arg?: { jobType?: string }) => {
      api.filter.push(arg?.jobType ?? "");
      const list = api.bindings && arg?.jobType ? (api.bindings as { job_type: string }[]).filter((b) => b.job_type === arg.jobType) : api.bindings;
      return { data: list, isLoading: api.bindings === undefined };
    },
    useGetDqRenderQuery: () => ({ data: undefined, isFetching: false, error: undefined }),
    useListDqRulesetsQuery: () => ({ data: [] }),
    useCreateDqBindingMutation: mutation, useDeleteDqBindingMutation: mutation, useRefreshDqRenderMutation: mutation,
    useApplyDqBindingMutation: mutation, useResolveDqBindingMutation: mutation,
  };
});
vi.mock("../../src/features/dbt", () => ({ useListDbtProjectsQuery: () => ({ data: [] }), useGetDbtLineageQuery: () => ({ data: undefined, isFetching: false }) }));
vi.mock("../../src/features/flink", () => ({ flinkSinks: () => [], useListFlinkJobsQuery: () => ({ data: [] }) }));
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => undefined }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { DataQualityPanel } from "../../src/features/dq/components/DataQualityPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const binding = (id: string, table: string, job_type = "dbt_project") => ({
  id, org_id: "o", job_id: "j", job_type, ruleset_id: "r", ruleset: { name: `rules_${table}` }, target_table: table, ucs_fqn: `cat.${table}`,
  enforcement_mode: "warn", resolved_at: "2026-10-16T00:00:00Z", created_at: "2026-10-16T00:00:00Z", created_by: null,
});

let screens: { unmount: () => void }[] = [];
function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const render = () => act(() => root.render(createElement(DataQualityPanel)));
  render();
  const handle = { host, render, unmount: () => act(() => root.unmount()) };
  screens.push(handle);
  return handle;
}
const tabs = (host: HTMLElement) => [...host.querySelectorAll<HTMLElement>("div")].filter((d) => d.children.length === 0 && /^(bindings|render)$/.test(d.textContent ?? ""));
const openTab = (host: HTMLElement) => tabs(host).find((t) => /2px solid (rgb\(10, 125, 90\)|#0A7D5A)/i.test(t.style.borderBottom))?.textContent;
const onRender = (host: HTMLElement) => openTab(host) === "render";
const openBinding = async (host: HTMLElement, table: string) =>
  act(async () => { [...host.querySelectorAll<HTMLElement>("div")].find((d) => d.children.length === 0 && d.textContent === table)!.click(); });

beforeEach(() => {
  screens.forEach((s) => { try { s.unmount(); } catch { /* gone */ } });
  screens = [];
  document.body.innerHTML = "";
  api.filter = [];
  api.bindings = [binding("b1", "orders"), binding("b2", "customers"), binding("b3", "events", "flink_job")];
});

describe("Data Quality: refreshing keeps your place", () => {
  it("a refresh on a binding's Render tab keeps that binding and the tab", async () => {
    const first = mount();
    expect(openTab(first.host)).toBe("bindings");
    await openBinding(first.host, "customers");
    expect(onRender(first.host)).toBe(true);
    first.unmount();
    const again = mount();
    expect(onRender(again.host)).toBe(true);
    expect(again.host.textContent).toContain("customers");
  });

  it("the Bindings tab is remembered too, and starts as Bindings", async () => {
    expect(openTab(mount().host)).toBe("bindings");
  });

  it("going back to the Bindings tab is remembered: a refresh then stays there", async () => {
    const first = mount();
    await openBinding(first.host, "customers");
    await act(async () => { tabs(first.host).find((t) => t.textContent === "bindings")!.click(); });
    first.unmount();
    expect(openTab(mount().host)).toBe("bindings");
  });

  it("the Render tab waits for the list instead of falling back to Bindings while it loads", () => {
    window.sessionStorage.setItem("inaiera.ui.dq.tab", '"render"');
    window.sessionStorage.setItem("inaiera.ui.dq.binding", '"b2"');
    api.bindings = undefined;
    const screen = mount();
    expect(window.sessionStorage.getItem("inaiera.ui.dq.tab")).toBe('"render"');                    // not reset while loading
    api.bindings = [binding("b1", "orders"), binding("b2", "customers")];
    screen.render();
    expect(onRender(screen.host)).toBe(true);
  });

  it("a binding that no longer exists is forgotten and the Bindings tab shows", () => {
    window.sessionStorage.setItem("inaiera.ui.dq.tab", '"render"');
    window.sessionStorage.setItem("inaiera.ui.dq.binding", '"deleted-elsewhere"');
    const screen = mount();
    expect(openTab(screen.host)).toBe("bindings");
    expect(window.sessionStorage.getItem("inaiera.ui.dq.binding")).toBeNull();
  });

  it("the Render tab with no binding remembered falls back to Bindings", () => {
    window.sessionStorage.setItem("inaiera.ui.dq.tab", '"render"');
    expect(openTab(mount().host)).toBe("bindings");
  });

  it("the job-type filter is remembered and used for the list after a refresh", () => {
    window.sessionStorage.setItem("inaiera.ui.dq.filter", '"flink_job"');
    const screen = mount();
    expect(api.filter).toContain("flink_job");
    expect(screen.host.textContent).toContain("events");
    expect(screen.host.textContent).not.toContain("orders");
  });

  it("junk in storage is ignored", () => {
    window.sessionStorage.setItem("inaiera.ui.dq.tab", '"nonsense"');
    window.sessionStorage.setItem("inaiera.ui.dq.filter", '"not-a-job-type"');
    window.sessionStorage.setItem("inaiera.ui.dq.binding", "7");
    const screen = mount();
    expect(openTab(screen.host)).toBe("bindings");
    expect(api.filter.every((f) => f === "")).toBe(true);
  });
});

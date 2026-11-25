/**
 * The header search finds jobs (dbt, SQL, Python, Flink) by name, engine or Git URL and opens the one picked: "trino" used to say
 * "No matches" on a page full of Trino jobs because only files, workspaces and pages were searched.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import uiReducer from "../../src/store/uiSlice";
import { PERMISSIONS } from "../../src/rbac/generated/permissions";

const api = vi.hoisted(() => ({ permissions: [] as string[], projects: [] as unknown[], flink: [] as unknown[], skipped: [] as string[] }));
vi.mock("../../src/rbac/hooks", () => ({
  usePermissions: () => api.permissions,
  useHasPermission: (p: string) => api.permissions.includes(p),
}));
vi.mock("../../src/features/workspace/api", () => ({ useListFilesQuery: () => ({ data: [] }) }));
vi.mock("../../src/features/dbt/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDbtProjectsQuery: (_a: unknown, o?: { skip?: boolean }) => { if (o?.skip) api.skipped.push("dbt"); return { data: o?.skip ? undefined : api.projects, isLoading: false, refetch: vi.fn() }; },
    useListDbtRunsQuery: () => ({ data: [] }),
    useListDbtRunEventsQuery: () => ({ data: [] }),
    useGetDbtLineageQuery: () => ({ data: undefined }),
    useTriggerDbtRunMutation: mutation,
    useDeleteDbtProjectMutation: mutation,
    useRegisterDbtProjectMutation: mutation,
    useUpdateDbtProjectMutation: mutation,
  };
});
vi.mock("../../src/features/flink/api", () => ({
  useListFlinkJobsQuery: (_a: unknown, o?: { skip?: boolean }) => { if (o?.skip) api.skipped.push("flink"); return { data: o?.skip ? undefined : api.flink }; },
}));

import { HeaderSearch } from "../../src/app/HeaderSearch";
import { DbtRunnerPanel } from "../../src/features/dbt/components/DbtRunnerPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const job = (name: string, kind: "dbt" | "sql" | "python", engine = "trino") => ({
  id: `id-${name}`, org_id: "o", workspace_id: null, name, kind, git_url: "https://github.com/acme/r.git", git_ref: "main", project_path: name, engine,
  schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false, credential_ref: null, last_synced_sha: null, requested_ref: null,
  created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", last_run: null, dependent_project_ids: [], upstream_project_ids: [],
  data_upstream_project_ids: [],
});

function mount() {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(HeaderSearch, { workspaces: [], activeWs: null, onSelectWorkspace() {} }) })));
  const type = async (text: string) => {
    const box = host.querySelector("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const rows = () => [...host.querySelectorAll<HTMLElement>("div[style*='cursor: pointer']")].map((r) => r.textContent ?? "");
  const pick = async (label: string) => {
    const row = [...host.querySelectorAll<HTMLElement>("div[style*='cursor: pointer']")].find((r) => r.textContent?.startsWith(label))!;
    await act(async () => { row.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true })); });
  };
  return { store, host, type, rows, pick };
}

beforeEach(() => {
  document.body.innerHTML = "";
  sessionStorage.clear();
  api.permissions = [...PERMISSIONS];
  api.skipped = [];
  api.projects = [job("trino_sales", "dbt"), job("customers", "dbt", "pg_lake"), job("daily.sql", "sql"), job("etl.py", "python", "pg_lake")];
  api.flink = [{ id: "fl-1", name: "orders-stream", git_url: "https://github.com/acme/flink.git" }];
});

describe("header search: jobs", () => {
  it("finds the jobs that run on an engine, by name or engine, across dbt / SQL / Python", async () => {
    const t = mount();
    await t.type("trino");
    expect(t.rows()).toEqual(["trino_sales" + "trino", "daily.sql" + "trino"]);
    await t.type("pg_lake");
    expect(t.rows().map((r) => r.replace("pg_lake", ""))).toEqual(["customers", "etl.py"]);
  });

  it("finds a Flink job too, and says what is not found", async () => {
    const t = mount();
    await t.type("orders");
    expect(t.rows()).toEqual(["orders-stream"]);
    await t.type("zzz");
    expect(t.host.textContent).toContain("No matches");
  });

  it("choosing a job opens its page on that job", async () => {
    const t = mount();
    await t.type("daily");
    await t.pick("daily.sql");
    expect(t.store.getState().ui.navSection).toBe("sqljobs");
    expect(t.store.getState().ui.openJob).toEqual({ section: "sqljobs", id: "id-daily.sql" });
  });

  it("does not ask for job lists the user may not open", () => {
    api.permissions = ["workspace:access:view"];
    mount();
    expect(api.skipped).toEqual(expect.arrayContaining(["dbt", "flink"]));
  });

  it("finds only the kinds of job whose page the user may open", async () => {
    api.permissions = ["workspace:access:view", "workspace:sql-jobs:run"];
    const t = mount();
    await t.type("trino");
    expect(t.rows()).toEqual(["daily.sql" + "trino"]);
    expect(api.skipped).toContain("flink");
  });
});

describe("jobs page: a job chosen in the search", () => {
  const mountPanel = (store: ReturnType<typeof mount>["store"], kind: "dbt" | "sql") => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(DbtRunnerPanel, { kind }) })));
    return host;
  };
  const projectName = (host: HTMLElement) => host.querySelector('button[title="Back to all projects"]')?.nextElementSibling?.firstElementChild?.textContent;

  it("opens that job, even when another is already open, and then forgets the request", async () => {
    const t = mount();
    const host = mountPanel(t.store, "dbt");
    expect(projectName(host)).toBeUndefined();                                 // the list
    await act(async () => { t.store.dispatch({ type: "ui/showJob", payload: { section: "dbt", id: "id-customers" } }); });
    expect(projectName(host)).toBe("customers");
    await act(async () => { t.store.dispatch({ type: "ui/showJob", payload: { section: "dbt", id: "id-trino_sales" } }); });
    expect(projectName(host)).toBe("trino_sales");
    expect(t.store.getState().ui.openJob).toBeNull();
  });

  it("ignores a job meant for another page", async () => {
    const t = mount();
    const host = mountPanel(t.store, "dbt");
    await act(async () => { t.store.dispatch({ type: "ui/showJob", payload: { section: "sqljobs", id: "id-daily.sql" } }); });
    expect(projectName(host)).toBeUndefined();
    expect(t.store.getState().ui.openJob).not.toBeNull();
  });
});

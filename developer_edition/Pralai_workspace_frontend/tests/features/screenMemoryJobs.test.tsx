/**
 * Jobs → dbt / SQL / Python: refreshing the page keeps the job you opened and its tab (it used to drop you back at the list).
 * A "refresh" here is unmounting the screen and mounting a new one: the browser tab's sessionStorage stays, everything in memory is gone.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";

const api = vi.hoisted(() => ({ projects: [] as unknown[], loading: false }));
vi.mock("../../src/features/dbt/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDbtProjectsQuery: () => (api.loading ? { data: undefined, isLoading: true, refetch: vi.fn() } : { data: api.projects, isLoading: false, refetch: vi.fn() }),
    useListDbtRunsQuery: () => ({ data: [] }),
    useListDbtRunEventsQuery: () => ({ data: [] }),
    useGetDbtLineageQuery: () => ({ data: undefined }),
    useTriggerDbtRunMutation: mutation,
    useDeleteDbtProjectMutation: mutation,
    useRegisterDbtProjectMutation: mutation,
    useUpdateDbtProjectMutation: mutation,
  };
});
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => "jobs" }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { DbtRunnerPanel } from "../../src/features/dbt/components/DbtRunnerPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, kind: "dbt" | "sql" | "python"): DbtProject => ({
  id, org_id: "o", workspace_id: null, name: id, kind, git_url: "https://github.com/acme/r.git", git_ref: "main", project_path: id, engine: "trino",
  schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false, credential_ref: null, last_synced_sha: null, requested_ref: null,
  created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", last_run: null, dependent_project_ids: [], upstream_project_ids: [],
  data_upstream_project_ids: [],
}) as DbtProject;

type Kind = "dbt" | "sql" | "python";
let open: { unmount: () => void }[] = [];

function mount(kind: Kind = "dbt") {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const render = () => act(() => root.render(createElement(DbtRunnerPanel, { kind })));
  render();
  const handle = { host, render, unmount: () => act(() => root.unmount()) };
  open.push(handle);
  return handle;
}
/** A page refresh: the screen in memory is gone, a new one starts. */
const refresh = (screen: { unmount: () => void }, kind: Kind = "dbt") => { screen.unmount(); return mount(kind); };

const clickText = async (host: HTMLElement, text: string) =>
  act(async () => { [...host.querySelectorAll<HTMLElement>("div")].find((d) => d.children.length === 0 && d.textContent === text)!.click(); });
const inProject = (host: HTMLElement) => !!host.querySelector('button[title="Back to all projects"]');
const projectName = (host: HTMLElement) => host.querySelector('button[title="Back to all projects"]')?.nextElementSibling?.firstElementChild?.textContent;
const openTab = (host: HTMLElement) =>
  [...host.querySelectorAll<HTMLElement>("div")].find((d) => d.children.length === 0 && /2px solid (rgb\(10, 125, 90\)|#0A7D5A)/i.test(d.style.borderBottom))?.textContent;

beforeEach(() => {
  open.forEach((h) => { try { h.unmount(); } catch { /* already gone */ } });
  open = [];
  document.body.innerHTML = "";
  api.loading = false;
  api.projects = [project("customers", "dbt"), project("scorecard", "dbt"), project("daily.sql", "sql"), project("etl.py", "python")];
});

describe("jobs screens: refreshing keeps your place", () => {
  it("a refresh inside a job keeps the job open", async () => {
    const screen = mount();
    await clickText(screen.host, "scorecard");
    expect(inProject(screen.host)).toBe(true);
    const again = refresh(screen);
    expect(inProject(again.host)).toBe(true);
    expect(projectName(again.host)).toBe("scorecard");
  });

  it("and the tab it was on, not just the job", async () => {
    const screen = mount();
    await clickText(screen.host, "scorecard");
    await clickText(screen.host, "dependents");
    expect(openTab(screen.host)).toBe("dependents");
    const again = refresh(screen);
    expect(projectName(again.host)).toBe("scorecard");
    expect(openTab(again.host)).toBe("dependents");
    expect(again.host.querySelector('[role="tablist"]')).toBeTruthy();               // the links form itself is showing
  });

  it("going back to the list is remembered too: a refresh then stays on the list", async () => {
    const screen = mount();
    await clickText(screen.host, "scorecard");
    await act(async () => { screen.host.querySelector<HTMLButtonElement>('button[title="Back to all projects"]')!.click(); });
    expect(inProject(screen.host)).toBe(false);
    expect(inProject(refresh(screen).host)).toBe(false);
  });

  it("going back also forgets the tab, so nothing stale is kept for the next job", async () => {
    const screen = mount();
    await clickText(screen.host, "scorecard");
    await clickText(screen.host, "dependents");
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.tab")).toBe('"dependents"');
    await act(async () => { screen.host.querySelector<HTMLButtonElement>('button[title="Back to all projects"]')!.click(); });
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.tab")).toBeNull();
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.project")).toBeNull();
  });

  it("opening another job starts on its Runs tab, not on the tab the last one was left on", async () => {
    const screen = mount();
    await clickText(screen.host, "scorecard");
    await clickText(screen.host, "dependents");
    await act(async () => { screen.host.querySelector<HTMLButtonElement>('button[title="Back to all projects"]')!.click(); });
    await clickText(screen.host, "customers");
    expect(openTab(screen.host)).toBe("runs");
  });

  it("dbt, SQL and Python each remember their own job", async () => {
    const dbt = mount("dbt");
    await clickText(dbt.host, "scorecard");
    dbt.unmount();
    const sql = mount("sql");
    expect(inProject(sql.host)).toBe(false);                                         // the SQL screen has not opened anything
    await clickText(sql.host, "daily.sql");
    sql.unmount();
    expect(projectName(mount("dbt").host)).toBe("scorecard");
    expect(projectName(mount("sql").host)).toBe("daily.sql");
    expect(inProject(mount("python").host)).toBe(false);
  });

  it("a screen that is simply mounted again, with nothing navigated, finds its job (that is all a refresh is)", async () => {
    const screen = mount();
    await clickText(screen.host, "customers");
    screen.unmount();
    expect(projectName(mount().host)).toBe("customers");                             // going to a page from the sidebar forgets it: tests/store/placeMemory.test.ts
  });

  it("while the list is still loading the choice is kept, then the job opens", () => {
    api.projects = [project("scorecard", "dbt")];
    window.sessionStorage.setItem("inaiera.ui.jobs.dbt.project", '"scorecard"');
    api.loading = true;
    const screen = mount();
    expect(inProject(screen.host)).toBe(false);
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.project")).toBe('"scorecard"');     // not forgotten while loading
    api.loading = false;
    screen.render();
    expect(projectName(screen.host)).toBe("scorecard");
  });

  it("a job that no longer exists is forgotten: the list shows, and a refresh stays there", () => {
    window.sessionStorage.setItem("inaiera.ui.jobs.dbt.project", '"deleted-elsewhere"');
    window.sessionStorage.setItem("inaiera.ui.jobs.dbt.tab", '"dependents"');
    const screen = mount();
    expect(inProject(screen.host)).toBe(false);
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.project")).toBeNull();
    expect(window.sessionStorage.getItem("inaiera.ui.jobs.dbt.tab")).toBeNull();
  });

  it("a remembered job of another kind is not opened here", () => {
    window.sessionStorage.setItem("inaiera.ui.jobs.sql.project", '"scorecard"');       // scorecard is a dbt project
    expect(inProject(mount("sql").host)).toBe(false);
  });

  it("a remembered Lineage tab means nothing for a SQL job: it opens on Runs", () => {
    window.sessionStorage.setItem("inaiera.ui.jobs.sql.project", '"daily.sql"');
    window.sessionStorage.setItem("inaiera.ui.jobs.sql.tab", '"lineage"');
    const screen = mount("sql");
    expect(projectName(screen.host)).toBe("daily.sql");
    expect(openTab(screen.host)).toBe("runs");
  });

  it("junk in storage is ignored", () => {
    window.sessionStorage.setItem("inaiera.ui.jobs.dbt.project", "12345");
    window.sessionStorage.setItem("inaiera.ui.jobs.dbt.tab", '"no-such-tab"');
    expect(inProject(mount().host)).toBe(false);
  });
});

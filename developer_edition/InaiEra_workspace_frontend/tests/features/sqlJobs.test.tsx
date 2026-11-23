/** Jobs → SQL: the dbt screens reused for SQL jobs (one .sql file of a repo, run on a schedule like a dbt project). */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";
import { searchSelectOptions } from "../helpers/searchSelect";

const api = vi.hoisted(() => ({ projects: [] as unknown[], registered: [] as Record<string, unknown>[] }));
vi.mock("../../src/features/dbt/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDbtProjectsQuery: () => ({ data: api.projects, isLoading: false, refetch: vi.fn() }),
    useListDbtRunsQuery: () => ({ data: [] }),
    useListDbtRunEventsQuery: () => ({ data: [] }),
    useGetDbtLineageQuery: () => ({ data: undefined }),
    useTriggerDbtRunMutation: mutation,
    useUpdateDbtProjectMutation: mutation,
    useDeleteDbtProjectMutation: mutation,
    useRegisterDbtProjectMutation: () => [(body: Record<string, unknown>) => {
      api.registered.push(body);
      return { unwrap: async () => ({ name: "x" }) };
    }, { isLoading: false }],
  };
});
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => "sqljobs" }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { DbtRunnerPanel } from "../../src/features/dbt/components/DbtRunnerPanel";
import { failedModelsText } from "../../src/features/dbt/runCounts";
import { NAV_SECTIONS } from "../../src/app/navigation";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, kind: "dbt" | "sql" | undefined, path: string, over: Partial<DbtProject> = {}): DbtProject => ({
  id, org_id: "o", workspace_id: null, name: `inaiera-dbt-test/${path}`, ...(kind ? { kind } : {}), git_url: "https://github.com/acme/r.git",
  git_ref: "main", project_path: path, engine: "pg_lake", schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false,
  credential_ref: null, last_synced_sha: null, requested_ref: null, created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z",
  last_run: null, dependent_project_ids: [], data_upstream_project_ids: [], ...over,
}) as DbtProject;

function render(el: ReturnType<typeof createElement>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(el));
  return host;
}

const button = (host: HTMLElement, text: string) => [...host.querySelectorAll("button")].find((b) => b.textContent?.includes(text))!;

beforeEach(() => {
  document.body.innerHTML = "";
  api.registered = [];
  api.projects = [
    project("c", undefined, "customers"),                      // an older runner sends no kind: a dbt project
    project("s", "sql", "jobs/daily_totals.sql", { last_run: { id: "r", status: "succeeded", trigger: "scheduled", started_at: null,
      completed_at: null, duration_ms: 1, models_total: 3, models_failed: 0, error_message: null } }),
  ];
});

describe("Jobs → SQL", () => {
  it("is in the sidebar under Jobs, for whoever may edit or run SQL jobs", () => {
    const jobs = NAV_SECTIONS.find((s) => s.label === "Jobs")!.items;
    expect(jobs.map((i) => i.label)).toEqual(["dbt", "SQL", "Python", "Flink"]);
    expect(jobs.find((i) => i.label === "SQL")!.permission).toEqual(["workspace:sql-jobs:edit", "workspace:sql-jobs:run"]);
  });

  it("lists only SQL jobs, counted in statements; the dbt screen lists only dbt projects", () => {
    const sql = render(createElement(DbtRunnerPanel, { kind: "sql" }));
    expect(sql.textContent).toContain("SQL Jobs");
    expect(sql.textContent).toContain("jobs/daily_totals.sql");
    expect(sql.textContent).toContain("3 statements");
    expect(sql.textContent).not.toContain("inaiera-dbt-test/customers");
    document.body.innerHTML = "";
    const dbt = render(createElement(DbtRunnerPanel));
    expect(dbt.textContent).toContain("dbt Projects");
    expect(dbt.textContent).toContain("inaiera-dbt-test/customers");
    expect(dbt.textContent).not.toContain("daily_totals");
  });

  it("registers a SQL file as kind sql", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "sql" }));
    await act(async () => { button(host, "Register SQL job").click(); });
    const drawer = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(drawer.textContent).toContain("SQL file");
    const type = (placeholder: string, value: string) => {
      const input = drawer.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => { type("https://github.com/acme/dbt-project.git", "https://github.com/acme/r.git"); type("jobs/daily_totals.sql", "jobs/weekly.sql"); });
    await act(async () => { [...drawer.querySelectorAll("button")].find((b) => b.textContent === "Register SQL job")!.click(); });
    expect(api.registered).toHaveLength(1);
    expect(api.registered[0]).toMatchObject({ kind: "sql", project_path: "jobs/weekly.sql" });
  });

  it("the engine of a new dbt project starts on Trino, and Trino and ClickHouse are the engines offered", async () => {
    const host = render(createElement(DbtRunnerPanel));
    await act(async () => { button(host, "Register project").click(); });
    const drawer = document.querySelector('[role="dialog"]') as HTMLElement;
    const engine = drawer.querySelector<HTMLElement>("[aria-label='Engine']")!;
    expect(engine.textContent).toBe("Trino");
    expect(await searchSelectOptions(engine)).toEqual(["Trino", "ClickHouse"]);
    expect(drawer.textContent).not.toContain("pg_lake");
  });

  it("a SQL job has Runs, Schedule and Dependents but no Lineage tab", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "sql" }));
    const row = [...host.querySelectorAll("div")].find((d) => d.textContent === "inaiera-dbt-test/jobs/daily_totals.sql")!;
    await act(async () => { row.click(); });
    const tabs = [...host.querySelectorAll("div")].filter((d) => ["runs", "lineage", "schedule", "dependents"].includes(d.textContent ?? ""))
      .map((d) => d.textContent);
    expect(tabs).toEqual(["runs", "schedule", "dependents"]);
  });

  it("a failed run of a SQL job counts statements, a dbt run models", () => {
    expect(failedModelsText({ models_total: 3, models_failed: 1 }, "statement")).toBe("1/3 statements failed");
    expect(failedModelsText({ models_total: 13, models_failed: 1 })).toBe("1/13 models failed");
  });

  it("registering a SQL job: Upstream Jobs and Downstream Jobs list both kinds, tagging dbt jobs and leaving SQL jobs plain", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "sql" }));
    await act(async () => { button(host, "Register SQL job").click(); });
    for (const title of ["Upstream Jobs", "Downstream Jobs"]) {
      const list = document.querySelector(`[role="dialog"] [role="group"][aria-label="${title}"]`) as HTMLElement;
      expect([...list.querySelectorAll("label")].map((l) => l.textContent)).toEqual(["inaiera-dbt-test/customersdbt", "inaiera-dbt-test/jobs/daily_totals.sql"]);
    }
    expect(document.querySelector('[role="dialog"] [role="group"][aria-label="Dependents"]')).toBeNull();
  });

  it("registering a SQL job can name a dbt project as upstream and a SQL job as downstream", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "sql" }));
    await act(async () => { button(host, "Register SQL job").click(); });
    const drawer = document.querySelector('[role="dialog"]') as HTMLElement;
    const type = (placeholder: string, value: string) => {
      const input = drawer.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => { type("https://github.com/acme/dbt-project.git", "https://github.com/acme/r.git"); type("jobs/daily_totals.sql", "jobs/after.sql"); });
    const tick = async (title: string, name: string) => {
      const list = drawer.querySelector(`[role="group"][aria-label="${title}"]`)!;
      await act(async () => { [...list.querySelectorAll("label")].find((l) => l.textContent?.startsWith(name))!.querySelector("input")!.click(); });
    };
    await tick("Upstream Jobs", "inaiera-dbt-test/customers");
    await tick("Downstream Jobs", "inaiera-dbt-test/jobs/daily_totals.sql");
    await act(async () => { [...drawer.querySelectorAll("button")].find((b) => b.textContent === "Register SQL job")!.click(); });
    expect(api.registered[0]).toMatchObject({ kind: "sql", project_path: "jobs/after.sql", upstream_project_ids: ["c"], dependent_project_ids: ["s"] });
  });

  describe("engine", () => {
    const open = async () => {
      const host = render(createElement(DbtRunnerPanel, { kind: "sql" }));
      await act(async () => { button(host, "Register SQL job").click(); });
      return document.querySelector('[role="dialog"]') as HTMLElement;
    };
    const type = (drawer: HTMLElement, placeholder: string, value: string) => {
      const input = drawer.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    const submit = async (drawer: HTMLElement) => {
      await act(async () => { type(drawer, "https://github.com/acme/dbt-project.git", "https://github.com/acme/r.git"); type(drawer, "jobs/daily_totals.sql", "jobs/w.sql"); });
      await act(async () => { [...drawer.querySelectorAll("button")].find((b) => b.textContent === "Register SQL job")!.click(); });
    };

    it("like dbt and Python jobs, a SQL job only picks an engine: there is no connection to run on", async () => {
      const drawer = await open();
      expect(drawer.querySelector('[aria-label="Run on"]')).toBeNull();
      expect(drawer.textContent).not.toContain("Run on");
      const selects = [...drawer.querySelectorAll<HTMLElement>(".ss-trigger")];
      expect(selects).toHaveLength(1);
      expect(await searchSelectOptions(selects[0])).toEqual(["Trino", "ClickHouse"]);
    });

    it("registers on Trino by default and never sends a connection", async () => {
      const drawer = await open();
      await submit(drawer);
      expect(api.registered[0]).toMatchObject({ kind: "sql", engine: "trino" });
      expect(api.registered[0]).not.toHaveProperty("connection_id");
    });

    it("pg_lake is not offered, so a new SQL job is registered on Trino unless ClickHouse is picked", async () => {
      const drawer = await open();
      const engine = drawer.querySelector<HTMLElement>("[aria-label='Engine']")!;
      expect(await searchSelectOptions(engine)).not.toContain("pg_lake");
      await submit(drawer);
      expect(api.registered).toHaveLength(1);
      expect(api.registered[0].engine).toBe("trino");
    });
  });
});

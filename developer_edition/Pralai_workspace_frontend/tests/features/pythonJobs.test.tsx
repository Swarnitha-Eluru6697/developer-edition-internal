/** Jobs → Python: the dbt screens reused for Python jobs (one .py file of a repo, run on a schedule like a dbt project). */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";

const api = vi.hoisted(() => ({ projects: [] as unknown[], registered: [] as Record<string, unknown>[], events: [] as unknown[], runs: [] as unknown[] }));
vi.mock("../../src/features/dbt/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDbtProjectsQuery: () => ({ data: api.projects, isLoading: false, refetch: vi.fn() }),
    useListDbtRunsQuery: () => ({ data: api.runs }),
    useListDbtRunEventsQuery: () => ({ data: api.events }),
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
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => "pythonjobs" }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { DbtRunnerPanel } from "../../src/features/dbt/components/DbtRunnerPanel";
import { NAV_SECTIONS } from "../../src/app/navigation";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, kind: "dbt" | "sql" | "python" | undefined, path: string, over: Partial<DbtProject> = {}): DbtProject => ({
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
  api.events = [];
  api.runs = [];
  api.projects = [
    project("c", undefined, "customers"),
    project("s", "sql", "jobs/daily_totals.sql"),
    project("p", "python", "jobs/pipeline.py", { last_run: { id: "r", status: "succeeded", trigger: "scheduled", started_at: null,
      completed_at: null, duration_ms: 1, models_total: 5, models_failed: 0, error_message: null } }),
  ];
});

describe("Jobs → Python", () => {
  it("is in the sidebar under Jobs, for whoever may edit or run Python jobs", () => {
    const jobs = NAV_SECTIONS.find((s) => s.label === "Jobs")!.items;
    expect(jobs.map((i) => i.label)).toEqual(["dbt", "SQL", "Python", "Flink"]);
    expect(jobs.find((i) => i.label === "Python")!.permission).toEqual(["workspace:python-jobs:edit", "workspace:python-jobs:run"]);
  });

  it("lists only Python jobs, counted in steps; the dbt screen lists only dbt projects", () => {
    const py = render(createElement(DbtRunnerPanel, { kind: "python" }));
    expect(py.textContent).toContain("Python Jobs");
    expect(py.textContent).toContain("jobs/pipeline.py");
    expect(py.textContent).toContain("5 steps");
    expect(py.textContent).not.toContain("inaiera-dbt-test/customers");
    expect(py.textContent).not.toContain("jobs/daily_totals.sql");
  });

  it("registers a Python file as kind python", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "python" }));
    await act(async () => { button(host, "Register Python job").click(); });
    const drawer = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(drawer.textContent).toContain("Python script");
    const type = (placeholder: string, value: string) => {
      const input = drawer.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => { type("https://github.com/acme/dbt-project.git", "https://github.com/acme/r.git"); type("jobs/pipeline.py", "jobs/etl.py"); });
    await act(async () => { [...drawer.querySelectorAll("button")].find((b) => b.textContent === "Register Python job")!.click(); });
    expect(api.registered).toHaveLength(1);
    expect(api.registered[0]).toMatchObject({ kind: "python", project_path: "jobs/etl.py", engine: "trino" });
  });

  it("tells how a Python job gets its packages and warehouse, and offers no connection to run on", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "python" }));
    await act(async () => { button(host, "Register Python job").click(); });
    const drawer = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(drawer.textContent).toContain("Python 3.11");
    expect(drawer.textContent).toContain("requirements.txt");
    expect(drawer.textContent).toContain("INAIERA_TRINO_HOST");
    expect(drawer.textContent).not.toMatch(/INAIERA_DATABASE_URL|pg_lake/);          // pg_lake is not offered any more
    expect(drawer.querySelector('select[aria-label="Run on"]')).toBeNull();
  });

  it("a Python job has Runs, Schedule and Dependents but no Lineage tab", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "python" }));
    const row = [...host.querySelectorAll("div")].find((d) => d.textContent === "inaiera-dbt-test/jobs/pipeline.py")!;
    await act(async () => { row.click(); });
    const tabs = [...host.querySelectorAll("div")].filter((d) => ["runs", "lineage", "schedule", "dependents"].includes(d.textContent ?? ""))
      .map((d) => d.textContent);
    expect(tabs).toEqual(["runs", "schedule", "dependents"]);
  });

  it("registering a Python job: Upstream Jobs and Downstream Jobs list every kind, tagging dbt and SQL jobs and leaving Python jobs plain", async () => {
    const host = render(createElement(DbtRunnerPanel, { kind: "python" }));
    await act(async () => { button(host, "Register Python job").click(); });
    for (const title of ["Upstream Jobs", "Downstream Jobs"]) {
      const list = document.querySelector(`[role="dialog"] [role="group"][aria-label="${title}"]`) as HTMLElement;
      expect([...list.querySelectorAll("label")].map((l) => l.textContent)).toEqual([
        "inaiera-dbt-test/customersdbt", "inaiera-dbt-test/jobs/daily_totals.sqlSQL", "inaiera-dbt-test/jobs/pipeline.py"]);
    }
    expect(document.querySelector('[role="dialog"] [role="group"][aria-label="Dependents"]')).toBeNull();
  });

  it("shows what a failed script printed, open, and why the run failed", async () => {
    api.projects = [project("p", "python", "jobs/pipeline.py", { last_run: { id: "r1", status: "failed", trigger: "manual", started_at: null,
      completed_at: null, duration_ms: 1, models_total: 1, models_failed: 1, error_message: "boom" } })];
    api.runs = [{ id: "r1", project_id: "p", status: "failed", trigger: "manual", created_at: "2026-10-16T00:00:00Z", duration_ms: 1,
      models_total: 1, models_failed: 1, models_succeeded: 0 }];
    api.events = [{ id: "e1", run_id: "r1", event_type: "node_finish", node_id: "step.1", created_at: "2026-10-16T00:00:00Z",
      payload: { status: "error", message: "the script failed (exit code 1)", execution_time: 0.4, output: "Traceback: boom" } }];
    const host = render(createElement(DbtRunnerPanel, { kind: "python" }));
    await act(async () => { [...host.querySelectorAll("div")].find((d) => d.textContent === "inaiera-dbt-test/jobs/pipeline.py")!.click(); });
    await act(async () => { [...host.querySelectorAll("span")].find((d) => d.textContent === "failed")!.closest("div")!.click(); });
    expect(host.textContent).toContain("the script failed (exit code 1)");
    const details = host.querySelector("details")!;
    expect(details.textContent).toContain("Traceback: boom");
    expect(details.open).toBe(true);
  });
});

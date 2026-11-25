/** The job screens save a job's upstream and downstream jobs together: one update carrying both lists. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";

const api = vi.hoisted(() => ({ projects: [] as unknown[], updates: [] as { id: string; body: Record<string, unknown> }[], fail: false }));
vi.mock("../../src/features/dbt/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListDbtProjectsQuery: () => ({ data: api.projects, isLoading: false, refetch: vi.fn() }),
    useListDbtRunsQuery: () => ({ data: [] }),
    useListDbtRunEventsQuery: () => ({ data: [] }),
    useGetDbtLineageQuery: () => ({ data: undefined }),
    useTriggerDbtRunMutation: mutation,
    useDeleteDbtProjectMutation: mutation,
    useRegisterDbtProjectMutation: mutation,
    useUpdateDbtProjectMutation: () => [(arg: { id: string; body: Record<string, unknown> }) => {
      api.updates.push(arg);
      return { unwrap: async () => { if (api.fail) throw { status: 400, data: { detail: { message: "These links would make a loop." } } }; return {}; } };
    }, { isLoading: false }],
  };
});
const dispatched = vi.hoisted(() => ({ actions: [] as { payload?: { kind?: string; message?: string } }[] }));
vi.mock("react-redux", () => ({ useDispatch: () => (a: { payload?: { kind?: string; message?: string } }) => { dispatched.actions.push(a); }, useSelector: () => "jobs" }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { DbtRunnerPanel } from "../../src/features/dbt/components/DbtRunnerPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, kind: "dbt" | "sql" | "python", over: Partial<DbtProject> = {}): DbtProject => ({
  id, org_id: "o", workspace_id: null, name: id, kind, git_url: "https://github.com/acme/r.git", git_ref: "main", project_path: id, engine: "trino",
  schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false, credential_ref: null, last_synced_sha: null, requested_ref: null,
  created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", last_run: null, dependent_project_ids: [], upstream_project_ids: [],
  data_upstream_project_ids: [], ...over,
}) as DbtProject;

beforeEach(() => {
  document.body.innerHTML = ""; api.updates = []; api.fail = false; dispatched.actions = [];
  api.projects = [project("a", "dbt"), project("d", "dbt"), project("e", "sql"), project("p", "python")];
});

async function openTab(kind: "dbt" | "sql" | "python", name: string) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(DbtRunnerPanel, { kind })));
  await act(async () => { [...host.querySelectorAll("div")].find((d) => d.textContent === name)!.click(); });
  await act(async () => { [...host.querySelectorAll("div")].find((d) => d.textContent === "dependents")!.click(); });
  return host;
}
const tick = async (host: HTMLElement, title: string, name: string) => {
  const list = host.querySelector(`[role="group"][aria-label="${title}"]`)!;
  await act(async () => { [...list.querySelectorAll("label")].find((l) => l.querySelector("span")?.textContent === name)!.querySelector("input")!.click(); });
};
const save = async (host: HTMLElement) => { await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Save")!.click(); }); };

describe("job screens: saving upstream and downstream jobs", () => {
  it("one update carries both lists, for a dbt project", async () => {
    const host = await openTab("dbt", "d");
    await tick(host, "Upstream Jobs", "a");
    await tick(host, "Downstream Jobs", "e");
    await save(host);
    expect(api.updates).toEqual([{ id: "d", body: { upstream_project_ids: ["a"], dependent_project_ids: ["e"] } }]);
  });

  it("the same for a SQL job and a Python job", async () => {
    for (const [kind, name] of [["sql", "e"], ["python", "p"]] as const) {
      api.updates = [];
      document.body.innerHTML = "";
      const host = await openTab(kind, name);
      await tick(host, "Upstream Jobs", "a");
      await save(host);
      expect(api.updates).toEqual([{ id: name, body: { upstream_project_ids: ["a"], dependent_project_ids: [] } }]);
    }
  });

  it("clearing both lists sends two empty lists, and says so", async () => {
    api.projects = [project("a", "dbt", { dependent_project_ids: ["d"] }), project("d", "dbt", { upstream_project_ids: ["a"] })];
    const host = await openTab("dbt", "d");
    await tick(host, "Upstream Jobs", "a");                                                   // un-tick the saved upstream
    await save(host);
    expect(api.updates).toEqual([{ id: "d", body: { upstream_project_ids: [], dependent_project_ids: [] } }]);
    expect(dispatched.actions.at(-1)!.payload).toMatchObject({ kind: "success", message: 'Removed the upstream and downstream jobs of "d"' });
  });

  it("a refusal from the Runner (a loop) is shown as the error", async () => {
    api.fail = true;
    const host = await openTab("dbt", "d");
    await tick(host, "Upstream Jobs", "a");
    await save(host);
    expect(dispatched.actions.at(-1)!.payload).toMatchObject({ kind: "error", message: expect.stringContaining("loop") });
  });
});

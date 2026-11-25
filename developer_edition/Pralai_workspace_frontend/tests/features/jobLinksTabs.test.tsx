/**
 * Upstream Jobs and Downstream Jobs are two tabs side by side (like Files | Changes in the file panel), in the register drawer and in a
 * job's links tab. Only one list shows at a time, each tab shows how many jobs are chosen on its side, and switching loses nothing.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";

vi.mock("../../src/features/dbt/api", () => ({
  useRegisterDbtProjectMutation: () => [vi.fn(() => ({ unwrap: async () => ({ name: "x" }) })), { isLoading: false }],
}));
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn() }));

import { DbtDependentsTab } from "../../src/features/dbt/components/DbtDependentsTab";
import { DbtRegisterDrawer } from "../../src/features/dbt/components/DbtRegisterDrawer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, over: Partial<DbtProject> = {}): DbtProject => ({
  id, org_id: "o", workspace_id: null, name: id, kind: "dbt", git_url: `https://github.com/acme/${id}.git`, git_ref: "main", project_path: id,
  engine: "trino", schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false, credential_ref: null, last_synced_sha: null,
  requested_ref: null, created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", last_run: null,
  dependent_project_ids: [], upstream_project_ids: [], ...over,
}) as DbtProject;

const ALL = [project("a"), project("b"), project("c"), project("d")];

function render(el: ReturnType<typeof createElement>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(el));
  return host;
}
const tabs = (host: HTMLElement) => [...host.querySelectorAll<HTMLElement>('[role="tab"]')];
const tab = (host: HTMLElement, name: string) => tabs(host).find((t) => t.textContent?.startsWith(name))!;
const panel = (host: HTMLElement, side: "upstream" | "downstream") => host.querySelector<HTMLElement>(`#job-links-panel-${side}`)!;
const shown = (el: HTMLElement) => el.style.display !== "none";
const box = (host: HTMLElement, title: string, name: string) =>
  [...host.querySelector(`[role="group"][aria-label="${title}"]`)!.querySelectorAll("label")]
    .find((l) => l.querySelector("span")?.textContent === name)!.querySelector("input")!;
const tabForJob = (job: DbtProject, over: Partial<DbtProject> = {}) => {
  const me = { ...job, ...over };
  return render(createElement(DbtDependentsTab, { project: me, projects: [...ALL.filter((p) => p.id !== me.id), me], saving: false, onSave: vi.fn() }));
};

beforeEach(() => { document.body.innerHTML = ""; });

describe("job links tabs", () => {
  it("shows two tabs side by side, Upstream Jobs first and open", () => {
    const host = tabForJob(ALL[1]);
    expect(host.querySelector('[role="tablist"]')).toBeTruthy();
    expect(tabs(host).map((t) => t.textContent)).toEqual(["Upstream Jobs", "Downstream Jobs"]);
    expect(tab(host, "Upstream Jobs").getAttribute("aria-selected")).toBe("true");
    expect(tab(host, "Downstream Jobs").getAttribute("aria-selected")).toBe("false");
    expect(shown(panel(host, "upstream"))).toBe(true);
    expect(shown(panel(host, "downstream"))).toBe(false);
  });

  it("shows only one list at a time, and switches on a click", async () => {
    const host = tabForJob(ALL[1]);
    await act(async () => { tab(host, "Downstream Jobs").click(); });
    expect(shown(panel(host, "upstream"))).toBe(false);
    expect(shown(panel(host, "downstream"))).toBe(true);
    expect(tab(host, "Downstream Jobs").getAttribute("aria-selected")).toBe("true");
    expect(tab(host, "Upstream Jobs").getAttribute("aria-selected")).toBe("false");
    await act(async () => { tab(host, "Upstream Jobs").click(); });
    expect(shown(panel(host, "upstream"))).toBe(true);
    expect(shown(panel(host, "downstream"))).toBe(false);
  });

  it("marks the open tab like the file panel does: an orange line under it and darker text", async () => {
    const host = tabForJob(ALL[1]);
    const accent = (t: HTMLElement) => /10,\s*125,\s*90|#0A7D5A/i.test(t.style.borderBottom);
    expect([accent(tab(host, "Upstream Jobs")), accent(tab(host, "Downstream Jobs"))]).toEqual([true, false]);
    await act(async () => { tab(host, "Downstream Jobs").click(); });
    expect([accent(tab(host, "Upstream Jobs")), accent(tab(host, "Downstream Jobs"))]).toEqual([false, true]);
  });

  it("each tab names its panel, and the panel names its tab", () => {
    const host = tabForJob(ALL[1]);
    for (const side of ["upstream", "downstream"] as const) {
      expect(tab(host, side === "upstream" ? "Upstream Jobs" : "Downstream Jobs").getAttribute("aria-controls")).toBe(`job-links-panel-${side}`);
      expect(panel(host, side).getAttribute("aria-labelledby")).toBe(`job-links-tab-${side}`);
      expect(panel(host, side).getAttribute("role")).toBe("tabpanel");
    }
  });

  it("counts the jobs chosen on each side, only when there are some", async () => {
    const host = tabForJob(ALL[1], { upstream_project_ids: ["a"], dependent_project_ids: [] });
    expect(host.querySelector('[data-testid="upstream-count"]')?.textContent).toBe("1");
    expect(host.querySelector('[data-testid="downstream-count"]')).toBeNull();
    await act(async () => { box(host, "Upstream Jobs", "c").click(); });
    expect(host.querySelector('[data-testid="upstream-count"]')?.textContent).toBe("2");
    await act(async () => { tab(host, "Downstream Jobs").click(); });
    await act(async () => { box(host, "Downstream Jobs", "d").click(); });
    expect(host.querySelector('[data-testid="downstream-count"]')?.textContent).toBe("1");
    await act(async () => { box(host, "Downstream Jobs", "d").click(); });
    expect(host.querySelector('[data-testid="downstream-count"]')).toBeNull();
  });

  it("switching tabs loses nothing: both lists keep what was chosen, and Save sends both", async () => {
    const onSave = vi.fn();
    const me = ALL[1];
    const host = render(createElement(DbtDependentsTab, { project: me, projects: ALL, saving: false, onSave }));
    await act(async () => { box(host, "Upstream Jobs", "a").click(); });
    await act(async () => { tab(host, "Downstream Jobs").click(); });
    await act(async () => { box(host, "Downstream Jobs", "d").click(); });
    await act(async () => { tab(host, "Upstream Jobs").click(); });
    expect(box(host, "Upstream Jobs", "a").checked).toBe(true);
    expect(box(host, "Downstream Jobs", "d").checked).toBe(true);
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Save")!.click(); });
    expect(onSave).toHaveBeenCalledWith({ upstream: ["a"], downstream: ["d"] });
  });

  it("each panel keeps its own help text and no list repeats the tab's title as a heading", () => {
    const host = tabForJob(ALL[1]);
    expect(panel(host, "upstream").textContent).toContain("Every run of this job");
    expect(panel(host, "downstream").textContent).toContain("If this job fails or goes out of date");
    expect(host.querySelectorAll("label").length).toBeGreaterThan(0);
    expect([...host.querySelectorAll("label")].filter((l) => l.textContent === "Upstream Jobs" || l.textContent === "Downstream Jobs")).toHaveLength(0);
  });

  it("a job's own reasons (already upstream) stay in the list of the tab they belong to", async () => {
    const host = tabForJob(ALL[1], { upstream_project_ids: ["a"] });
    await act(async () => { tab(host, "Downstream Jobs").click(); });
    expect(panel(host, "downstream").textContent).toContain("already upstream");
  });

  it("the register drawer has the same tabs", () => {
    const host = render(createElement(DbtRegisterDrawer, { projects: ALL, onClose: () => {}, onRegistered: () => {}, kind: "dbt" }));
    expect(tabs(host).map((t) => t.textContent)).toEqual(["Upstream Jobs", "Downstream Jobs"]);
    expect(shown(panel(host, "upstream"))).toBe(true);
    expect(shown(panel(host, "downstream"))).toBe(false);
  });

  it("the button just says Save (Saving… while it works), not 'Save links'", () => {
    const me = ALL[1];
    const idle = render(createElement(DbtDependentsTab, { project: me, projects: ALL, saving: false, onSave: vi.fn() }));
    expect([...idle.querySelectorAll("button")].map((b) => b.textContent)).toContain("Save");
    expect(idle.textContent).not.toContain("Save links");
    const busy = render(createElement(DbtDependentsTab, { project: me, projects: ALL, saving: true, onSave: vi.fn() }));
    expect([...busy.querySelectorAll("button")].map((b) => b.textContent)).toContain("Saving…");
  });

  it("with no other job registered there is nothing to tab between", () => {
    const only = project("only");
    const host = render(createElement(DbtDependentsTab, { project: only, projects: [only], saving: false, onSave: vi.fn() }));
    expect(tabs(host)).toHaveLength(0);
    expect(host.textContent).toContain("No other jobs registered yet");
  });
});

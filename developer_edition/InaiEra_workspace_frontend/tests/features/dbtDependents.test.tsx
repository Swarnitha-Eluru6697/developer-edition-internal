/**
 * Upstream and downstream jobs in the dbt, SQL and Python screens: choosing them (register drawer, Dependents tab), loops refused up
 * front by greying a job out with the reason, and tags on jobs of another kind.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DbtProject } from "../../src/types";
import { DOWNSTREAM_HELP, UPSTREAM_HELP, linkLimits, linksAfterEdit, projectsAbove, projectsBelow } from "../../src/features/dbt/dependents";
import type { JobKind } from "../../src/features/dbt/jobKinds";

const api = vi.hoisted(() => ({ registered: [] as Record<string, unknown>[] }));
vi.mock("../../src/features/dbt/api", () => ({
  useRegisterDbtProjectMutation: () => [(body: Record<string, unknown>) => {
    api.registered.push(body);
    return { unwrap: async () => ({ name: "combined" }) };
  }, { isLoading: false }],
}));
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn() }));

import { DbtDependentsTab } from "../../src/features/dbt/components/DbtDependentsTab";
import { DbtProjectList } from "../../src/features/dbt/components/DbtProjectList";
import { DbtRegisterDrawer } from "../../src/features/dbt/components/DbtRegisterDrawer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (id: string, deps: string[] = [], over: Partial<DbtProject> = {}): DbtProject => ({
  id, org_id: "o", workspace_id: null, name: id, git_url: `https://github.com/acme/${id}.git`, git_ref: "main", project_path: id,
  engine: "pg_lake", schedule_cron: null, schedule_enabled: false, git_trigger_enabled: false, credential_ref: null, last_synced_sha: null,
  requested_ref: null, created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", last_run: null,
  dependent_project_ids: deps, upstream_project_ids: [], ...over,
});

// customers -> scorecard -> executive, products unlinked
const CHAIN = [project("customers", ["scorecard"]), project("scorecard", ["executive"]), project("executive"), project("products")];
// the user's scenario: a, b, c, d, e, f, g, none linked yet
const LETTERS = ["a", "b", "c", "d", "e", "f", "g"].map((id) => project(id));

function render(el: ReturnType<typeof createElement>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(el));
  return host;
}

const group = (host: HTMLElement, title: "Upstream Jobs" | "Downstream Jobs") =>
  host.ownerDocument.querySelector<HTMLElement>(`[role="group"][aria-label="${title}"]`)!;
const row = (host: HTMLElement, title: "Upstream Jobs" | "Downstream Jobs", name: string) =>
  [...group(host, title).querySelectorAll("label")].find((l) => l.querySelector("span")?.textContent === name)!;
const box = (host: HTMLElement, title: "Upstream Jobs" | "Downstream Jobs", name: string) => row(host, title, name).querySelector("input")!;
const tagOf = (host: HTMLElement, title: "Upstream Jobs" | "Downstream Jobs", name: string) =>
  row(host, title, name).querySelector('[data-testid="kind-tag"]')?.textContent ?? null;
const names = (host: HTMLElement, title: "Upstream Jobs" | "Downstream Jobs") =>
  [...group(host, title).querySelectorAll("label")].map((l) => l.querySelector("span")!.textContent);

async function drawer(kind: JobKind, projects: DbtProject[]) {
  const host = render(createElement(DbtRegisterDrawer, { projects, onClose: () => {}, onRegistered: () => {}, kind }));
  return host;
}

beforeEach(() => { api.registered = []; document.body.innerHTML = ""; });

describe("job links: following the links", () => {
  it("finds everything below and above a job, not just its direct links", () => {
    expect(projectsBelow(CHAIN, "customers")).toEqual(["scorecard", "executive"]);
    expect(projectsAbove(CHAIN, "executive")).toEqual(["scorecard", "customers"]);
    expect(projectsBelow(CHAIN, "products")).toEqual([]);
  });

  it("judges the links as they would be after saving: the job's own links are replaced by the chosen lists", () => {
    const after = linksAfterEdit(CHAIN, "scorecard", ["products"], []);
    expect(projectsAbove(after, "scorecard")).toEqual(["products"]);          // customers -> scorecard is gone, products -> scorecard is new
    expect(projectsBelow(after, "scorecard")).toEqual([]);                     // scorecard -> executive is gone
    expect(linksAfterEdit(CHAIN, null, ["customers"], ["executive"]).find((p) => p.id === "customers")!.dependent_project_ids)
      .toEqual(["scorecard", "__new_job__"]);                                  // a job being registered counts, under a stand-in id
  });
});

describe("job links: which jobs are greyed out, and why", () => {
  it("never offers the job itself; greys out downstream what is already above it, and offers upstream what is not below it", () => {
    // executive is edited with its saved upstream ticked: scorecard (which customers feeds)
    const l = linkLimits(CHAIN, "executive", ["scorecard"], []);
    expect([...l.downstream.entries()].sort()).toEqual([["customers", "loop"], ["scorecard", "other-side"]]);   // customers -> scorecard -> executive
    expect(l.upstream.size).toBe(0);
    expect(l.upstream.has("executive") || l.downstream.has("executive")).toBe(false);
    expect(l.alsoAbove).toEqual(["customers"]);                                                              // fails executive too, through scorecard
  });

  it("a job chosen on one side is greyed out on the other: already upstream / already downstream", () => {
    const up = linkLimits(LETTERS, null, ["a", "b", "c"], []);
    expect([...up.downstream.entries()]).toEqual([["a", "other-side"], ["b", "other-side"], ["c", "other-side"]]);
    expect(up.upstream.size).toBe(0);
    const both = linkLimits(LETTERS, null, ["a", "b", "c"], ["e", "f", "g"]);
    expect([...both.upstream.keys()].sort()).toEqual(["e", "f", "g"]);
    expect([...both.downstream.keys()].sort()).toEqual(["a", "b", "c"]);
    expect(new Set([...both.upstream.values(), ...both.downstream.values()])).toEqual(new Set(["other-side"]));
  });

  it("a longer loop is greyed out too: with f -> a, choosing a upstream greys out f downstream", () => {
    const projects = [project("f", ["a"]), project("a"), project("d")];
    const l = linkLimits(projects, null, ["a"], []);
    expect(l.downstream.get("f")).toBe("loop");
    expect(l.alsoAbove).toEqual(["f"]);                                         // f also fails this job, through a
  });

  it("a downstream choice greys out its own downstream jobs as upstream, and lists them as also failing", () => {
    const l = linkLimits(CHAIN, null, [], ["scorecard"]);
    expect(l.upstream.get("scorecard")).toBe("other-side");
    expect(l.upstream.get("executive")).toBe("loop");                           // scorecard -> executive: executive upstream would close a loop
    expect(l.upstream.has("customers")).toBe(false);                            // customers already feeds scorecard: allowed
    expect(l.alsoBelow).toEqual(["executive"]);
  });

  it("editing: a job can move from downstream to upstream in one edit, because the link it is leaving no longer counts", () => {
    // scorecard has customers above it and executive below it; the user un-ticks executive downstream ...
    const l = linkLimits(CHAIN, "scorecard", ["customers"], []);
    expect(l.upstream.has("executive")).toBe(false);                            // ... so ticking it upstream is now fine
    // while it is still ticked downstream, it is "already downstream"
    expect(linkLimits(CHAIN, "scorecard", ["customers"], ["executive"]).upstream.get("executive")).toBe("other-side");
  });

  it("a chosen job is never greyed out itself (it must stay possible to un-tick it)", () => {
    const l = linkLimits(CHAIN, "scorecard", ["customers"], ["executive"]);
    expect(l.upstream.has("customers")).toBe(false);
    expect(l.downstream.has("executive")).toBe(false);
  });
});

describe("job links: register drawer", () => {
  it("has two lists, Upstream Jobs and Downstream Jobs, and no list called Dependents", async () => {
    const host = await drawer("dbt", LETTERS);
    expect(group(host, "Upstream Jobs")).toBeTruthy();
    expect(group(host, "Downstream Jobs")).toBeTruthy();
    expect(host.ownerDocument.querySelector('[role="group"][aria-label="Dependents"]')).toBeNull();
    expect(names(host, "Upstream Jobs")).toEqual(LETTERS.map((p) => p.name));
  });

  it("registers d with a, b, c upstream and e, f, g downstream (the user's example)", async () => {
    const host = await drawer("dbt", LETTERS.filter((p) => p.id !== "d"));
    const type = (placeholder: string, value: string) => {
      const input = host.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => { type("https://github.com/acme/dbt-project.git", "https://github.com/acme/inaiera-dbt-test.git"); type("customers", "d"); });
    for (const n of ["a", "b", "c"]) await act(async () => { box(host, "Upstream Jobs", n).click(); });
    for (const n of ["e", "f", "g"]) await act(async () => { box(host, "Downstream Jobs", n).click(); });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Register project")!.click(); });
    expect(api.registered).toHaveLength(1);
    expect(api.registered[0]).toMatchObject({ project_path: "d", upstream_project_ids: ["a", "b", "c"], dependent_project_ids: ["e", "f", "g"] });
  });

  it("ticking a job upstream greys it out in the downstream list with the reason, and keeps it in the list", async () => {
    const host = await drawer("dbt", LETTERS);
    await act(async () => { box(host, "Upstream Jobs", "a").click(); });
    const downstream = row(host, "Downstream Jobs", "a");
    expect(downstream.querySelector("input")!.disabled).toBe(true);
    expect(downstream.textContent).toContain("already upstream");
    expect(names(host, "Downstream Jobs")).toEqual(LETTERS.map((p) => p.name));          // still listed, same order
    expect(box(host, "Downstream Jobs", "b").disabled).toBe(false);
    await act(async () => { box(host, "Upstream Jobs", "a").click(); });                  // un-tick: free again
    expect(box(host, "Downstream Jobs", "a").disabled).toBe(false);
  });

  it("ticking a job downstream greys it out in the upstream list as already downstream", async () => {
    const host = await drawer("dbt", LETTERS);
    await act(async () => { box(host, "Downstream Jobs", "e").click(); });
    expect(box(host, "Upstream Jobs", "e").disabled).toBe(true);
    expect(row(host, "Upstream Jobs", "e").textContent).toContain("already downstream");
  });

  it("greys out a job that would make a loop through the existing links", async () => {
    const host = await drawer("dbt", [project("f", ["a"]), project("a"), project("b")]);
    await act(async () => { box(host, "Upstream Jobs", "a").click(); });                  // f -> a, so f cannot be downstream of this job
    expect(box(host, "Downstream Jobs", "f").disabled).toBe(true);
    expect(row(host, "Downstream Jobs", "f").textContent).toContain("would make a loop");
    expect(host.ownerDocument.querySelector('[data-testid="upstream-also"]')?.textContent).toContain("f");
  });

  it("lists what also fails through the chosen downstream jobs' own links", async () => {
    const host = await drawer("dbt", CHAIN);
    await act(async () => { box(host, "Downstream Jobs", "scorecard").click(); });
    expect(host.ownerDocument.querySelector('[data-testid="downstream-also"]')?.textContent).toContain("executive");
  });
});

describe("job links: the kind tag shows only on jobs of another kind", () => {
  const mixed = [
    project("customers"),                                                                  // no kind: a dbt project
    project("daily", [], { kind: "sql" }),
    project("etl", [], { kind: "python" }),
    project("revenue", [], { kind: "dbt" }),
  ];
  const tags = (kind: JobKind) => async () => {
    const host = await drawer(kind, mixed);
    return Object.fromEntries(mixed.map((p) => [p.name, [tagOf(host, "Upstream Jobs", p.name), tagOf(host, "Downstream Jobs", p.name)]]));
  };

  it("registering a dbt project: dbt jobs are plain, SQL and Python jobs are tagged", async () => {
    expect(await tags("dbt")()).toEqual({ customers: [null, null], daily: ["SQL", "SQL"], etl: ["Python", "Python"], revenue: [null, null] });
  });

  it("registering a Python job: Python jobs are plain, dbt and SQL jobs are tagged", async () => {
    expect(await tags("python")()).toEqual({ customers: ["dbt", "dbt"], daily: ["SQL", "SQL"], etl: [null, null], revenue: ["dbt", "dbt"] });
  });

  it("registering a SQL job: SQL jobs are plain, dbt and Python jobs are tagged", async () => {
    expect(await tags("sql")()).toEqual({ customers: ["dbt", "dbt"], daily: [null, null], etl: ["Python", "Python"], revenue: ["dbt", "dbt"] });
  });
});

describe("job links: tab", () => {
  it("shows the saved upstream and downstream jobs, saves both lists, and only enables Save once something changed", async () => {
    const onSave = vi.fn();
    const scorecard = { ...CHAIN[1], upstream_project_ids: ["customers"] };
    const host = render(createElement(DbtDependentsTab, { project: scorecard, projects: CHAIN, saving: false, onSave }));
    const save = [...host.querySelectorAll("button")].find((b) => b.textContent === "Save")!;
    expect(save.disabled).toBe(true);
    expect(box(host, "Upstream Jobs", "customers").checked).toBe(true);
    expect(box(host, "Downstream Jobs", "executive").checked).toBe(true);
    await act(async () => { box(host, "Downstream Jobs", "products").click(); });
    expect(save.disabled).toBe(false);
    await act(async () => { save.click(); });
    expect(onSave).toHaveBeenCalledWith({ upstream: ["customers"], downstream: ["executive", "products"] });
  });

  it("changing only the upstream list is enough to enable Save, and likewise only the downstream list", async () => {
    const host = render(createElement(DbtDependentsTab, { project: CHAIN[3], projects: CHAIN, saving: false, onSave: vi.fn() }));
    const save = () => [...host.querySelectorAll("button")].find((b) => b.textContent === "Save")!;
    expect(save().disabled).toBe(true);
    await act(async () => { box(host, "Upstream Jobs", "customers").click(); });
    expect(save().disabled).toBe(false);
    await act(async () => { box(host, "Upstream Jobs", "customers").click(); });          // back to what is saved
    expect(save().disabled).toBe(true);
    await act(async () => { box(host, "Downstream Jobs", "customers").click(); });
    expect(save().disabled).toBe(false);
  });

  it("moves a job from downstream to upstream in one edit", async () => {
    const onSave = vi.fn();
    const scorecard = { ...CHAIN[1], upstream_project_ids: ["customers"] };
    const host = render(createElement(DbtDependentsTab, { project: scorecard, projects: CHAIN, saving: false, onSave }));
    expect(box(host, "Upstream Jobs", "executive").disabled).toBe(true);                    // still ticked downstream
    await act(async () => { box(host, "Downstream Jobs", "executive").click(); });          // un-tick it there ...
    expect(box(host, "Upstream Jobs", "executive").disabled).toBe(false);
    await act(async () => { box(host, "Upstream Jobs", "executive").click(); });            // ... and tick it upstream
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Save")!.click(); });
    expect(onSave).toHaveBeenCalledWith({ upstream: ["customers", "executive"], downstream: [] });
  });

  it("tags jobs of another kind in the tab too, by the kind of the job being edited", () => {
    const sqlJob = project("daily", [], { kind: "sql" });
    const host = render(createElement(DbtDependentsTab, { project: sqlJob, projects: [sqlJob, project("customers"), project("etl", [], { kind: "python" })], saving: false, onSave: () => {} }));
    expect(tagOf(host, "Upstream Jobs", "customers")).toBe("dbt");
    expect(tagOf(host, "Upstream Jobs", "etl")).toBe("Python");
  });
});

describe("job links: help", () => {
  it("describes the rule Dagster enforces, for runs by hand too, and promises no way around it", () => {
    expect(UPSTREAM_HELP).toContain("scheduled or Run now");
    expect(UPSTREAM_HELP).toContain("schedule interval plus 5 minutes");
    expect(UPSTREAM_HELP).toContain("never run");
    for (const text of [UPSTREAM_HELP, DOWNSTREAM_HELP]) expect(text).not.toMatch(/by hand always goes ahead|Run now still works|stopped/);
  });

  it("shows no held-back banner or badge: whether a job may run is Dagster's call, made when it starts", () => {
    const rows = [project("customers"), project("combined", ["customers"])];
    const host = render(createElement(DbtProjectList, { projects: rows, loading: false, onOpen: () => {}, onDelete: () => {} }));
    expect(host.querySelector('[data-testid="blocked-by"]')).toBeNull();
    const tab = render(createElement(DbtDependentsTab, { project: rows[0], projects: rows, saving: false, onSave: () => {} }));
    expect(tab.querySelector('[role="status"]')).toBeNull();
  });
});

describe("job links: no visible scrollbars in the register drawer", () => {
  it("hides the drawer body's and both lists' scrollbars (they still scroll)", async () => {
    const many = Array.from({ length: 12 }, (_, i) => project(`p${i}`));
    const host = await drawer("dbt", many);
    for (const el of [document.querySelector<HTMLElement>('[data-testid="drawer-body"]')!, group(host, "Upstream Jobs"), group(host, "Downstream Jobs")]) {
      expect(el.style.overflowY || el.style.overflow).toBe("auto");
      expect(el.style.scrollbarWidth).toBe("none");
    }
  });
});

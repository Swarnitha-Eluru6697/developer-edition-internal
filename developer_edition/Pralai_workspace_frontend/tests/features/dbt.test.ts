import { describe, expect, it, vi } from "vitest";
import {
  buildDbtScaffold,
  dbtFeatureApi,
  failedModelsText,
  layoutLineage,
  lineageNodeName,
  nodeEventSummary,
  normalizeDbtProjectName,
} from "../../src/features/dbt";
import { FakeRequest, callEndpoint, makeStore } from "../helpers/apiHarness";

describe("dbt: project scaffold", () => {
  it("normalises project names to dbt identifiers", () => {
    expect(normalizeDbtProjectName("  My Project-2 ")).toBe("my_project_2");
    expect(normalizeDbtProjectName("")).toBe("my_project");
    expect(normalizeDbtProjectName("!!!")).toBe("my_project");
  });

  it("creates dbt_project.yml first, then a .gitkeep per standard folder", () => {
    const files = buildDbtScaffold("shop");
    expect(files[0]).toMatchObject({ path: "shop/dbt_project.yml", language: "yaml" });
    expect(files[0].content).toContain("name: 'shop'");
    expect(files.slice(1).every((f) => f.path.endsWith("/.gitkeep") && f.language === "other")).toBe(true);
    expect(files.map((f) => f.path)).toContain("shop/models/staging/.gitkeep");
  });
});

describe("dbt: lineage layout", () => {
  it("layers nodes by longest path and strips schema prefixes", () => {
    const g = layoutLineage([
      { source_table: '"raw"."orders"', target_table: "stg.stg_orders" },
      { source_table: "stg.stg_orders", target_table: "marts.fct_orders" },
      { source_table: '"raw"."orders"', target_table: "marts.fct_orders" },
    ]);
    expect(g.nodes.sort()).toEqual(["fct_orders", "orders", "stg_orders"]);
    expect(g.pos.orders.x).toBeLessThan(g.pos.stg_orders.x);
    expect(g.pos.stg_orders.x).toBeLessThan(g.pos.fct_orders.x);
    expect(lineageNodeName('"a"."b"."c"')).toBe("c");
  });
});

describe("dbt: run events", () => {
  it("summarises node_finish outcome and timing", () => {
    expect(nodeEventSummary({ payload: { status: "success", execution_time: 1.5 } })).toEqual({ ok: true, seconds: 1.5, message: null, output: null });
    expect(nodeEventSummary({ payload: { status: "error" } })).toEqual({ ok: false, seconds: null, message: null, output: null });
    // a job step: what it printed, and why it failed
    expect(nodeEventSummary({ payload: { status: "error", message: "the script failed (exit code 1)", output: "Traceback\nboom" } }))
      .toMatchObject({ ok: false, message: "the script failed (exit code 1)", output: "Traceback\nboom" });
    // a successful dbt node's message is dbt's own chatter: not shown. A successful job step keeps its message next to its output.
    expect(nodeEventSummary({ payload: { status: "success", message: "CREATE TABLE (3 rows)" } }).message).toBeNull();
    expect(nodeEventSummary({ payload: { status: "success", message: "exit code 0 · 1.2s", output: "hi" } }))
      .toMatchObject({ message: "exit code 0 · 1.2s", output: "hi" });
    expect(nodeEventSummary({ payload: { status: "success", output: "   " } }).output).toBeNull();
  });
});

describe("dbt: endpoints (proxied at /api/v1/dbt)", () => {
  it("listDbtProjects filters by workspace", async () => {
    const { request } = await callEndpoint(dbtFeatureApi.endpoints.listDbtProjects, { workspaceId: "w1" }, []);
    expect(request).toMatchObject({ method: "GET", url: "/api/v1/dbt/projects?workspace_id=w1" });
  });

  it("triggerDbtRun POSTs the --select expression", async () => {
    const { request } = await callEndpoint(dbtFeatureApi.endpoints.triggerDbtRun, { projectId: "p1", select: "tag:daily" }, {});
    expect(request).toEqual({ method: "POST", url: "/api/v1/dbt/projects/p1/run", body: { select: "tag:daily" } });
  });
});


describe("dbt: project list stays in step with runs", () => {
  it("triggering or updating a project refetches the project list (its Last run column)", async () => {
    const gets: string[] = [];
    vi.stubGlobal("Request", FakeRequest);
    vi.stubGlobal("fetch", vi.fn(async (req: FakeRequest) => {
      if (req.method === "GET") gets.push(req.url);
      const body = req.method === "GET" && req.url.endsWith("/dbt/projects") ? [{ id: "p1" }] : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
    try {
      const store = makeStore();
      const sub = store.dispatch(dbtFeatureApi.endpoints.listDbtProjects.initiate({}));
      await sub;
      expect(gets).toEqual(["/api/v1/dbt/projects"]);

      await store.dispatch(dbtFeatureApi.endpoints.triggerDbtRun.initiate({ projectId: "p1" }));
      await vi.waitFor(() => expect(gets.length).toBe(2));

      await store.dispatch(dbtFeatureApi.endpoints.updateDbtProject.initiate({ id: "p1", body: { schedule_cron: "*/5 * * * *" } }));
      await vi.waitFor(() => expect(gets.length).toBe(3));
      sub.unsubscribe();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});


describe("dbt: failed-model wording (list and history agree)", () => {
  it("a failed run shows just the failed count, as failed/total", () => {
    expect(failedModelsText({ models_total: 13, models_failed: 1 })).toBe("1/13 models failed");
    expect(failedModelsText({ models_total: 55, models_failed: 3 })).toBe("3/55 models failed");
  });

  it("says nothing for a run that did not fail, so each screen keeps its usual text", () => {
    expect(failedModelsText({ models_total: 55, models_failed: 0 })).toBeNull();
    expect(failedModelsText({ models_total: 55 })).toBeNull();
    expect(failedModelsText({ models_total: null, models_failed: 2 })).toBeNull();
  });
});

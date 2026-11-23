import { describe, expect, it } from "vitest";
import { dqErrorMessage, dqFeatureApi, jobTypeLabel, leafName, renderArtifacts } from "../../src/features/dq";
import type { DqRender } from "../../src/types";
import { callEndpoint } from "../helpers/apiHarness";

describe("dq: error messages", () => {
  it("explains a 422 refusal with unsupported rules and remediation", () => {
    const e = { status: 422, data: { error: "Refused", unsupported_rules: [{ rule_name: "freshness" }], remediation: "Use warn." } };
    expect(dqErrorMessage(e, "x")).toBe("Refused Unsupported: freshness. Use warn.");
  });

  it("prefixes runner failures and falls back to detail.message", () => {
    expect(dqErrorMessage({ status: 502, data: { message: "runner down" } }, "x")).toBe("The runner couldn't apply it — runner down");
    expect(dqErrorMessage({ data: { detail: { message: "nope" } } }, "x")).toBe("nope");
    expect(dqErrorMessage(undefined, "fallback")).toBe("fallback");
  });
});

describe("dq: render artifacts", () => {
  it("lists only populated artifacts, pretty-printing the GX suite", () => {
    const render = { dbt_artifact: "version: 2", gx_artifact: { a: 1 }, flinksql_artifact: null, soda_artifact: null } as unknown as DqRender;
    const arts = renderArtifacts(render);
    expect(arts.map((a) => a.label)).toEqual(["dbt schema.yml tests", "Great Expectations suite"]);
    expect(arts[1].body).toBe('{\n  "a": 1\n}');
    expect(renderArtifacts(undefined)).toEqual([]);
  });

  it("labels job types and strips table prefixes", () => {
    expect(jobTypeLabel("flink_job")).toBe("FlinkSQL job");
    expect(leafName('"db"."public"."orders"')).toBe("orders");
  });
});

describe("dq: endpoints (proxied at /api/v1/dq)", () => {
  it("listDqBindings filters by job type", async () => {
    const { request } = await callEndpoint(dqFeatureApi.endpoints.listDqBindings, { jobType: "dbt_project" }, []);
    expect(request.url).toBe("/api/v1/dq/bindings?job_type=dbt_project");
  });

  it("syncDqToUcs scopes to one binding when given", async () => {
    const { request } = await callEndpoint(dqFeatureApi.endpoints.syncDqToUcs, { bindingId: "b1" }, { bindings: 1 });
    expect(request).toMatchObject({ method: "POST", url: "/api/v1/dq/sync/ucs?binding_id=b1" });
  });
});

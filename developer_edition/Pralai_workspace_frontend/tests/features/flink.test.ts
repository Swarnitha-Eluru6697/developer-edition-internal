import { describe, expect, it } from "vitest";
import {
  FLINKSQL_TEMPLATE,
  flinkFeatureApi,
  flinkSinks,
  flinkUiJobUrl,
  parseProvenance,
  stripSqlComments,
  userDescription,
} from "../../src/features/flink";
import { callEndpoint } from "../helpers/apiHarness";

const DESC = "Orders stream\n[flink-git] url=https://github.com/a/b ref=main path=job.sql sha=abc123";

describe("flink: git provenance", () => {
  it("parses the [flink-git] marker line", () => {
    expect(parseProvenance(DESC)).toEqual({ url: "https://github.com/a/b", ref: "main", path: "job.sql", sha: "abc123" });
    expect(parseProvenance(null)).toEqual({});
    expect(parseProvenance("no marker")).toEqual({});
  });

  it("keeps only what the user wrote", () => {
    expect(userDescription(DESC)).toBe("Orders stream");
  });
});

describe("flink: SQL helpers", () => {
  it("treats the new-file template as empty", () => {
    expect(stripSqlComments(FLINKSQL_TEMPLATE)).toBe("");
    expect(stripSqlComments("/* x */ SELECT 1; -- y")).toBe("SELECT 1; -- y");
  });

  it("finds INSERT INTO sinks", () => {
    expect(flinkSinks("INSERT INTO `lake`.orders SELECT 1; insert into x select 2")).toEqual(["lake.orders", "x"]);
  });

  it("deep-links running vs finished jobs in the Flink UI", () => {
    expect(flinkUiJobUrl("http://f", "j1", "RUNNING")).toBe("http://f/#/job/running/j1/overview");
    expect(flinkUiJobUrl("http://f", "j1", "FINISHED")).toBe("http://f/#/job/completed/j1/overview");
  });
});

describe("flink: endpoints (proxied at /api/v1/flink)", () => {
  it("deploy sends an empty JSON body so FastAPI doesn't 422", async () => {
    const { request } = await callEndpoint(flinkFeatureApi.endpoints.deployFlinkJob, "j1", {});
    expect(request).toEqual({ method: "POST", url: "/api/v1/flink/jobs/j1/deploy", body: {} });
  });

  it("validateFlinkSql POSTs the FlinkSqlValidate body", async () => {
    const { request, data } = await callEndpoint(flinkFeatureApi.endpoints.validateFlinkSql, { sql: "SELECT 1" }, { valid: true, errors: [] });
    expect(request).toEqual({ method: "POST", url: "/api/v1/flink/validate", body: { sql: "SELECT 1" } });
    expect(data).toEqual({ valid: true, errors: [] });
  });
});

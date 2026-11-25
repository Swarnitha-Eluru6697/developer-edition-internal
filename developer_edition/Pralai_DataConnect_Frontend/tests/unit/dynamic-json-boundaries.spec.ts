/*
 * FE-GAP-02 (continued): the places that used to CAST dynamic JSON to a convenient type — the failed-records drawer, the schema drift dialog,
 * the Pipelines page's stored runtime flow and deploy result toast, and the error-handling `catch` paths. Each now validates, so these tests
 * feed them what a cast would have trusted (wrong types, missing fields, null, a string where an object belongs).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { toFailureInfo } from "../../src/errorCopy";
import { conflictFromDetails, operationResultSummary, qualityModeOf, runtimeFlowFromStorage } from "../../src/pipelineAdapters";
import { qualityFailuresOf } from "../../src/qualityFailures";
import { driftEventOf } from "../../src/schemaDrift";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src");

const envelope = (over: Record<string, unknown> = {}) => ({
  original_topic: "org_1-raw-erp-sap-customers-cdc-v1", pipeline_id: "p1", schema_id: "s1", quality_policy_id: "q1",
  failure_type: "validation", failed_rules: ["not_null"], received_at: "2026-10-09T05:00:00Z", original_record: { id: 1 }, ...over,
});

test.describe("qualityFailuresOf", () => {
  test("a well-formed envelope is returned as is", () => {
    expect(qualityFailuresOf([envelope()])).toEqual([envelope()]);
  });
  test("a failure's Kafka origin (topic position) is kept when it is a number — including 0 — and dropped when it is not (NEW-02)", () => {
    const [kept] = qualityFailuresOf([envelope({ original_partition: 0, original_offset: 0 })]);
    expect(kept).toMatchObject({ original_partition: 0, original_offset: 0 });
    const [dropped] = qualityFailuresOf([envelope({ original_partition: "2", original_offset: null })]);
    expect(dropped).not.toHaveProperty("original_partition");
    expect(dropped).not.toHaveProperty("original_offset");
    expect(qualityFailuresOf([envelope()])[0]).not.toHaveProperty("original_offset");     // a manual check has none
  });
  test("anything that is not an array is no failures", () => {
    for (const bad of [undefined, null, "x", 1, {}, { 0: envelope() }]) expect(qualityFailuresOf(bad)).toEqual([]);
  });
  test("entries that are not objects, or lack the topic or arrival time the drawer shows, are dropped", () => {
    const kept = envelope({ original_topic: "keep" });
    const out = qualityFailuresOf([null, "x", 3, [], envelope({ original_topic: 7 }), envelope({ received_at: undefined }), kept]);
    expect(out.map((f) => f.original_topic)).toEqual(["keep"]);
  });
  test("a missing or wrongly typed optional field gets a neutral value, never `undefined` or the wrong type", () => {
    const [f] = qualityFailuresOf([{ original_topic: "t", received_at: "x", failed_rules: ["a", 1, null], original_record: "not an object", schema_id: 4 }]);
    expect(f).toEqual({
      original_topic: "t", received_at: "x", pipeline_id: "", schema_id: null, quality_policy_id: null, failure_type: "unknown",
      failed_rules: ["a"], original_record: {},
    });
  });
});

test.describe("driftEventOf", () => {
  test("reads the event the backend builds", () => {
    expect(driftEventOf({ field: "price", drift_type: "type_changed", action: "x", details: { approved: { type: "int" }, observed: { type: "string", nullable: false } } }))
      .toEqual({
        field: "price", drift_type: "type_changed",
        approved: { type: "int", nullable: undefined, primary_key: undefined },
        observed: { type: "string", nullable: false, primary_key: undefined },
      });
  });
  test("an event of the wrong shape becomes an empty one (shown as an unrecognized change), never a throw", () => {
    for (const bad of [null, undefined, "x", 4, [], { details: "x" }, { details: null }, { field: 3, drift_type: {} }]) {
      expect(() => driftEventOf(bad), String(bad)).not.toThrow();
      const e = driftEventOf(bad);
      expect(e.field).toBeUndefined();
      expect(e.drift_type).toBeUndefined();
    }
  });
  test("a field description that is not an object is ignored", () => {
    expect(driftEventOf({ field: "a", details: { approved: "int", observed: ["x"] } })).toMatchObject({ field: "a", approved: undefined, observed: undefined });
  });
});

test.describe("runtimeFlowFromStorage never trusts what storage hands back", () => {
  const flow = {
    reachable: true, connect_url: "http://c", bootstrap: "k:9092",
    connectors: [{ name: "c1", pipeline_id: "p1", type: "source", state: "RUNNING", tasks: ["RUNNING", null], topics: ["t"] }],
    topics: [{ topic: "t", messages: 5, pipeline_id: "p1" }],
    sinks: [{ connector: "s1", consumed: 3, lag: 1, topics: ["t"] }],
  };
  test("a valid snapshot round-trips", () => {
    expect(runtimeFlowFromStorage(JSON.stringify(flow))).toEqual(flow);
  });
  test("entries of the wrong shape are dropped, the rest kept", () => {
    const polluted = {
      ...flow,
      connectors: [...flow.connectors, { name: "no-state" }, "x", null, { name: 1, pipeline_id: "p", type: "t", state: "s" }],
      topics: [...flow.topics, { topic: "t2", messages: "many", pipeline_id: "p" }, []],
      sinks: [...flow.sinks, { consumed: 2 }, 7],
    };
    expect(runtimeFlowFromStorage(JSON.stringify(polluted))).toEqual(flow);
  });
  test("a task state that is neither text nor null is dropped from that connector", () => {
    const out = runtimeFlowFromStorage(JSON.stringify({ ...flow, connectors: [{ ...flow.connectors[0], tasks: ["RUNNING", 3, {}] }] }));
    expect(out?.connectors?.[0]?.tasks).toEqual(["RUNNING"]);
  });
  test("a message is kept only when it is text", () => {
    expect(runtimeFlowFromStorage(JSON.stringify({ ...flow, message: "hi" }))?.message).toBe("hi");
    expect(runtimeFlowFromStorage(JSON.stringify({ ...flow, message: 5 }))).not.toHaveProperty("message");
  });
});

test.describe("operationResultSummary", () => {
  test("reads status and runtime.topic when they are text", () => {
    expect(operationResultSummary({ status: "deployed", runtime: { topic: "t1" } })).toEqual({ status: "deployed", topic: "t1" });
  });
  test("a result that is null, text, or has the wrong types says nothing, never throws", () => {
    for (const bad of [null, undefined, "ok", 3, [], { status: 1, runtime: "x" }, { runtime: { topic: 5 } }]) {
      expect(operationResultSummary(bad), String(bad)).toEqual({ status: undefined, topic: undefined });
    }
  });
});

test.describe("qualityModeOf (NEW-01: Observe vs. Enforce)", () => {
  test("only exactly ENFORCE is Enforce; everything else, including a missing value, is the safe default Observe", () => {
    expect(qualityModeOf("ENFORCE")).toBe("ENFORCE");
    for (const other of ["OBSERVE", "enforce", "Enforce", "BLOCK_ON_FAILURE", "", " ENFORCE", undefined, null, 1, true, {}, []]) {
      expect(qualityModeOf(other), String(other)).toBe("OBSERVE");
    }
  });
  test("the edit-conflict view carries the server's mode, and drops one that is not text", () => {
    expect(conflictFromDetails({ name: "p", quality_mode: "ENFORCE" })).toMatchObject({ quality_mode: "ENFORCE" });
    expect(conflictFromDetails({ name: "p", quality_mode: 5 })?.quality_mode).toBeUndefined();
  });
});

test.describe("toFailureInfo (what a catch block caught)", () => {
  test("an ApiError-shaped object keeps its code, request id and details", () => {
    expect(toFailureInfo({ message: "boom", retryable: true, code: "X", requestId: "r1", details: { a: 1 } }))
      .toEqual({ code: "X", message: "boom", retryable: true, requestId: "r1", details: { a: 1 } });
  });
  test("wrongly typed code, request id and details are dropped, not passed through", () => {
    expect(toFailureInfo({ message: "m", retryable: false, code: 5, requestId: {}, details: "text" }))
      .toEqual({ code: undefined, message: "m", retryable: false, requestId: undefined, details: undefined });
  });
  test("anything else becomes a bare, non-retryable message and never throws", () => {
    expect(toFailureInfo(null)).toEqual({ message: "null", retryable: false });
    expect(toFailureInfo("plain")).toEqual({ message: "plain", retryable: false });
    expect(toFailureInfo(new Error("e1"))).toEqual({ message: "e1", retryable: false });
    expect(toFailureInfo({ message: 5 })).toMatchObject({ retryable: false });
  });
});

test.describe("source guard", () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return sourceFiles(path);
      return /\.(ts|tsx)$/.test(name) && !name.endsWith(".generated.ts") ? [path] : [];
    });
  }
  test("no source file casts through `unknown` (`as unknown as`): validate with an adapter in json.ts instead", () => {
    const offenders = sourceFiles(SRC).filter((f) => /\bas unknown as\b/.test(readFileSync(f, "utf-8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "")));
    expect(offenders.map((f) => f.slice(SRC.length))).toEqual([]);
  });
  test("no source file declares or casts to `any`", () => {
    const offenders = sourceFiles(SRC).filter((f) => /(:\s*any\b|\bas any\b|<any>|\bany\[\])/.test(readFileSync(f, "utf-8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "")));
    expect(offenders.map((f) => f.slice(SRC.length))).toEqual([]);
  });
});
